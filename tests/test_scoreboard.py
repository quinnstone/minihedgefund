"""Tests for the scoreboard aggregation."""

from src.tracking.scoreboard import compute_realized_tax_totals, update_scoreboard


class TestUpdateScoreboard:
    def test_first_week_initializes(self):
        prior = {"weekly_returns": [], "current_aum": 10_000.0}
        new = {"week_of": "2026-05-11", "return_pct": 0.02, "spy_pct": 0.01,
               "alpha": 0.01, "aum": 10_200.0}
        sb = update_scoreboard(prior, new, 10_000.0, "2026-05-11", 3, 50.0, 0.0, 17.4)
        assert sb["weeks_tracked"] == 1
        assert sb["current_aum"] == 10_200.0
        assert sb["cumulative_return_pct"] == 0.02
        assert sb["weekly_win_rate"] == 1.0

    def test_multiple_weeks_compound_spy(self):
        prior = {"weekly_returns": [], "current_aum": 10_000.0}
        sb = update_scoreboard(prior, {"week_of": "2026-05-11", "return_pct": 0.02, "spy_pct": 0.01, "aum": 10_200},
                                10_000, "2026-05-11", 0, 0, 0, 0)
        sb = update_scoreboard(sb, {"week_of": "2026-05-18", "return_pct": -0.01, "spy_pct": 0.005, "aum": 10_098},
                                10_000, "2026-05-11", 0, 0, 0, 0)
        # SPY: (1.01 * 1.005) - 1 = 0.01505
        assert abs(sb["spy_cumulative_pct"] - 0.01505) < 1e-4
        assert sb["weeks_tracked"] == 2

    def test_duplicate_week_replaced(self):
        prior = {"weekly_returns": [
            {"week_of": "2026-05-11", "return_pct": 0.02, "spy_pct": 0.01, "aum": 10_200},
        ], "current_aum": 10_200.0}
        # Reprocess same week with updated return
        sb = update_scoreboard(prior, {"week_of": "2026-05-11", "return_pct": 0.05, "spy_pct": 0.02, "aum": 10_500},
                                10_000, "2026-05-11", 0, 0, 0, 0)
        assert sb["weeks_tracked"] == 1  # not duplicated
        assert sb["weekly_returns"][0]["return_pct"] == 0.05

    def test_win_rate(self):
        prior = {"weekly_returns": [], "current_aum": 10_000}
        for ret in [0.02, -0.01, 0.03, 0.01]:
            prior = update_scoreboard(prior, {"week_of": f"2026-05-{ret*100:.0f}",
                                                "return_pct": ret, "spy_pct": 0.005,
                                                "aum": 10_000 * (1 + ret)},
                                       10_000, "2026-05-11", 0, 0, 0, 0)
        # Note: the weeks above all set aum based on `ret` alone, not compounding.
        # We're just testing win rate counts, which is correct: 3 wins out of 4
        assert prior["weekly_win_rate"] == 0.75


class TestRealizedTaxTotals:
    def test_summing_gains_and_losses(self):
        trades = [
            {"kind": "sell", "realized_pnl": 100.0, "is_long_term": False},
            {"kind": "sell", "realized_pnl": -50.0, "is_long_term": False},
            {"kind": "sell", "realized_pnl": 200.0, "is_long_term": True},
            {"kind": "buy", "realized_pnl": None},   # ignored
        ]
        g, l, tax = compute_realized_tax_totals(trades, 0.347, 0.257)
        assert g == 300.0
        assert l == -50.0
        # Tax: 100 * 0.347 + 200 * 0.257 = 34.7 + 51.4 = 86.1
        assert abs(tax - 86.1) < 1e-6
