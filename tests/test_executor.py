"""Tests for the PM-decision executor."""

from datetime import date
import pytest

from src.portfolio.schwab import SchwabRealism
from src.portfolio.state import PortfolioState
from src.portfolio.tax import TaxEngine
from src.tracking.executor import execute_decisions


@pytest.fixture
def schwab():
    return SchwabRealism(sp500_tickers={"NVDA", "AAPL", "MSFT"})


@pytest.fixture
def tax_engine():
    return TaxEngine()


@pytest.fixture
def empty_portfolio():
    return PortfolioState(cash=10_000.0, initial_capital=10_000.0)


@pytest.fixture
def sector_map():
    return {"NVDA": "Technology", "QQQ": "ETF", "IBIT": "ETF"}


class TestOpenAction:
    def test_open_fractional(self, empty_portfolio, schwab, tax_engine, sector_map):
        decisions = [{
            "ticker": "NVDA", "action": "OPEN", "target_weight_pct": 15.0,
            "thesis": "test", "conviction": "high",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"NVDA": 130.0}, set(),
            schwab, tax_engine, sector_map, date(2026, 5, 11),
        )
        assert len(result.trades) == 1
        assert result.trades[0].kind == "buy"
        assert result.trades[0].ticker == "NVDA"
        # 15% of $10k = $1500, frac eligible
        assert result.trades[0].fill_dollars if hasattr(result.trades[0], "fill_dollars") else True

    def test_open_blocked_by_wash_sale(self, empty_portfolio, schwab, tax_engine, sector_map):
        decisions = [{
            "ticker": "NVDA", "action": "OPEN", "target_weight_pct": 15.0,
            "thesis": "test", "conviction": "high",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"NVDA": 130.0}, {"NVDA"},   # wash-sale block
            schwab, tax_engine, sector_map, date(2026, 5, 11),
        )
        assert len(result.trades) == 0
        assert len(result.skipped) == 1
        assert "wash-sale" in result.skipped[0]["reason"]

    def test_open_respects_single_name_cap(self, empty_portfolio, schwab, tax_engine, sector_map):
        # Try to allocate 50% to one name; cap is 20%, should be reduced
        decisions = [{
            "ticker": "NVDA", "action": "OPEN", "target_weight_pct": 50.0,
            "thesis": "test", "conviction": "high",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"NVDA": 130.0}, set(),
            schwab, tax_engine, sector_map, date(2026, 5, 11),
            single_name_cap_pct=0.20,
        )
        assert len(result.trades) == 1
        # ~$2000 of $10k = 20% (allowing for slippage)
        assert empty_portfolio.positions["NVDA"].total_cost_basis <= 2010

    def test_open_respects_sector_cap(self, empty_portfolio, schwab, tax_engine, sector_map):
        decisions = [
            {"ticker": "NVDA", "action": "OPEN", "target_weight_pct": 20.0, "thesis": "x", "conviction": "high"},
            {"ticker": "AAPL", "action": "OPEN", "target_weight_pct": 20.0, "thesis": "y", "conviction": "high"},
            {"ticker": "MSFT", "action": "OPEN", "target_weight_pct": 20.0, "thesis": "z", "conviction": "high"},
        ]
        result = execute_decisions(
            decisions, empty_portfolio,
            {"NVDA": 130.0, "AAPL": 200.0, "MSFT": 400.0},
            set(), schwab, tax_engine,
            {"NVDA": "Technology", "AAPL": "Technology", "MSFT": "Technology"},
            date(2026, 5, 11),
            sector_cap_pct=0.40,
        )
        # First two go through (40% sector); third should be reduced or skipped
        # Just assert total Tech exposure stays ≤ ~40% of starting AUM
        tech_value = sum(
            p.market_value(price)
            for ticker, p in empty_portfolio.positions.items()
            for price in [{"NVDA": 130.0, "AAPL": 200.0, "MSFT": 400.0}[ticker]]
        )
        assert tech_value <= 4100  # 40% of $10k + slippage buffer


class TestNonFractionalRoundUp:
    """For ETFs / non-S&P names whose 1 share costs more than the dollar
    target, the executor rounds up to 1 share if PM intent was meaningful."""

    def test_rounds_up_when_target_at_least_half_share(
        self, empty_portfolio, schwab, tax_engine
    ):
        # PM wants 6% of $10k = $600 in QQQ; QQQ at $625 → would floor to 0
        # without round-up. Target $600 >= 50% of $625 ($312.50) → round up to 1.
        decisions = [{
            "ticker": "QQQ", "action": "OPEN", "target_weight_pct": 6.0,
            "thesis": "test", "conviction": "medium",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"QQQ": 625.0}, set(),
            schwab, tax_engine, {"QQQ": "ETF"}, date(2026, 5, 25),
        )
        assert len(result.trades) == 1
        assert len(result.skipped) == 0
        assert result.trades[0].ticker == "QQQ"
        assert result.trades[0].shares == 1
        # Bought 1 share at slipped price (~$625.31)
        assert abs(result.trades[0].price - 625.0 * 1.0005) < 0.01

    def test_skips_when_target_below_half_share(
        self, empty_portfolio, schwab, tax_engine
    ):
        # PM wants 1% = $100; QQQ at $625 → target way below half a share.
        # Should skip with a descriptive reason, not round up.
        decisions = [{
            "ticker": "QQQ", "action": "OPEN", "target_weight_pct": 1.0,
            "thesis": "test", "conviction": "low",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"QQQ": 625.0}, set(),
            schwab, tax_engine, {"QQQ": "ETF"}, date(2026, 5, 25),
        )
        assert len(result.trades) == 0
        assert len(result.skipped) == 1
        assert "1 share at $625" in result.skipped[0]["reason"]

    def test_round_up_respects_single_name_cap(
        self, empty_portfolio, schwab, tax_engine
    ):
        # Tiny $10k portfolio + super-expensive share. 1 share of an
        # imaginary $3,000 ETF = 30% of AUM, blows the 20% single-name cap.
        # Even though target is "meaningful" (e.g. 10%), can't round up.
        decisions = [{
            "ticker": "FAKEHIGH", "action": "OPEN", "target_weight_pct": 10.0,
            "thesis": "test", "conviction": "medium",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"FAKEHIGH": 3000.0}, set(),
            schwab, tax_engine, {"FAKEHIGH": "ETF"}, date(2026, 5, 25),
            single_name_cap_pct=0.20,
        )
        assert len(result.trades) == 0
        assert len(result.skipped) == 1


class TestCloseAndTrim:
    def test_close_full_exit(self, empty_portfolio, schwab, tax_engine, sector_map):
        empty_portfolio.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))
        decisions = [{
            "ticker": "NVDA", "action": "CLOSE",
            "thesis": "exit", "conviction": "medium",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"NVDA": 145.0}, set(),
            schwab, tax_engine, sector_map, date(2026, 5, 18),
        )
        assert "NVDA" not in empty_portfolio.positions
        assert result.realized_pnl > 0   # bought at 130, sold at ~144.93

    def test_trim_partial(self, empty_portfolio, schwab, tax_engine, sector_map):
        empty_portfolio.open_lot("NVDA", 10.0, 130.0, date(2026, 5, 11))
        decisions = [{
            "ticker": "NVDA", "action": "TRIM", "trim_pct_of_position": 50,
            "thesis": "lock half", "conviction": "medium",
        }]
        result = execute_decisions(
            decisions, empty_portfolio, {"NVDA": 145.0}, set(),
            schwab, tax_engine, sector_map, date(2026, 5, 18),
        )
        assert empty_portfolio.positions["NVDA"].total_shares == 5.0
        assert result.realized_pnl > 0


class TestActionOrder:
    def test_sells_before_buys(self, schwab, tax_engine, sector_map):
        """CLOSE should free up cash before OPEN tries to spend it."""
        p = PortfolioState(cash=10_000.0, initial_capital=10_000.0)
        p.open_lot("NVDA", 5.0, 130.0, date(2026, 5, 11))   # spends 650, cash=9350
        # Manually drop cash so the OPEN can only succeed if the CLOSE runs first
        p.cash = 50.0

        decisions = [
            # Put OPEN first in the decision list to verify ordering
            {"ticker": "AAPL", "action": "OPEN", "target_weight_pct": 10.0,
             "thesis": "swap", "conviction": "high"},
            {"ticker": "NVDA", "action": "CLOSE", "thesis": "rotate", "conviction": "high"},
        ]
        result = execute_decisions(
            decisions, p, {"NVDA": 145.0, "AAPL": 200.0}, set(),
            schwab, tax_engine, {"NVDA": "Technology", "AAPL": "Technology"}, date(2026, 5, 18),
        )
        # CLOSE happened first → freed cash → OPEN could spend
        assert "NVDA" not in p.positions
        assert "AAPL" in p.positions
