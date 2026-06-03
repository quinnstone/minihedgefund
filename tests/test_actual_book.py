"""Tests for the actual paper-money book parser + builder."""

from datetime import date

import pytest

from src.tracking.actual_book import (
    ActualEntry,
    INITIAL_CAPITAL,
    ParseError,
    _parse_line,
    build_actual_book,
    held_tickers_from_entries,
)


# ─── Parser ────────────────────────────────────────────────────────────

class TestParser:
    def test_basic_line(self):
        e = _parse_line("2026-05-19  NVDA  buy  5.2  235.50")
        assert e == ActualEntry(date(2026, 5, 19), "NVDA", "buy", 5.2, 235.50, "")

    def test_notes_preserved(self):
        e = _parse_line("2026-05-19  NVDA  buy  5.2  235.50  after hours, thin volume")
        assert e.notes == "after hours, thin volume"

    def test_tabs_and_multi_space_ok(self):
        e = _parse_line("2026-05-19\tNVDA\t\tbuy   5.2\t235.50")
        assert e.ticker == "NVDA"
        assert e.shares == 5.2

    def test_case_normalization(self):
        e = _parse_line("2026-05-19 nvda BUY 5 235.5")
        assert e.ticker == "NVDA"
        assert e.action == "buy"

    def test_sell_action(self):
        e = _parse_line("2026-06-02 AMZN sell 0.4 178.25")
        assert e.action == "sell"

    def test_integer_shares_and_price(self):
        e = _parse_line("2026-06-01 QQQ buy 1 730")
        assert e.shares == 1.0
        assert e.price == 730.0

    def test_bad_date_raises(self):
        with pytest.raises(ParseError, match="bad date"):
            _parse_line("05-19-2026 NVDA buy 5.2 235.50")

    def test_bad_action_raises(self):
        with pytest.raises(ParseError, match="must be 'buy' or 'sell'"):
            _parse_line("2026-05-19 NVDA hold 5.2 235.50")

    def test_negative_shares_raises(self):
        with pytest.raises(ParseError, match="positive number"):
            _parse_line("2026-05-19 NVDA buy -5 235.50")

    def test_zero_price_raises(self):
        with pytest.raises(ParseError, match="positive number"):
            _parse_line("2026-05-19 NVDA buy 5.2 0")

    def test_too_few_fields_raises(self):
        with pytest.raises(ParseError, match="expected at least 5"):
            _parse_line("2026-05-19 NVDA buy 5.2")


# ─── Builder ───────────────────────────────────────────────────────────

class TestBuilder:
    def test_empty_entries_empty_book(self):
        b = build_actual_book([], current_price_map={})
        assert b["entries_count"] == 0
        assert b["positions"] == []
        assert b["current_aum"] == INITIAL_CAPITAL

    def test_single_buy_position_visible(self):
        entries = [ActualEntry(date(2026, 5, 19), "NVDA", "buy", 2.0, 100.0)]
        b = build_actual_book(entries, current_price_map={"NVDA": 110.0})
        assert len(b["positions"]) == 1
        p = b["positions"][0]
        assert p["ticker"] == "NVDA"
        assert p["shares"] == 2.0
        assert p["avg_cost_per_share"] == 100.0
        assert p["market_value"] == 220.0
        assert p["unrealized_pnl"] == 20.0

    def test_aum_math_includes_cash(self):
        entries = [ActualEntry(date(2026, 5, 19), "NVDA", "buy", 2.0, 100.0)]
        b = build_actual_book(entries, current_price_map={"NVDA": 110.0})
        # Cash: 10000 - 200 = 9800; positions value: 220; AUM = 10020
        assert b["cash"] == 9800.0
        assert b["positions_value"] == 220.0
        assert b["current_aum"] == 10020.0

    def test_full_sell_closes_position(self):
        entries = [
            ActualEntry(date(2026, 5, 19), "NVDA", "buy", 2.0, 100.0),
            ActualEntry(date(2026, 5, 22), "NVDA", "sell", 2.0, 110.0),
        ]
        b = build_actual_book(entries, current_price_map={"NVDA": 115.0})
        assert b["positions"] == []
        # Cash: 10000 - 200 + 220 = 10020; AUM = 10020 (all cash)
        assert b["cash"] == 10020.0
        assert b["current_aum"] == 10020.0
        assert b["realized_pnl"] == 20.0
        assert b["closed_trades_count"] == 1

    def test_partial_sell_keeps_remainder(self):
        entries = [
            ActualEntry(date(2026, 5, 19), "NVDA", "buy", 5.0, 100.0),
            ActualEntry(date(2026, 5, 22), "NVDA", "sell", 2.0, 110.0),
        ]
        b = build_actual_book(entries, current_price_map={"NVDA": 115.0})
        assert len(b["positions"]) == 1
        assert b["positions"][0]["shares"] == 3.0
        # Realized PnL on 2 shares: (110-100)*2 = 20
        assert b["realized_pnl"] == 20.0

    def test_fifo_across_multiple_lots(self):
        # Two buys at different prices, partial sell — FIFO closes from oldest lot first
        entries = [
            ActualEntry(date(2026, 5, 19), "NVDA", "buy", 2.0, 100.0),
            ActualEntry(date(2026, 5, 26), "NVDA", "buy", 2.0, 150.0),
            ActualEntry(date(2026, 6, 2),  "NVDA", "sell", 3.0, 200.0),
        ]
        b = build_actual_book(entries, current_price_map={"NVDA": 200.0})
        # FIFO: sell 2 from $100 lot (full) + 1 from $150 lot
        # Realized: (200-100)*2 + (200-150)*1 = 200 + 50 = 250
        assert b["realized_pnl"] == 250.0
        # Remainder: 1 share @ $150 cost basis
        assert len(b["positions"]) == 1
        assert b["positions"][0]["shares"] == 1.0
        assert b["positions"][0]["avg_cost_per_share"] == 150.0

    def test_inception_date_is_first_entry(self):
        entries = [
            ActualEntry(date(2026, 6, 2),  "AAPL", "buy", 1.0, 150.0),
            ActualEntry(date(2026, 5, 19), "NVDA", "buy", 1.0, 100.0),   # earlier date
        ]
        b = build_actual_book(entries, current_price_map={"NVDA": 100.0, "AAPL": 150.0})
        # Sorted internally → inception = 2026-05-19
        assert b["inception_date"] == "2026-05-19"

    def test_leverage_flag_when_over_deployed(self):
        entries = [
            ActualEntry(date(2026, 5, 19), "NVDA", "buy", 100.0, 200.0),   # $20k spend on $10k notional
        ]
        b = build_actual_book(entries, current_price_map={"NVDA": 200.0})
        assert b["leveraged"] is True
        assert b["cash"] == -10_000.0

    def test_spy_benchmark_math(self):
        entries = [ActualEntry(date(2026, 5, 19), "NVDA", "buy", 1.0, 100.0)]
        # SPY: $500 inception → $510 current = +2%
        # Book: cash 9900 + NVDA at 110 = 10010 → +0.1%
        # Alpha = 0.001 - 0.02 = -0.019
        b = build_actual_book(
            entries,
            current_price_map={"NVDA": 110.0},
            spy_inception_price=500.0,
            spy_current_price=510.0,
        )
        assert abs(b["spy_return_from_inception_pct"] - 0.02) < 1e-6
        assert abs(b["cumulative_return_pct"] - 0.001) < 1e-6
        assert abs(b["alpha_pct"] - (-0.019)) < 1e-6

    def test_sell_with_no_position_skipped(self):
        """Defensive: a sell-before-buy doesn't crash the build."""
        entries = [ActualEntry(date(2026, 5, 19), "NVDA", "sell", 1.0, 100.0)]
        b = build_actual_book(entries, current_price_map={})
        assert b["positions"] == []
        assert b["closed_trades_count"] == 0
        assert b["cash"] == INITIAL_CAPITAL    # nothing happened


class TestHeldTickers:
    def test_buy_then_full_sell_not_held(self):
        entries = [
            ActualEntry(date(2026, 5, 19), "NVDA", "buy", 2.0, 100.0),
            ActualEntry(date(2026, 5, 22), "NVDA", "sell", 2.0, 110.0),
        ]
        assert held_tickers_from_entries(entries) == []

    def test_multiple_tickers_partial(self):
        entries = [
            ActualEntry(date(2026, 5, 19), "NVDA", "buy", 5.0, 100.0),
            ActualEntry(date(2026, 5, 20), "AAPL", "buy", 3.0, 150.0),
            ActualEntry(date(2026, 5, 22), "NVDA", "sell", 5.0, 110.0),   # fully sold
            ActualEntry(date(2026, 5, 22), "AAPL", "sell", 1.0, 160.0),   # partial
        ]
        assert held_tickers_from_entries(entries) == ["AAPL"]
