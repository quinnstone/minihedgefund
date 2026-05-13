"""Tax constraint module — deterministic wrapper over the tax engine.

Evaluates a candidate set against the current portfolio + recent closed lots
and surfaces: wash-sale blocks, LTCG-proximity holds (don't sell yet), and
TLH opportunities (consider selling at a loss for the tax offset).

Not an LLM agent. The PM reads this as structured input alongside risk view.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from ..portfolio.state import ClosedLot, PortfolioState
from ..portfolio.tax import TaxEngine


def build_tax_brief(
    candidate_tickers: list[str],
    portfolio: PortfolioState,
    price_map: dict[str, float],
    recent_closed_lots: list[ClosedLot],
    as_of: Optional[date] = None,
    tax_engine: Optional[TaxEngine] = None,
) -> dict:
    """Build a tax brief for the PM. Always returns a populated dict."""
    if as_of is None:
        as_of = date.today()
    if tax_engine is None:
        tax_engine = TaxEngine()

    wash_sale_blocks: dict[str, dict] = {}
    for ticker in candidate_tickers:
        check = tax_engine.check_wash_sale(ticker, as_of, recent_closed_lots)
        if check.is_wash_sale_risk:
            wash_sale_blocks[ticker] = {
                "blocking_close_date": check.blocking_close_date.isoformat() if check.blocking_close_date else None,
                "blocking_realized_loss": check.blocking_realized_loss,
                "days_until_clear": check.days_until_clear,
            }

    ltcg_proximity: list[dict] = []
    for ticker, position in portfolio.positions.items():
        price = price_map.get(ticker)
        if price is None:
            continue
        for flag in tax_engine.ltcg_proximity_flags(position, as_of, price):
            ltcg_proximity.append({
                "ticker": ticker,
                "lot_id": flag.lot_id,
                "shares": flag.shares,
                "days_held": flag.days_held,
                "days_to_long_term": flag.days_to_long_term,
                "unrealized_pnl": flag.unrealized_pnl,
                "extra_tax_if_sold_now": flag.extra_tax_if_sold_now,
            })

    tlh = tax_engine.tlh_candidates(portfolio.positions, price_map, as_of, min_loss=25.0)
    tlh_brief = [
        {
            "ticker": c.ticker,
            "shares": c.shares,
            "unrealized_loss": c.unrealized_loss,
            "estimated_tax_savings": c.estimated_tax_savings,
            "is_long_term": c.is_long_term,
        }
        for c in tlh
    ]

    return {
        "as_of": as_of.isoformat(),
        "tax_brackets": {
            "stcg_rate": round(tax_engine.brackets.stcg_rate, 4),
            "ltcg_rate": round(tax_engine.brackets.ltcg_rate, 4),
        },
        "wash_sale_blocks": wash_sale_blocks,
        "ltcg_proximity": ltcg_proximity,
        "tlh_candidates": tlh_brief,
        "year_end_tlh_window_open": tax_engine.in_year_end_tlh_window(as_of),
    }
