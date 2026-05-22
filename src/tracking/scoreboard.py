"""Scoreboard — aggregate stats with SPY benchmark + after-tax view.

Updated each Monday after marking. The scoreboard is what shows in Discord
and what the reflection agent reads for compound learning.
"""

from __future__ import annotations

from typing import Optional


def update_scoreboard(
    prior: dict,
    new_week: dict,
    initial_capital: float,
    inception_date_iso: Optional[str],
    trades_count: int,
    realized_gains: float,
    realized_losses: float,
    estimated_tax_owed: float,
) -> dict:
    """Append a new weekly outcome and recompute aggregates.

    `new_week` shape: {week_of, return_pct, return_usd, spy_pct, alpha, aum}
    """
    weekly_returns = list(prior.get("weekly_returns") or [])

    # Don't double-count the same week if reprocessed
    week_of = new_week.get("week_of")
    weekly_returns = [w for w in weekly_returns if w.get("week_of") != week_of]
    weekly_returns.append(new_week)
    weekly_returns.sort(key=lambda w: w.get("week_of", ""))

    weeks = len(weekly_returns)
    current_aum = new_week.get("aum") or prior.get("current_aum", initial_capital)

    # Performance baseline: the AUM AT END of the first week, NOT the
    # pre-deployment cash. PMs report performance from inception of the book,
    # not from "before I funded the account." Entry slippage on week 1's
    # deployment is a one-time entry cost, not "performance" — surfacing it
    # as a -0.02% loss on day zero is misleading.
    #
    # Retroactive fix: scoreboards that pre-date this field have no
    # deployment_aum. Recover it from the first recorded week's AUM (always
    # available because we just appended new_week if nothing else was there).
    deployment_aum = prior.get("deployment_aum")
    if deployment_aum is None or deployment_aum <= 0:
        first_week = weekly_returns[0] if weekly_returns else None
        if first_week and first_week.get("aum"):
            deployment_aum = float(first_week["aum"])
        else:
            deployment_aum = current_aum

    cum_pct = (current_aum - deployment_aum) / deployment_aum if deployment_aum > 0 else 0.0
    cum_usd = current_aum - deployment_aum

    # Slippage / entry cost = the difference between what was originally
    # funded and what got deployed after slippage + rounding. Tracked
    # separately so it's visible without polluting "performance."
    deployment_cost_usd = deployment_aum - initial_capital
    deployment_cost_pct = deployment_cost_usd / initial_capital if initial_capital > 0 else 0.0

    wins = sum(1 for w in weekly_returns if (w.get("return_pct") or 0) > 0)
    win_rate = wins / weeks if weeks > 0 else 0.0

    # SPY cumulative — compound the weekly SPY returns
    spy_cum = 1.0
    for w in weekly_returns:
        spy_pct = w.get("spy_pct")
        if spy_pct is not None:
            spy_cum *= (1.0 + spy_pct)
    spy_cum_pct = spy_cum - 1.0

    after_tax_cum = cum_pct - (estimated_tax_owed / deployment_aum if deployment_aum > 0 else 0)

    best = max(weekly_returns, key=lambda w: w.get("return_pct") or 0, default=None)
    worst = min(weekly_returns, key=lambda w: w.get("return_pct") or 0, default=None)

    return {
        "inception_date": inception_date_iso,
        "initial_capital": initial_capital,
        "deployment_aum": round(deployment_aum, 4),
        "deployment_cost_usd": round(deployment_cost_usd, 4),
        "deployment_cost_pct": round(deployment_cost_pct, 6),
        "weeks_tracked": weeks,
        "weekly_returns": weekly_returns,
        "cumulative_return_pct": round(cum_pct, 6),
        "cumulative_return_usd": round(cum_usd, 4),
        "current_aum": round(current_aum, 4),
        "weekly_win_rate": round(win_rate, 4),
        "spy_cumulative_pct": round(spy_cum_pct, 6),
        "cumulative_alpha_pct": round(cum_pct - spy_cum_pct, 6),
        "trades_count": trades_count,
        "total_realized_gains": round(realized_gains, 4),
        "total_realized_losses": round(realized_losses, 4),
        "estimated_tax_owed": round(estimated_tax_owed, 4),
        "after_tax_cumulative_return_pct": round(after_tax_cum, 6),
        "best_week": {"week_of": best.get("week_of"), "return_pct": best.get("return_pct")} if best else None,
        "worst_week": {"week_of": worst.get("week_of"), "return_pct": worst.get("return_pct")} if worst else None,
    }


def compute_realized_tax_totals(trades: list[dict], stcg_rate: float, ltcg_rate: float) -> tuple[float, float, float]:
    """Sum realized gains/losses + estimated tax across all sell trades."""
    gains = 0.0
    losses = 0.0
    tax = 0.0
    for t in trades:
        if t.get("kind") != "sell":
            continue
        pnl = float(t.get("realized_pnl") or 0)
        is_lt = bool(t.get("is_long_term", False))
        if pnl > 0:
            gains += pnl
            tax += pnl * (ltcg_rate if is_lt else stcg_rate)
        elif pnl < 0:
            losses += pnl
    return gains, losses, tax
