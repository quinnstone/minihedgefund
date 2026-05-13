"""Tests for the tax engine — STCG/LTCG, lot selection, wash sale, LTCG proximity, TLH."""

from datetime import date
import pytest

from src.portfolio.state import ClosedLot, Lot, Position
from src.portfolio.tax import (
    LotSelection,
    NYC_140K_SINGLE,
    TaxBrackets,
    TaxEngine,
)


def _lot(lot_id: str, shares: float, cost: float, acq: date) -> Lot:
    return Lot(lot_id, "NVDA", shares, cost, acq)


class TestBrackets:
    def test_nyc_140k_rates(self):
        # Sanity: STCG ~34.7%, LTCG ~25.7%
        assert abs(NYC_140K_SINGLE.stcg_rate - 0.3473) < 0.001
        assert abs(NYC_140K_SINGLE.ltcg_rate - 0.2573) < 0.001

    def test_niit_bump(self):
        b = TaxBrackets(federal_ordinary=0.24, federal_ltcg=0.15, state=0, city=0, niit_applies=True)
        assert b.stcg_rate == pytest.approx(0.24 + 0.038)
        assert b.ltcg_rate == pytest.approx(0.15 + 0.038)


class TestClassification:
    def test_long_term_boundary(self):
        te = TaxEngine()
        assert te.is_long_term(date(2025, 5, 11), date(2026, 5, 11)) is True   # exactly 365
        assert te.is_long_term(date(2025, 5, 11), date(2026, 5, 10)) is False  # 364
        assert te.is_long_term(date(2025, 5, 11), date(2026, 5, 12)) is True   # 366

    def test_estimated_tax_only_on_gains(self):
        te = TaxEngine()
        assert te.estimated_tax(0, True) == 0
        assert te.estimated_tax(-100, False) == 0
        # Gain
        assert te.estimated_tax(100, False) == pytest.approx(100 * te.brackets.stcg_rate)
        assert te.estimated_tax(100, True) == pytest.approx(100 * te.brackets.ltcg_rate)


class TestLotSelection:
    def _pos_three_lots(self) -> Position:
        return Position("NVDA", [
            _lot("a", 2.0, 100.0, date(2025, 3, 1)),   # oldest, low basis (LT-qualified)
            _lot("b", 3.0, 120.0, date(2025, 5, 1)),   # middle (LT-qualified)
            _lot("c", 5.0, 160.0, date(2026, 3, 1)),   # newest, high basis (short-term)
        ])

    def test_fifo(self):
        te = TaxEngine()
        plan = te.select_lots_to_sell(
            self._pos_three_lots(), 4.0, 140.0, date(2026, 5, 11), LotSelection.FIFO,
        )
        # FIFO: pull from a then b
        assert plan == [("a", 2.0), ("b", 2.0)]

    def test_lifo(self):
        te = TaxEngine()
        plan = te.select_lots_to_sell(
            self._pos_three_lots(), 4.0, 140.0, date(2026, 5, 11), LotSelection.LIFO,
        )
        # LIFO: pull from c first
        assert plan == [("c", 4.0)]

    def test_hifo_picks_highest_basis(self):
        te = TaxEngine()
        plan = te.select_lots_to_sell(
            self._pos_three_lots(), 4.0, 140.0, date(2026, 5, 11), LotSelection.HIFO,
        )
        # HIFO: highest cost basis first (c=160, then b=120)
        assert plan == [("c", 4.0)]

    def test_auto_at_loss_uses_hifo(self):
        # Sell price below avg cost (avg = (200+360+800)/10 = 136); price=120 = loss
        te = TaxEngine()
        plan = te.select_lots_to_sell(
            self._pos_three_lots(), 4.0, 120.0, date(2026, 5, 11), LotSelection.AUTO,
        )
        # At a loss → HIFO → c first
        assert plan == [("c", 4.0)]

    def test_auto_at_gain_prefers_oldest_lt(self):
        # Sell price 200 > avg cost 136 = gain; AUTO should prefer oldest LT-qualified
        te = TaxEngine()
        plan = te.select_lots_to_sell(
            self._pos_three_lots(), 4.0, 200.0, date(2026, 5, 11), LotSelection.AUTO,
        )
        # At gain → oldest LT-qualified first (a, then b)
        assert plan == [("a", 2.0), ("b", 2.0)]

    def test_oversell_rejected(self):
        te = TaxEngine()
        with pytest.raises(ValueError):
            te.select_lots_to_sell(
                self._pos_three_lots(), 100.0, 140.0, date(2026, 5, 11), LotSelection.AUTO,
            )


class TestWashSale:
    def _loss_lot(self, sell_date: date) -> ClosedLot:
        return ClosedLot("zz", "NVDA", 1.0, 150.0, 130.0, date(2026, 1, 1), sell_date)

    def test_recent_loss_blocks_rebuy(self):
        te = TaxEngine()
        chk = te.check_wash_sale("NVDA", date(2026, 5, 15), [self._loss_lot(date(2026, 5, 1))])
        assert chk.is_wash_sale_risk is True
        assert chk.days_until_clear > 0
        assert chk.blocking_realized_loss < 0

    def test_old_loss_does_not_block(self):
        te = TaxEngine()
        chk = te.check_wash_sale("NVDA", date(2026, 5, 15), [self._loss_lot(date(2026, 3, 1))])
        assert chk.is_wash_sale_risk is False

    def test_gain_does_not_block(self):
        te = TaxEngine()
        gain = ClosedLot("zz", "NVDA", 1.0, 100.0, 130.0, date(2026, 1, 1), date(2026, 5, 1))
        chk = te.check_wash_sale("NVDA", date(2026, 5, 15), [gain])
        assert chk.is_wash_sale_risk is False

    def test_different_ticker_does_not_block(self):
        te = TaxEngine()
        chk = te.check_wash_sale("AMD", date(2026, 5, 15), [self._loss_lot(date(2026, 5, 1))])
        assert chk.is_wash_sale_risk is False


class TestLTCGProximity:
    def test_flags_lots_near_long_term(self):
        te = TaxEngine()
        # acquired 335 days before 2026-05-11
        pos = Position("AAPL", [Lot("x", "AAPL", 10.0, 150.0, date(2025, 6, 10))])
        flags = te.ltcg_proximity_flags(pos, date(2026, 5, 11), current_price=180.0)
        assert len(flags) == 1
        assert flags[0].days_to_long_term == 30
        # extra tax = 300 (unrealized) * (STCG - LTCG)
        expected_extra = 300 * (te.brackets.stcg_rate - te.brackets.ltcg_rate)
        assert flags[0].extra_tax_if_sold_now == pytest.approx(expected_extra)

    def test_no_flag_when_already_lt(self):
        te = TaxEngine()
        pos = Position("AAPL", [Lot("x", "AAPL", 10.0, 150.0, date(2024, 1, 1))])
        flags = te.ltcg_proximity_flags(pos, date(2026, 5, 11), current_price=180.0)
        assert flags == []

    def test_no_flag_when_unrealized_loss(self):
        te = TaxEngine()
        pos = Position("AAPL", [Lot("x", "AAPL", 10.0, 200.0, date(2025, 6, 10))])
        flags = te.ltcg_proximity_flags(pos, date(2026, 5, 11), current_price=180.0)
        assert flags == []


class TestTLH:
    def test_ranks_by_savings(self):
        te = TaxEngine()
        positions = {
            "AAA": Position("AAA", [Lot("a", "AAA", 10, 100, date(2026, 4, 1))]),
            "BBB": Position("BBB", [Lot("b", "BBB", 10, 100, date(2024, 1, 1))]),  # LT
        }
        prices = {"AAA": 80.0, "BBB": 60.0}   # both at losses
        out = te.tlh_candidates(positions, prices, date(2026, 5, 11))
        assert len(out) == 2
        # BBB has the bigger loss → bigger savings, even at LTCG rate
        assert out[0].ticker == "BBB"

    def test_year_end_window(self):
        assert TaxEngine.in_year_end_tlh_window(date(2026, 11, 15)) is True
        assert TaxEngine.in_year_end_tlh_window(date(2026, 12, 31)) is True
        assert TaxEngine.in_year_end_tlh_window(date(2026, 10, 31)) is False
        assert TaxEngine.in_year_end_tlh_window(date(2026, 5, 11)) is False
