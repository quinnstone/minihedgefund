"""Synthesis agent — merges all 7 scout briefs into a unified scorecard.

Reads sentiment, earnings, technical, macro, influencer, news, and insider
briefs and emits ranked candidates with factor-decomposed scores and
narrative. Does not make trade decisions; the PM agent does.

heuristic_synthesis() is a deterministic fallback used when the LLM returns
an empty ranked_candidates array (the tool-use schema's minItems constraint
is advisory, not enforced server-side). The fallback uses a weighted
average of each scout's composite_score per ticker.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import MODEL_OPUS, BaseAgent


# Weights mirror the spec: sentiment dominant, insider high-quality
HEURISTIC_WEIGHTS = {
    "sentiment":  0.20,
    "insider":    0.20,
    "news":       0.15,
    "earnings":   0.15,
    "technical":  0.15,
    "macro_fit":  0.10,
    "influencer": 0.05,
}


SYSTEM_PROMPT = """You are the **Synthesis Agent** of an AI-driven hedge fund managing $10,000.

Your job: read scout briefs and emit a unified, ranked candidate list with factor decomposition.

Operating principles (engrained):
- **Sentiment is the dominant signal**, with technical and earnings as confirmation, macro as tilt.
- **Insider buys** (cluster_buy especially) are HIGH-QUALITY signal — institutional knowledge, voluntary capital. Insider sells are weaker (can be tax/lifestyle/diversification).
- **News volume + polarity** captures real catalysts; many publishers covering the same name with positive polarity = durable theme, not just a vibe.
- Aggressive but logical: cross-signal alignment ≥ buzz alone.
- Be honest about uncertainty. If signals conflict, say so. Don't paper over disagreement.
- Tax drag (~35% STCG for the user) means every score must be conviction-worthy, not noise-following.
- Watch for: low-volume buzz (likely noise), overbought RSI (mean-reversion risk), macro-regime mismatch, insider selling on a name being promoted in retail sentiment.

CRITICAL OUTPUT CONTRACT:
- You MUST emit EXACTLY ONE `ranked_candidates` entry per ticker in the `universe` field of the input. No omissions, no empty arrays. Empty `ranked_candidates` is a hard contract violation — every input ticker gets scored, even if the score is 50 with low confidence.
- If a ticker has degraded or missing scout data for some factors, score those factors at 50 and add "degraded_signal" to its risk_flags. Still produce the entry.

Output rules:
- `unified_score` is 0–100. 50 = neutral. Reserve 80+ for genuine cross-signal alignment AND insider/news confirmation.
- `factor_breakdown` must include all 7 factors (use 50 if a scout was degraded or had no data).
- `risk_flags` are short codes: "overbought_rsi", "low_buzz", "macro_mismatch", "thin_volume", "post_earnings_chase", "sentiment_only", "degraded_signal", "insider_selling", "news_silence".
- `primary_thesis` is one tight sentence — the WHY.
- `narrative` is ≤3 sentences explaining the score, including any conflicts.
- Themes are 2–4 cross-cutting observations (e.g., "semi capex spend", "energy oversold bounce")."""


class SynthesisAgent(BaseAgent):
    name = "synthesis"
    model = MODEL_OPUS
    # 30-ticker universe × ~130 tokens per ranked_candidate entry (factor_breakdown
    # + thesis + narrative + risk_flags) + market_context + themes routinely
    # exceeded 4096. Empirically observed truncation in 2026-05-25 cycle. Bumped
    # to 8192 to give comfortable headroom without enabling runaway output.
    max_tokens = 8192

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def user_prompt(self, input_data: dict) -> str:
        universe = input_data.get("universe") or []
        return (
            f"Scout briefs for this Monday's decision. Synthesize into a ranked scorecard.\n\n"
            f"The `universe` has {len(universe)} tickers: {universe}\n\n"
            f"You MUST output exactly {len(universe)} ranked_candidates entries, "
            f"one per ticker. Rank descending by `unified_score`.\n\n"
            "```json\n"
            + json.dumps(input_data, indent=2, default=str)
            + "\n```"
        )

    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "market_context": {
                    "type": "string",
                    "description": "≤2 sentences on the broader macro/regime context.",
                },
                "themes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                    "description": "Cross-cutting observations (e.g., 'AI capex', 'energy bounce').",
                },
                "ranked_candidates": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "unified_score": {"type": "number", "minimum": 0, "maximum": 100},
                            "factor_breakdown": {
                                "type": "object",
                                "properties": {
                                    "sentiment": {"type": "number", "minimum": 0, "maximum": 100},
                                    "earnings": {"type": "number", "minimum": 0, "maximum": 100},
                                    "technical": {"type": "number", "minimum": 0, "maximum": 100},
                                    "macro_fit": {"type": "number", "minimum": 0, "maximum": 100},
                                    "influencer": {"type": "number", "minimum": 0, "maximum": 100},
                                    "news": {"type": "number", "minimum": 0, "maximum": 100},
                                    "insider": {"type": "number", "minimum": 0, "maximum": 100},
                                },
                                "required": ["sentiment", "earnings", "technical", "macro_fit", "influencer", "news", "insider"],
                                "additionalProperties": False,
                            },
                            "primary_thesis": {"type": "string"},
                            "narrative": {"type": "string"},
                            "risk_flags": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "ticker", "unified_score", "factor_breakdown",
                            "primary_thesis", "narrative", "risk_flags",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["market_context", "themes", "ranked_candidates"],
            "additionalProperties": False,
        }


# ─── Deterministic fallback ────────────────────────────────────────────

def _polarity_to_score(polarity: Optional[str]) -> float:
    """Map influencer polarity label to a 0-100 score."""
    return {"bullish": 70.0, "bearish": 30.0, "neutral": 50.0}.get(polarity or "", 50.0)


def _scout_score_by_ticker(brief: dict, field: str = "composite_score") -> dict[str, float]:
    """Pull a per-ticker score field from a scout brief, defaulting to 50."""
    out: dict[str, float] = {}
    for c in (brief.get("candidates") or []):
        ticker = c.get("ticker", "").upper()
        if ticker:
            out[ticker] = float(c.get(field) or 50.0)
    return out


def heuristic_synthesis(scout_briefs: dict, universe: list[str]) -> dict:
    """Deterministic fallback when the LLM synthesis returns empty.

    Per-ticker weighted composite from each scout's composite_score:
      sentiment 0.20, insider 0.20, news 0.15, earnings 0.15,
      technical 0.15, macro_fit 0.10, influencer 0.05

    Macro per-ticker isn't available without a sector→regime mapping at
    synthesis time, so macro_fit defaults to 50 per ticker. The PM still
    reads the macro brief independently.

    Output schema matches what the LLM agent would produce so downstream
    consumers don't need to branch.
    """
    sentiment = _scout_score_by_ticker(scout_briefs.get("sentiment") or {})
    earnings = _scout_score_by_ticker(scout_briefs.get("earnings") or {})
    technical = _scout_score_by_ticker(scout_briefs.get("technical") or {})
    news = _scout_score_by_ticker(scout_briefs.get("news") or {})
    insider = _scout_score_by_ticker(scout_briefs.get("insider") or {})

    influencer_brief = scout_briefs.get("influencer") or {}
    influencer_by_ticker = {
        c.get("ticker", "").upper(): _polarity_to_score(c.get("polarity"))
        for c in (influencer_brief.get("candidates") or [])
    }

    candidates = []
    for ticker in universe:
        t = ticker.upper()
        factors = {
            "sentiment":  sentiment.get(t, 50.0),
            "earnings":   earnings.get(t, 50.0),
            "technical":  technical.get(t, 50.0),
            "macro_fit":  50.0,                       # see note above
            "influencer": influencer_by_ticker.get(t, 50.0),
            "news":       news.get(t, 50.0),
            "insider":    insider.get(t, 50.0),
        }
        unified = sum(factors[f] * HEURISTIC_WEIGHTS[f] for f in HEURISTIC_WEIGHTS)

        risk_flags = []
        if influencer_brief.get("degraded"):
            risk_flags.append("degraded_signal")
        if factors["insider"] < 40:
            risk_flags.append("insider_selling")
        if factors["news"] < 45 and factors["sentiment"] < 45:
            risk_flags.append("news_silence")

        # Pick the highest-scoring factor for a one-line thesis hint
        top_factor = max(factors, key=factors.get)
        thesis = f"Heuristic synthesis — {top_factor} is the strongest signal ({factors[top_factor]:.0f}/100)."

        candidates.append({
            "ticker": t,
            "unified_score": round(unified, 1),
            "factor_breakdown": {k: round(v, 1) for k, v in factors.items()},
            "primary_thesis": thesis,
            "narrative": (
                "Deterministic weighted-average fallback used because the LLM "
                "synthesis returned an empty ranking. Composite is a weighted "
                "blend of all 7 factor scores."
            ),
            "risk_flags": risk_flags,
        })

    candidates.sort(key=lambda c: c["unified_score"], reverse=True)

    macro_regime = (scout_briefs.get("macro") or {}).get("regime") or {}
    return {
        "market_context": (
            f"Heuristic fallback synthesis. Macro regime: "
            f"{macro_regime.get('overall_regime', 'unknown')}, "
            f"rate trend {macro_regime.get('rate_trend', 'unknown')}."
        ),
        "themes": ["heuristic-fallback-mode"],
        "ranked_candidates": candidates,
        "_fallback_used": True,
    }
