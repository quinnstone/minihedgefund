"""Synthesis agent — merges all 5 scout briefs into a unified scorecard.

Reads sentiment, earnings, technical, macro, and influencer briefs and emits
ranked candidates with factor-decomposed scores and narrative. Does not make
trade decisions; the PM agent does.
"""

from __future__ import annotations

import json

from .base import MODEL_OPUS, BaseAgent


SYSTEM_PROMPT = """You are the **Synthesis Agent** of an AI-driven hedge fund managing $10,000.

Your job: read scout briefs and emit a unified, ranked candidate list with factor decomposition.

Operating principles (engrained):
- **Sentiment is the dominant signal**, with technical and earnings as confirmation, macro as tilt.
- Aggressive but logical: cross-signal alignment ≥ buzz alone.
- Be honest about uncertainty. If signals conflict, say so. Don't paper over disagreement.
- Tax drag (~35% STCG for the user) means every score must be conviction-worthy, not noise-following.
- Watch for: low-volume buzz (likely noise), overbought RSI (mean-reversion risk), macro-regime mismatch.

Output rules:
- `unified_score` is 0–100. 50 = neutral. Reserve 80+ for genuine cross-signal alignment.
- `factor_breakdown` must include all 5 factors (use 50 if a scout was degraded or had no data).
- `risk_flags` are short codes: "overbought_rsi", "low_buzz", "macro_mismatch", "thin_volume", "post_earnings_chase", "sentiment_only", "degraded_signal".
- `primary_thesis` is one tight sentence — the WHY.
- `narrative` is ≤3 sentences explaining the score, including any conflicts.
- Themes are 2–4 cross-cutting observations (e.g., "semi capex spend", "energy oversold bounce")."""


class SynthesisAgent(BaseAgent):
    name = "synthesis"
    model = MODEL_OPUS
    max_tokens = 4096

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def user_prompt(self, input_data: dict) -> str:
        return (
            "Scout briefs for this Monday's decision. Synthesize into a ranked scorecard.\n\n"
            "```json\n"
            + json.dumps(input_data, indent=2, default=str)
            + "\n```\n\n"
            "Rank by `unified_score`. Include every ticker present in at least one scout."
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
                                },
                                "required": ["sentiment", "earnings", "technical", "macro_fit", "influencer"],
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
