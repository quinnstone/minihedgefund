"""Tests for portfolio state, lots, and AUM math."""

from datetime import date, datetime
from src.portfolio.state import ClosedLot, Lot, Position, PortfolioState


class TestLot:
    def test_total_cost(self):
        lot = Lot("a", "NVDA", 5.0, 130.0, date(2026, 5, 11))
        assert lot.total_cost == 650.0

    def test_market_value_and_pnl(self):
        lot = Lot("a", "NVDA", 5.0, 130.0, date(2026, 5, 11))
        assert lot.market_value(140.0) == 700.0
        assert lot.unrealized_pnl(140.0) == 50.0
        assert abs(lot.unrealized_pnl_pct(140.0) - 0.07692) < 1e-4

    def test_holding_period(self):
        lot = Lot("a", "NVDA", 1.0, 100.0, date(2025, 5, 11))
        assert lot.holding_period_days(date(2026, 5, 11)) == 365

    def test_round_trip_serialization(self):
        lot = Lot("a", "NVDA", 5.0, 130.0, date(2026, 5, 11))
        d = lot.to_dict()
        restored = Lot.from_dict(d)
        assert restored == lot


class TestPosition:
    def test_aggregate_metrics(self):
        pos = Position(
            ticker="NVDA",
            lots=[
                Lot("a", "NVDA", 2.0, 100.0, date(2026, 1, 1)),
                Lot("b", "NVDA", 3.0, 120.0, date(2026, 2, 1)),
            ],
        )
        assert pos.total_shares == 5.0
        assert pos.total_cost_basis == 560.0
        assert pos.avg_cost_per_share == 112.0
        assert pos.market_value(130.0) == 650.0
        assert pos.unrealized_pnl(130.0) == 90.0
        assert pos.oldest_lot_date() == date(2026, 1, 1)

    def test_empty_position(self):
        pos = Position(ticker="X", lots=[])
        assert pos.total_shares == 0
        assert pos.avg_cost_per_share == 0
        assert pos.unrealized_pnl_pct(100.0) == 0
        assert pos.oldest_lot_date() is None

    def test_ticker_mismatch_rejected(self):
        pos = Position(ticker="NVDA", lots=[])
        import pytest
        with pytest.raises(ValueError):
            pos.add_lot(Lot("a", "AAPL", 1.0, 100.0, date(2026, 1, 1)))


class TestPortfolioState:
    def test_open_lot_debits_cash(self):
        p = PortfolioState(cash=10_000.0, initial_capital=10_000.0)
        lot = p.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))
        assert p.cash == 9350.0
        assert p.positions["NVDA"].total_shares == 5.0
        assert lot.acquisition_date == date(2026, 5, 11)

    def test_insufficient_cash_rejected(self):
        import pytest
        p = PortfolioState(cash=100.0, initial_capital=10_000.0)
        with pytest.raises(ValueError):
            p.open_lot("NVDA", 10.0, 130.0, date(2026, 5, 11))

    def test_close_lots_credits_cash_and_removes_position(self):
        p = PortfolioState(cash=10_000.0, initial_capital=10_000.0)
        lot = p.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))
        closed = p.close_lots("NVDA", [(lot.lot_id, 5.0)], 145.0, date(2026, 5, 18))
        assert len(closed) == 1
        assert closed[0].realized_pnl == 75.0
        assert p.cash == 10_075.0
        assert "NVDA" not in p.positions  # auto-removed when empty

    def test_partial_close_keeps_position(self):
        p = PortfolioState(cash=10_000.0, initial_capital=10_000.0)
        lot = p.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))
        p.close_lots("NVDA", [(lot.lot_id, 2.0)], 145.0, date(2026, 5, 18))
        assert "NVDA" in p.positions
        assert p.positions["NVDA"].total_shares == 3.0

    def test_total_aum_includes_cash_and_positions(self):
        p = PortfolioState(cash=5_000.0, initial_capital=10_000.0)
        p.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))   # spends $650
        # cash = 4350, position MV at $140 = 700
        assert p.total_aum({"NVDA": 140.0}) == 5_050.0

    def test_position_weights(self):
        p = PortfolioState(cash=5_000.0, initial_capital=10_000.0)
        p.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))   # $650 spent
        weights = p.position_weights({"NVDA": 140.0})
        # AUM = 4350 + 700 = 5050; NVDA weight = 700/5050
        assert abs(weights["NVDA"] - 700.0 / 5050.0) < 1e-6

    def test_round_trip_serialization(self):
        p = PortfolioState(cash=5_000.0, initial_capital=10_000.0, inception_date=date(2026, 5, 1))
        p.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))
        p.last_updated = datetime(2026, 5, 11, 14, 0, 0)
        d = p.to_dict()
        p2 = PortfolioState.from_dict(d)
        assert p2.cash == p.cash
        assert p2.initial_capital == p.initial_capital
        assert p2.inception_date == p.inception_date
        assert p2.positions["NVDA"].total_shares == p.positions["NVDA"].total_shares


class TestClosedLot:
    def test_long_term_threshold(self):
        cl_short = ClosedLot("a", "NVDA", 1, 100, 110, date(2026, 1, 1), date(2026, 12, 1))
        cl_long = ClosedLot("b", "NVDA", 1, 100, 110, date(2025, 1, 1), date(2026, 1, 1))
        assert cl_short.is_long_term is False
        assert cl_long.is_long_term is True

    def test_pnl(self):
        cl = ClosedLot("a", "NVDA", 2.0, 100.0, 110.0, date(2026, 1, 1), date(2026, 6, 1))
        assert cl.proceeds == 220.0
        assert cl.cost_basis == 200.0
        assert cl.realized_pnl == 20.0
