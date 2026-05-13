"""PM agent — the only agent with decision authority.

Reads: portfolio state, synthesis scorecard, risk brief, tax brief, reflection
lessons. Emits: per-ticker trade actions with target weights, theses, and
explicit overrides where the PM diverges from the heuristic. Every override is
logged so we can audit whether the LLM layer adds alpha over time.
"""

from __future__ import annotations

import json

from .base import MODEL_OPUS, BaseAgent


SYSTEM_PROMPT = """You are the **Portfolio Manager** of an AI-driven hedge fund managing $10,000 USD (long-only).

# Identity
Aggressive but disciplined. You take conviction-sized positions. You don't chase.
You cut losers. You don't anchor on cost basis. Cash is a position — sometimes the
right call is to raise it. Sole objective: maximize **after-tax** return.

# Inputs you will receive
- Current portfolio state (cash, positions, lots with cost basis + acquisition dates)
- Synthesis scorecard (ranked candidates with unified_score + factor breakdown)
- Risk brief (per-candidate vol/liquidity/sector + portfolio concentration)
- Tax brief (wash-sale blocks, LTCG-proximity flags, TLH candidates, year-end window)
- Reflection lessons (compound learning from prior weeks — read these carefully)

# Decision rules
1. **Sentiment is the dominant signal**, with technical + earnings as confirmation,
   macro as tilt. A high sentiment score with NO technical/earnings backing is "buzz only" — size small.
2. **Position sizing**: conviction × inverse-vol, capped at:
   - 20% single name (10% if risk flagged "high_vol", 5% if "thin_liquidity")
   - 40% single sector
   - ≥5 positions when fully deployed (don't concentrate to 2-3 names)
3. **Cash range**: 0%–100%. Default operating range 10–30%. Go defensive (>50% cash)
   only on explicit macro contraction + cross-signal weakness.
4. **Tax discipline**:
   - **Never** open a position in a wash-sale-blocked ticker. Period.
   - For positions with LTCG-proximity flags: holding ~35 more days saves ~9pp of
     tax. Only override if cross-signal weakness is severe — and log the reason.
   - In year-end TLH window: surface and seriously consider TLH candidates.
5. **Override authority**: you may override the heuristic ranking. Every override
   must be captured in `overrides` with a reason. We audit these.
6. **Sells are decisive**: TRIM at minor weakness, CLOSE on thesis break or stop.
   Don't average down a falling thesis.

# Action vocabulary (one per ticker decision)
- `OPEN`     — new position; specify `target_weight_pct` of AUM
- `ADD`      — increase existing; specify `additional_weight_pct` of AUM
- `HOLD`     — keep current; brief thesis on WHY (especially if cross-signals weakened)
- `TRIM`     — reduce; specify `trim_pct_of_position` (e.g., 50 = sell half)
- `CLOSE`    — exit entirely
- `NONE`     — no action this week on this candidate

# Output
Be concise but complete. Theses are 1 sentence each — the WHY, not the WHAT.
`weekly_thesis` is the overall theme of the week's deployment.
`narrative` is 2–4 sentences explaining the portfolio-level decisions.
Conviction is "low" | "medium" | "high". Reserve "high" for cross-signal alignment."""


class PMAgent(BaseAgent):
    name = "pm"
    model = MODEL_OPUS
    max_tokens = 4096

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def user_prompt(self, input_data: dict) -> str:
        return (
            "Decision week. Issue trade actions for this Monday.\n\n"
            "```json\n"
            + json.dumps(input_data, indent=2, default=str)
            + "\n```\n\n"
            "Consider EVERY candidate in the scorecard plus EVERY current position. "
            "Output one decision per ticker (NONE is valid). Respect tax brief "
            "(wash sales = hard block) and risk caps."
        )

    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "weekly_thesis": {
                    "type": "string",
                    "description": "One-sentence theme for the week's portfolio decisions.",
                },
                "narrative": {
                    "type": "string",
                    "description": "2–4 sentences explaining the portfolio-level call.",
                },
                "decisions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["OPEN", "ADD", "HOLD", "TRIM", "CLOSE", "NONE"],
                            },
                            "target_weight_pct": {
                                "type": ["number", "null"],
                                "description": "For OPEN — target % of AUM (0-100). Null for non-OPEN.",
                            },
                            "additional_weight_pct": {
                                "type": ["number", "null"],
                                "description": "For ADD — additional % of AUM. Null for non-ADD.",
                            },
                            "trim_pct_of_position": {
                                "type": ["number", "null"],
                                "description": "For TRIM — % of current shares to sell (0-100). Null for non-TRIM.",
                            },
                            "thesis": {"type": "string"},
                            "conviction": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "expected_horizon_weeks": {
                                "type": ["integer", "null"],
                                "minimum": 1,
                                "maximum": 52,
                            },
                        },
                        "required": ["ticker", "action", "thesis", "conviction"],
                        "additionalProperties": False,
                    },
                },
                "target_cash_pct": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Intended cash % after this week's actions.",
                },
                "overrides": {
                    "type": "array",
                    "description": "Cases where PM diverged from heuristic. Each must have a reason.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "heuristic_suggested": {"type": "string"},
                            "pm_chose": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["ticker", "heuristic_suggested", "pm_chose", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["weekly_thesis", "narrative", "decisions", "target_cash_pct", "overrides"],
            "additionalProperties": False,
        }
