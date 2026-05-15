"""Tests for the deterministic synthesis fallback."""

from src.agents.synthesis import (
    HEURISTIC_WEIGHTS,
    heuristic_synthesis,
    _polarity_to_score,
)


class TestWeights:
    def test_weights_sum_to_one(self):
        assert abs(sum(HEURISTIC_WEIGHTS.values()) - 1.0) < 1e-9

    def test_sentiment_and_insider_dominate(self):
        # Per spec, sentiment + insider should be highest-weighted factors
        ranked = sorted(HEURISTIC_WEIGHTS.items(), key=lambda kv: kv[1], reverse=True)
        top_two = {ranked[0][0], ranked[1][0]}
        assert top_two == {"sentiment", "insider"}


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
        # Expected: 90*0.2 + 70*0.15 + 80*0.15 + 50*0.10 + 50*0.05 + 85*0.15 + 60*0.20
        #         = 18 + 10.5 + 12 + 5 + 2.5 + 12.75 + 12 = 72.75
        assert abs(c["unified_score"] - 72.75) < 0.05

    def test_missing_scout_defaults_to_50(self):
        # No earnings/news/insider data — each should default to 50
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
        # 60*0.2 + 50*0.15 + 60*0.15 + 50*0.10 + 50*0.05 + 50*0.15 + 50*0.20
        # = 12 + 7.5 + 9 + 5 + 2.5 + 7.5 + 10 = 53.5
        assert abs(c["unified_score"] - 53.5) < 0.05

    def test_ranks_descending(self):
        briefs = {
            "sentiment": {"candidates": [
                {"ticker": "WIN", "composite_score": 95},
                {"ticker": "LOSS", "composite_score": 20},
            ]},
            "earnings":  {"candidates": []},
            "technical": {"candidates": []},
            "news":      {"candidates": []},
            "insider":   {"candidates": []},
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
