"""Tests for Schwab execution realism — fractional rules + slippage."""

import pytest
from src.portfolio.schwab import SchwabRealism


@pytest.fixture
def schwab():
    return SchwabRealism(sp500_tickers={"NVDA", "AAPL", "MSFT"}, slippage_bps=5)


class TestFractionalEligibility:
    def test_sp500_eligible(self, schwab):
        assert schwab.is_fractional_eligible("NVDA")
        assert schwab.is_fractional_eligible("nvda")  # case-insensitive

    def test_etf_not_eligible(self, schwab):
        assert not schwab.is_fractional_eligible("QQQ")
        assert not schwab.is_fractional_eligible("IBIT")
        assert not schwab.is_fractional_eligible("XLE")


class TestBuy:
    def test_fractional_buy_exact_dollar(self, schwab):
        fill = schwab.buy("NVDA", 700.0, 130.0)
        # slippage: 130 * 1.0005 = 130.065
        assert fill.fractional_eligible is True
        assert fill.cash_residual == 0.0
        assert fill.fill_dollars == pytest.approx(700.0)
        assert fill.fill_shares == pytest.approx(700.0 / 130.065)

    def test_whole_share_rounds_down(self, schwab):
        # IBIT not fractional: $600 / ~$55.03 = 10.9 → 10 shares
        fill = schwab.buy("IBIT", 600.0, 55.0)
        assert fill.fractional_eligible is False
        assert fill.fill_shares == 10
        # Residual ≈ $600 - 10 * 55.0275 = $49.725
        assert fill.cash_residual == pytest.approx(600.0 - 10 * 55.0275)

    def test_buy_with_zero_target_rejected(self, schwab):
        with pytest.raises(ValueError):
            schwab.buy("NVDA", 0.0, 130.0)

    def test_buy_with_zero_price_rejected(self, schwab):
        with pytest.raises(ValueError):
            schwab.buy("NVDA", 100.0, 0.0)


class TestSell:
    def test_fractional_sell_keeps_shares(self, schwab):
        fill = schwab.sell("NVDA", 5.5, 145.0)
        assert fill.fill_shares == 5.5
        # Slippage downward: 145 * 0.9995 = 144.9275
        assert fill.fill_price == pytest.approx(144.9275)

    def test_whole_share_sell_floors(self, schwab):
        # IBIT not fractional: 3.7 shares → 3 shares
        fill = schwab.sell("IBIT", 3.7, 55.0)
        assert fill.fill_shares == 3
        assert fill.fill_dollars == pytest.approx(3 * 55.0 * 0.9995)


class TestSlippage:
    def test_buy_slippage_adverse(self, schwab):
        fill = schwab.buy("NVDA", 1000.0, 100.0)
        assert fill.fill_price > 100.0
        assert fill.fill_price == pytest.approx(100.05)

    def test_sell_slippage_adverse(self, schwab):
        fill = schwab.sell("NVDA", 1.0, 100.0)
        assert fill.fill_price < 100.0
        assert fill.fill_price == pytest.approx(99.95)
