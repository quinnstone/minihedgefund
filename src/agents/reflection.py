"""Reflection agent — compound learning from prior weeks.

Runs FIRST each Monday, before any scouting. Reads recent decisions + their
outcomes and emits structured lessons that feed into this week's PM context.

This is where the system gets smarter over time. Without it, every week is
independent and we miss the patterns. With it, each cycle's mistakes refine
the next cycle's prompt.
"""

from __future__ import annotations

import json

from .base import MODEL_OPUS, BaseAgent


SYSTEM_PROMPT = """You are the **Reflection Agent** — the system's memory and learning loop.

Your job: read the last several weeks of PM decisions and their realized outcomes,
identify what worked and what didn't, and emit structured lessons that get fed
into this Monday's PM decision context.

# Inputs you will receive
- Last N weeks of decisions (actions taken, target weights, theses, conviction)
- Realized outcomes (per-position weekly returns, vs SPY, hit/miss vs thesis)
- Per-influencer hit rates (cumulative)
- Factor hit rates (sentiment, earnings, technical, macro, influencer)
- Heuristic-vs-LLM divergence record (when PM overrode, who was right)

# What to surface
1. **What worked**: 2–4 observations on signals that produced wins. Be specific —
   "high cross-signal alignment names returned +X%" not "the system did fine."
2. **What missed**: 2–4 observations on losers. Especially: where did conviction
   exceed warranted, where did we chase buzz, where did macro/tax flags get
   under-weighted.
3. **Factor weight proposals**: only propose adjustments when the evidence is
   clear (≥4 weeks of consistent signal). Otherwise leave empty.
4. **Influencer credibility deltas**: for handles whose calls preceded gains,
   nudge up. For handles whose calls preceded losses, nudge down.
5. **Lessons for PM**: 3–6 bullet points the PM should read this week. Each is
   actionable and references specific signals — "Don't size high when sentiment
   is high but technical is overbought; the last 3 cases lost an average of X%."
6. **Watch for**: 1–3 patterns or risks the PM should explicitly check for this week.

# Tone
Direct. Specific. No throat-clearing. Treat the PM as a colleague who already
knows the methodology — tell them what to recalibrate, with evidence.

# When data is sparse
If there are fewer than 2 weeks of history, output minimal lessons. Don't
fabricate patterns from noise. It is FINE to say "insufficient data for [X]."""


class ReflectionAgent(BaseAgent):
    name = "reflection"
    model = MODEL_OPUS
    max_tokens = 3072

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def user_prompt(self, input_data: dict) -> str:
        return (
            "Reflect on prior weeks before this Monday's PM decision.\n\n"
            "```json\n"
            + json.dumps(input_data, indent=2, default=str)
            + "\n```"
        )

    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "weeks_evaluated": {"type": "integer", "minimum": 0},
                "summary": {
                    "type": "string",
                    "description": "1–2 sentences on overall recent performance.",
                },
                "what_worked": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 4,
                },
                "what_missed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 4,
                },
                "factor_weight_proposals": {
                    "type": "object",
                    "description": "Optional. Only propose when evidence is clear (≥4 weeks consistent).",
                    "properties": {
                        "sentiment": {"type": "number"},
                        "earnings": {"type": "number"},
                        "technical": {"type": "number"},
                        "macro_fit": {"type": "number"},
                        "influencer": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
                "influencer_credibility_deltas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "handle": {"type": "string"},
                            "delta": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                            "reason": {"type": "string"},
                        },
                        "required": ["handle", "delta", "reason"],
                        "additionalProperties": False,
                    },
                },
                "lessons_for_pm": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 6,
                    "items": {"type": "string"},
                    "description": "Actionable bullets fed into the PM's prompt this week.",
                },
                "watch_for": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
            },
            "required": ["weeks_evaluated", "summary", "what_worked", "what_missed", "lessons_for_pm", "watch_for"],
            "additionalProperties": False,
        }
