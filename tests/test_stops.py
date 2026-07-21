"""Tests for the mechanical stop-loss + trailing-stop module."""

from datetime import date
from unittest.mock import patch

import pytest

from src.portfolio.state import PortfolioState
from src.tracking.stops import (
    HARD_STOP_LOSS_PCT,
    TRAILING_STOP_DROP,
    TRAILING_STOP_TRIGGER,
    StopSignal,
    evaluate_stops,
    stops_summary_for_brief,
)


def _open_position(p: PortfolioState, ticker: str, shares: float, price: float, acq_date=date(2026, 6, 1)):
    """Convenience: seed a position by directly opening a lot."""
    p.cash += shares * price   # fake cash so open_lot's check passes
    p.open_lot(ticker, shares, price, acq_date)


class TestHardStop:
    def test_triggers_at_negative_ten_pct(self):
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        # Price at $89 → -11% from $100 cost
        with patch("src.tracking.stops._fetch_peak_since", return_value=None):
            signals = evaluate_stops(p, {"NVDA": 89.0})
        assert len(signals) == 1
        assert signals[0].kind == "hard_stop"
        assert signals[0].ticker == "NVDA"
        assert signals[0].pnl_pct_from_cost < HARD_STOP_LOSS_PCT

    def test_no_trigger_above_threshold(self):
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        # Price at $91 → -9% from $100 cost, above the -10% threshold
        with patch("src.tracking.stops._fetch_peak_since", return_value=None):
            signals = evaluate_stops(p, {"NVDA": 91.0})
        assert signals == []

    def test_exact_boundary_triggers(self):
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        with patch("src.tracking.stops._fetch_peak_since", return_value=None):
            signals = evaluate_stops(p, {"NVDA": 90.0})
        assert len(signals) == 1


class TestTrailingStop:
    def test_triggers_after_peak_and_drop(self):
        # Cost 100, peaked 115 (+15%, above 10% trigger),
        # now 109 (-5.2% from peak, past -5% trigger)
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        with patch("src.tracking.stops._fetch_peak_since", return_value=115.0):
            signals = evaluate_stops(p, {"NVDA": 109.0})
        assert len(signals) == 1
        assert signals[0].kind == "trailing_stop"
        assert signals[0].peak_price == 115.0
        assert signals[0].pnl_pct_from_cost > 0   # still net positive

    def test_no_trigger_if_never_hit_ten_pct(self):
        # Peaked at 107 (+7%), below trigger of 10%
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        with patch("src.tracking.stops._fetch_peak_since", return_value=107.0):
            signals = evaluate_stops(p, {"NVDA": 100.0})
        assert signals == []

    def test_no_trigger_if_still_near_peak(self):
        # Peaked at 115, now at 114 → only -0.87% from peak
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        with patch("src.tracking.stops._fetch_peak_since", return_value=115.0):
            signals = evaluate_stops(p, {"NVDA": 114.0})
        assert signals == []

    def test_hard_stop_wins_over_trailing_when_both_apply(self):
        # Cost 100, peaked 115, now 88 (both hard-stop and trailing-stop
        # conditions met). Hard stop fires; trailing is skipped.
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        with patch("src.tracking.stops._fetch_peak_since", return_value=115.0):
            signals = evaluate_stops(p, {"NVDA": 88.0})
        assert len(signals) == 1
        assert signals[0].kind == "hard_stop"


class TestNoise:
    def test_no_position_no_signal(self):
        p = PortfolioState(cash=10_000.0, initial_capital=10_000.0)
        signals = evaluate_stops(p, {"NVDA": 50.0})
        assert signals == []

    def test_missing_price_skips_position(self):
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        signals = evaluate_stops(p, {})   # no price for NVDA
        assert signals == []

    def test_yfinance_failure_disables_trailing_stop_only(self):
        # If peak fetch fails, trailing stop can't be evaluated, but a
        # hard stop should still trigger if the price is far enough below cost.
        p = PortfolioState(cash=0.0, initial_capital=10_000.0)
        _open_position(p, "NVDA", 1.0, 100.0)
        with patch("src.tracking.stops._fetch_peak_since", return_value=None):
            signals_hardstop = evaluate_stops(p, {"NVDA": 85.0})
            signals_nostop = evaluate_stops(p, {"NVDA": 95.0})
        assert len(signals_hardstop) == 1 and signals_hardstop[0].kind == "hard_stop"
        assert signals_nostop == []


class TestSummary:
    def test_summary_shape(self):
        signals = [
            StopSignal("NVDA", "hard_stop", 100, 88, 88, -0.12, None, "reason"),
            StopSignal("AAPL", "trailing_stop", 100, 108, 115, 0.08, -0.061, "reason"),
        ]
        s = stops_summary_for_brief(signals)
        assert s["count"] == 2
        assert s["hard_stops"] == 1
        assert s["trailing_stops"] == 1
        assert len(s["must_close"]) == 2
        assert s["must_close"][0]["ticker"] == "NVDA"

    def test_empty_summary(self):
        s = stops_summary_for_brief([])
        assert s == {"must_close": [], "count": 0, "hard_stops": 0, "trailing_stops": 0}
