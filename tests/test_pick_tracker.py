"""Tests for the pick scoreboard tracker."""

from datetime import date

from src.tracking.pick_tracker import (
    FLAT_BAND,
    close_picks,
    compute_aggregate,
    record_picks,
    refresh_open_picks,
    update_weekly_recaps,
    _empty_scoreboard,
)


def _decision(ticker, action, weight=10.0, conviction="medium", thesis="t"):
    return {
        "ticker": ticker, "action": action,
        "target_weight_pct": weight, "conviction": conviction, "thesis": thesis,
    }


def _buy(ticker, price, shares=1.0):
    return {"kind": "buy", "ticker": ticker, "price": price, "shares": shares}


def _sell(ticker, price, shares=1.0):
    return {"kind": "sell", "ticker": ticker, "price": price, "shares": shares}


class TestRecordPicks:
    def test_records_one_pick_per_buy(self):
        sb = _empty_scoreboard()
        decisions = [_decision("NVDA", "OPEN", weight=15, conviction="high")]
        trades = [_buy("NVDA", 235.86)]
        n = record_picks(sb, date(2026, 5, 14), decisions, trades, {"NVDA": 235.74}, [])
        assert n == 1
        assert len(sb["picks"]) == 1
        p = sb["picks"][0]
        assert p["ticker"] == "NVDA"
        assert p["rec_market_price"] == 235.74
        assert p["executed_fill_price"] == 235.86
        assert p["conviction"] == "high"
        assert p["status"] == "open"

    def test_inception_date_set_on_first_record(self):
        sb = _empty_scoreboard()
        assert sb["inception_date"] is None
        record_picks(sb, date(2026, 5, 14), [_decision("NVDA", "OPEN")],
                     [_buy("NVDA", 235.86)], {"NVDA": 235.74}, [])
        assert sb["inception_date"] == "2026-05-14"

    def test_skips_sell_trades(self):
        sb = _empty_scoreboard()
        n = record_picks(sb, date(2026, 5, 14), [], [_sell("NVDA", 235)], {}, [])
        assert n == 0
        assert sb["picks"] == []


class TestClosePicks:
    def test_close_action_closes_matching_open_picks(self):
        sb = _empty_scoreboard()
        record_picks(sb, date(2026, 5, 14),
                     [_decision("NVDA", "OPEN")],
                     [_buy("NVDA", 200.00)],
                     {"NVDA": 200.00}, [])
        # Week 2: CLOSE
        n = close_picks(
            sb, date(2026, 5, 21),
            [_decision("NVDA", "CLOSE")],
            [_sell("NVDA", 220.00)],
            {"NVDA": 220.00},
        )
        assert n == 1
        p = sb["picks"][0]
        assert p["status"] == "closed"
        assert p["closed_at"] == "2026-05-21"
        assert p["closed_price"] == 220.00
        # final_return_pct = (220 - 200) / 200 = 0.10
        assert abs(p["final_return_pct"] - 0.10) < 1e-9

    def test_trim_does_not_close_picks(self):
        sb = _empty_scoreboard()
        record_picks(sb, date(2026, 5, 14), [_decision("NVDA", "OPEN")],
                     [_buy("NVDA", 200.00)], {"NVDA": 200.00}, [])
        n = close_picks(
            sb, date(2026, 5, 21),
            [_decision("NVDA", "TRIM")],
            [_sell("NVDA", 220.00, shares=0.5)],
            {"NVDA": 220.00},
        )
        assert n == 0
        assert sb["picks"][0]["status"] == "open"


class TestRefreshOpenPicks:
    def test_marks_open_picks_to_market(self):
        sb = _empty_scoreboard()
        record_picks(sb, date(2026, 5, 14), [_decision("NVDA", "OPEN")],
                     [_buy("NVDA", 200.00)], {"NVDA": 200.00}, [])
        refresh_open_picks(sb, date(2026, 5, 21), {"NVDA": 220.00})
        p = sb["picks"][0]
        assert p["current_price"] == 220.00
        assert abs(p["lifetime_return_pct"] - 0.10) < 1e-9
        assert p["days_held"] == 7

    def test_skips_closed_picks(self):
        sb = _empty_scoreboard()
        record_picks(sb, date(2026, 5, 14), [_decision("NVDA", "OPEN")],
                     [_buy("NVDA", 200.00)], {"NVDA": 200.00}, [])
        close_picks(sb, date(2026, 5, 21),
                    [_decision("NVDA", "CLOSE")], [_sell("NVDA", 220.00)],
                    {"NVDA": 220.00})
        # Even if we refresh with a different price later, closed picks frozen
        refresh_open_picks(sb, date(2026, 5, 28), {"NVDA": 250.00})
        p = sb["picks"][0]
        assert p["current_price"] == 220.00


class TestAggregate:
    def _setup_three_picks(self) -> dict:
        sb = _empty_scoreboard()
        record_picks(sb, date(2026, 5, 14),
                     [
                         _decision("WIN1", "OPEN", weight=20, conviction="high"),
                         _decision("WIN2", "OPEN", weight=10, conviction="medium"),
                         _decision("LOSS", "OPEN", weight=15, conviction="low"),
                     ],
                     [_buy("WIN1", 100), _buy("WIN2", 100), _buy("LOSS", 100)],
                     {"WIN1": 100, "WIN2": 100, "LOSS": 100}, [])
        refresh_open_picks(sb, date(2026, 5, 21),
                           {"WIN1": 110, "WIN2": 105, "LOSS": 92})
        return sb

    def test_counts_and_win_rate(self):
        sb = self._setup_three_picks()
        agg = compute_aggregate(sb)
        assert agg["total_picks"] == 3
        assert agg["open_picks"] == 3
        assert agg["win_count"] == 2     # WIN1 +10%, WIN2 +5%
        assert agg["loss_count"] == 1    # LOSS -8%
        assert abs(agg["win_rate"] - 2/3) < 1e-9

    def test_basket_returns(self):
        sb = self._setup_three_picks()
        agg = compute_aggregate(sb)
        # Equal-weight: mean of [+0.10, +0.05, -0.08] = +0.0233
        assert abs(agg["equal_weight_basket_return_pct"] - (0.10 + 0.05 - 0.08) / 3) < 1e-9
        # Weighted: (20*0.10 + 10*0.05 + 15*-0.08) / 45 = (2.0 + 0.5 - 1.2) / 45 = 0.0289
        expected_weighted = (20 * 0.10 + 10 * 0.05 + 15 * -0.08) / 45
        assert abs(agg["weighted_basket_return_pct"] - expected_weighted) < 1e-9

    def test_best_worst(self):
        sb = self._setup_three_picks()
        agg = compute_aggregate(sb)
        assert agg["best_pick"]["ticker"] == "WIN1"
        assert agg["worst_pick"]["ticker"] == "LOSS"

    def test_by_conviction_breakdown(self):
        sb = self._setup_three_picks()
        agg = compute_aggregate(sb)
        by_c = agg["by_conviction"]
        assert by_c["high"]["count"] == 1 and by_c["high"]["win_rate"] == 1.0
        assert by_c["medium"]["count"] == 1 and by_c["medium"]["win_rate"] == 1.0
        assert by_c["low"]["count"] == 1 and by_c["low"]["win_rate"] == 0.0

    def test_empty_scoreboard(self):
        agg = compute_aggregate(_empty_scoreboard())
        assert agg["total_picks"] == 0
        assert agg["win_rate"] == 0.0


class TestWeeklyRecaps:
    def test_appends_recap(self):
        sb = _empty_scoreboard()
        record_picks(sb, date(2026, 5, 14),
                     [_decision("A", "OPEN"), _decision("B", "OPEN")],
                     [_buy("A", 100), _buy("B", 100)],
                     {"A": 100, "B": 100}, [])
        refresh_open_picks(sb, date(2026, 5, 14), {"A": 110, "B": 100})
        update_weekly_recaps(sb, date(2026, 5, 14))
        assert len(sb["weekly_recaps"]) == 1
        r = sb["weekly_recaps"][0]
        assert r["week_of"] == "2026-05-14"
        assert r["picks_count"] == 2
        assert "A" in r["tickers"] and "B" in r["tickers"]

    def test_reprocess_replaces_not_duplicates(self):
        sb = _empty_scoreboard()
        record_picks(sb, date(2026, 5, 14), [_decision("A", "OPEN")],
                     [_buy("A", 100)], {"A": 100}, [])
        update_weekly_recaps(sb, date(2026, 5, 14))
        update_weekly_recaps(sb, date(2026, 5, 14))
        assert len(sb["weekly_recaps"]) == 1
