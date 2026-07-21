"""Tests for the deterministic synthesis fallback."""

from src.agents.synthesis import (
    HEURISTIC_WEIGHTS,
    heuristic_synthesis,
    _polarity_to_score,
)


class TestWeights:
    def test_weights_sum_to_one(self):
        assert abs(sum(HEURISTIC_WEIGHTS.values()) - 1.0) < 1e-9

    def test_insider_is_top_weight(self):
        # As of 2026-07-20: sentiment dropped (anti-signal); insider stays
        # highest-weighted, followed by news/earnings/technical tied at 0.20.
        ranked = sorted(HEURISTIC_WEIGHTS.items(), key=lambda kv: kv[1], reverse=True)
        assert ranked[0][0] == "insider"
        assert HEURISTIC_WEIGHTS["insider"] > HEURISTIC_WEIGHTS["macro_fit"]

    def test_sentiment_is_zero(self):
        # 9-week correlation analysis showed sentiment as an anti-signal
        # (corr -0.35). Weight is 0; keep the key so schema is stable.
        assert "sentiment" in HEURISTIC_WEIGHTS
        assert HEURISTIC_WEIGHTS["sentiment"] == 0.0


class TestPolarityMapping:
    def test_bullish(self):
        assert _polarity_to_score("bullish") == 70.0

    def test_bearish(self):
        assert _polarity_to_score("bearish") == 30.0

    def test_neutral_or_missing(self):
        assert _polarity_to_score("neutral") == 50.0
        assert _polarity_to_score(None) == 50.0
        assert _polarity_to_score("unknown") == 50.0


class TestHeuristicSynthesis:
    def _briefs_for(self, ticker: str, scores: dict[str, float]) -> dict:
        """Build a minimal scout-briefs dict with the given per-ticker scores."""
        return {
            scout: {"candidates": [{"ticker": ticker, "composite_score": score}]}
            for scout, score in scores.items()
        }

    def test_single_ticker_score(self):
        briefs = self._briefs_for("NVDA", {
            "sentiment": 90, "earnings": 70, "technical": 80,
            "news": 85, "insider": 60,
        })
        briefs["macro"] = {"regime": {"overall_regime": "expansion"}}
        briefs["influencer"] = {"candidates": []}

        out = heuristic_synthesis(briefs, ["NVDA"])
        assert out["_fallback_used"] is True
        assert len(out["ranked_candidates"]) == 1
        c = out["ranked_candidates"][0]
        assert c["ticker"] == "NVDA"
        # Expected under 2026-07-20 weights:
        #   insider*0.22 + news*0.20 + earnings*0.20 + technical*0.20 +
        #   macro_fit*0.13 + influencer*0.05 + sentiment*0.00
        # = 60*.22 + 85*.20 + 70*.20 + 80*.20 + 50*.13 + 50*.05 + 90*0
        # = 13.2 + 17 + 14 + 16 + 6.5 + 2.5 + 0 = 69.2
        assert abs(c["unified_score"] - 69.2) < 0.05

    def test_missing_scout_defaults_to_50(self):
        # No earnings/news/insider data — each defaults to 50
        briefs = {
            "sentiment": {"candidates": [{"ticker": "AAA", "composite_score": 60}]},
            "earnings":  {"candidates": []},
            "technical": {"candidates": [{"ticker": "AAA", "composite_score": 60}]},
            "news":      {"candidates": []},
            "insider":   {"candidates": []},
            "macro":     {"regime": {"overall_regime": "expansion"}},
            "influencer": {"candidates": []},
        }
        out = heuristic_synthesis(briefs, ["AAA"])
        c = out["ranked_candidates"][0]
        # 50*.22 + 50*.20 + 50*.20 + 60*.20 + 50*.13 + 50*.05 + 60*0
        # = 11 + 10 + 10 + 12 + 6.5 + 2.5 + 0 = 52.0
        assert abs(c["unified_score"] - 52.0) < 0.05

    def test_ranks_descending(self):
        # sentiment weight is 0 as of 2026-07-20 — differentiate via news + insider
        briefs = {
            "insider":  {"candidates": [
                {"ticker": "WIN", "composite_score": 90},
                {"ticker": "LOSS", "composite_score": 20},
            ]},
            "sentiment": {"candidates": []},
            "earnings":  {"candidates": []},
            "technical": {"candidates": []},
            "news":      {"candidates": []},
            "macro":     {},
            "influencer": {"candidates": []},
        }
        out = heuristic_synthesis(briefs, ["LOSS", "WIN"])
        # Should be sorted by unified_score descending
        assert out["ranked_candidates"][0]["ticker"] == "WIN"
        assert out["ranked_candidates"][1]["ticker"] == "LOSS"

    def test_insider_selling_flag(self):
        briefs = {
            "sentiment": {"candidates": [{"ticker": "T", "composite_score": 50}]},
            "earnings":  {"candidates": []},
            "technical": {"candidates": []},
            "news":      {"candidates": []},
            "insider":   {"candidates": [{"ticker": "T", "composite_score": 25}]},
            "macro":     {},
            "influencer": {"candidates": []},
        }
        out = heuristic_synthesis(briefs, ["T"])
        assert "insider_selling" in out["ranked_candidates"][0]["risk_flags"]

    def test_degraded_signal_flag_when_influencer_dark(self):
        briefs = {
            "sentiment": {"candidates": [{"ticker": "T", "composite_score": 50}]},
            "earnings":  {"candidates": []},
            "technical": {"candidates": []},
            "news":      {"candidates": []},
            "insider":   {"candidates": []},
            "macro":     {},
            "influencer": {"degraded": True, "candidates": []},
        }
        out = heuristic_synthesis(briefs, ["T"])
        assert "degraded_signal" in out["ranked_candidates"][0]["risk_flags"]
