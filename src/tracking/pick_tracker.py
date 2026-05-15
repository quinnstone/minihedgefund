"""Pick scoreboard — the signal-quality ledger.

Every OPEN/ADD recommendation is recorded as a Pick. The scoreboard tracks:
  - rec_market_price: market mid at PM-call time (un-slipped)
  - executed_fill_price: what we actually paid (post-slippage, post-rounding)
  - lifetime_return_pct: (current - rec_market) / rec_market  (the "signal P&L")
  - conviction tier, target_weight, thesis, factor_breakdown

Why this is separate from portfolio MTM:
  The portfolio is constrained (sizing caps, available cash, Schwab fractional
  rules). The pick scoreboard is what we would have earned if every
  recommendation had been bought at recommendation time, frictionless.

Two basket views are surfaced:
  - equal_weight_basket_return_pct: average pick return (raw signal quality)
  - weighted_basket_return_pct:    weighted by target_weight (PM intent)

Per-conviction breakdown lets the reflection agent see calibration:
  do HIGH-conviction picks actually win more than MEDIUM?
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .persistence import DATA_DIR

logger = logging.getLogger(__name__)

PICK_SCOREBOARD_FILE = DATA_DIR / "pick_scoreboard.json"

# Returns within ±0.5% are "flat" — pure noise from slippage and rounding
FLAT_BAND = 0.005


@dataclass
class Pick:
    pick_id: str
    ticker: str
    week_of: str                    # ISO date
    action: str                     # "OPEN" or "ADD"
    rec_market_price: float         # market mid at call time, no slippage
    executed_fill_price: float      # what we actually paid
    target_weight_pct: float
    conviction: str                 # "low" / "medium" / "high"
    thesis: str
    factor_breakdown: dict = field(default_factory=dict)
    current_price: float = 0.0
    lifetime_return_pct: float = 0.0
    days_held: int = 0
    status: str = "open"            # "open" or "closed"
    closed_at: Optional[str] = None
    closed_price: Optional[float] = None
    final_return_pct: Optional[float] = None

    @property
    def outcome_label(self) -> str:
        ret = self.final_return_pct if self.final_return_pct is not None else self.lifetime_return_pct
        if ret > FLAT_BAND:
            return "winner"
        if ret < -FLAT_BAND:
            return "loser"
        return "flat"


def _new_pick_id() -> str:
    return uuid.uuid4().hex[:12]


def load_pick_scoreboard() -> dict:
    if not PICK_SCOREBOARD_FILE.exists():
        return _empty_scoreboard()
    try:
        return json.loads(PICK_SCOREBOARD_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("pick_scoreboard.json corrupted; reinitializing")
        return _empty_scoreboard()


def save_pick_scoreboard(sb: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PICK_SCOREBOARD_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sb, indent=2, default=str))
    os.replace(tmp, PICK_SCOREBOARD_FILE)


def _empty_scoreboard() -> dict:
    return {
        "inception_date": None,
        "picks": [],
        "aggregate": _empty_aggregate(),
        "weekly_recaps": [],
    }


def _empty_aggregate() -> dict:
    return {
        "total_picks": 0,
        "open_picks": 0,
        "closed_picks": 0,
        "win_count": 0,
        "loss_count": 0,
        "flat_count": 0,
        "win_rate": 0.0,
        "avg_return_pct": 0.0,
        "avg_winner_return_pct": 0.0,
        "avg_loser_return_pct": 0.0,
        "equal_weight_basket_return_pct": 0.0,
        "weighted_basket_return_pct": 0.0,
        "best_pick": None,
        "worst_pick": None,
        "by_conviction": {},
    }


def record_picks(
    sb: dict,
    today: date,
    pm_decisions: list[dict],
    executed_trades: list[dict],
    market_price_map: dict[str, float],
    ranked_candidates: list[dict],
) -> int:
    """Append new Pick rows for every OPEN/ADD that actually executed.

    Returns the number of new picks recorded.
    """
    if sb.get("inception_date") is None:
        sb["inception_date"] = today.isoformat()

    # Index PM decisions by ticker for fast thesis/conviction lookup
    pm_by_ticker = {(d.get("ticker") or "").upper(): d for d in pm_decisions}
    # Index synthesis ranked candidates by ticker for factor_breakdown
    synth_by_ticker = {(c.get("ticker") or "").upper(): c for c in (ranked_candidates or [])}

    new_count = 0
    for trade in executed_trades:
        if trade.get("kind") != "buy":
            continue
        ticker = (trade.get("ticker") or "").upper()
        decision = pm_by_ticker.get(ticker, {})
        action = decision.get("action", "OPEN")
        if action not in ("OPEN", "ADD"):
            continue   # belt-and-suspenders; trade.kind=buy implies OPEN/ADD

        rec_market = market_price_map.get(ticker, trade.get("price", 0.0))
        synth = synth_by_ticker.get(ticker, {})

        pick = Pick(
            pick_id=_new_pick_id(),
            ticker=ticker,
            week_of=today.isoformat(),
            action=action,
            rec_market_price=float(rec_market),
            executed_fill_price=float(trade.get("price", 0.0)),
            target_weight_pct=float(decision.get("target_weight_pct") or decision.get("additional_weight_pct") or 0.0),
            conviction=str(decision.get("conviction") or "medium"),
            thesis=str(decision.get("thesis") or "")[:500],
            factor_breakdown=dict(synth.get("factor_breakdown") or {}),
            current_price=float(rec_market),
            lifetime_return_pct=0.0,
            days_held=0,
            status="open",
        )
        sb["picks"].append(asdict(pick))
        new_count += 1

    return new_count


def close_picks(
    sb: dict,
    today: date,
    pm_decisions: list[dict],
    executed_trades: list[dict],
    market_price_map: dict[str, float],
) -> int:
    """Mark all open picks of a ticker closed when the PM issued CLOSE.

    TRIM does NOT close picks — the pick continues tracking against the
    remaining shares' performance. Only a full CLOSE retires the picks.
    """
    closed_tickers = {
        (d.get("ticker") or "").upper()
        for d in pm_decisions
        if d.get("action") == "CLOSE"
    }
    # Use actual fill price from the trade if available
    sell_prices: dict[str, float] = {}
    for t in executed_trades:
        if t.get("kind") == "sell":
            ticker = (t.get("ticker") or "").upper()
            sell_prices[ticker] = float(t.get("price") or sell_prices.get(ticker, 0))

    closed_count = 0
    for pick in sb["picks"]:
        if pick["status"] != "open":
            continue
        if pick["ticker"] not in closed_tickers:
            continue
        rec = float(pick.get("rec_market_price") or 0)
        market_close = sell_prices.get(pick["ticker"]) or market_price_map.get(pick["ticker"])
        pick["status"] = "closed"
        pick["closed_at"] = today.isoformat()
        pick["closed_price"] = float(market_close) if market_close is not None else None
        if rec > 0 and market_close:
            pick["final_return_pct"] = (market_close - rec) / rec
            pick["lifetime_return_pct"] = pick["final_return_pct"]
        pick["current_price"] = float(market_close) if market_close is not None else pick.get("current_price")
        closed_count += 1

    return closed_count


def refresh_open_picks(
    sb: dict,
    today: date,
    price_map: dict[str, float],
) -> int:
    """Mark every open pick to market. Returns count of picks refreshed."""
    refreshed = 0
    for pick in sb["picks"]:
        if pick["status"] != "open":
            continue
        ticker = pick["ticker"]
        cur = price_map.get(ticker)
        if cur is None:
            continue
        pick["current_price"] = float(cur)
        rec = float(pick.get("rec_market_price") or 0)
        if rec > 0:
            pick["lifetime_return_pct"] = (cur - rec) / rec
        week_of = pick.get("week_of")
        if week_of:
            try:
                pick["days_held"] = (today - date.fromisoformat(week_of)).days
            except ValueError:
                pass
        refreshed += 1
    return refreshed


def compute_aggregate(sb: dict) -> dict:
    """Recompute aggregate stats from the picks list."""
    picks = sb.get("picks") or []
    agg = _empty_aggregate()
    if not picks:
        return agg

    agg["total_picks"] = len(picks)
    agg["open_picks"] = sum(1 for p in picks if p.get("status") == "open")
    agg["closed_picks"] = sum(1 for p in picks if p.get("status") == "closed")

    returns = [float(p.get("lifetime_return_pct") or 0) for p in picks]
    winners = [r for r in returns if r > FLAT_BAND]
    losers = [r for r in returns if r < -FLAT_BAND]
    flats = [r for r in returns if -FLAT_BAND <= r <= FLAT_BAND]

    agg["win_count"] = len(winners)
    agg["loss_count"] = len(losers)
    agg["flat_count"] = len(flats)
    agg["win_rate"] = len(winners) / len(picks)
    agg["avg_return_pct"] = sum(returns) / len(returns)
    agg["avg_winner_return_pct"] = sum(winners) / len(winners) if winners else 0.0
    agg["avg_loser_return_pct"] = sum(losers) / len(losers) if losers else 0.0

    # Equal-weight basket: simple mean (same as avg_return_pct here, but a
    # separate name keeps the "interpretation A" reading explicit)
    agg["equal_weight_basket_return_pct"] = agg["avg_return_pct"]

    # Weight-aware basket: each pick's return × its target weight, normalized
    total_weight = sum(float(p.get("target_weight_pct") or 0) for p in picks)
    if total_weight > 0:
        agg["weighted_basket_return_pct"] = sum(
            float(p.get("target_weight_pct") or 0) * float(p.get("lifetime_return_pct") or 0)
            for p in picks
        ) / total_weight
    else:
        agg["weighted_basket_return_pct"] = agg["avg_return_pct"]

    # Best/worst pick
    best = max(picks, key=lambda p: float(p.get("lifetime_return_pct") or 0))
    worst = min(picks, key=lambda p: float(p.get("lifetime_return_pct") or 0))
    agg["best_pick"] = {
        "ticker": best["ticker"],
        "week_of": best.get("week_of"),
        "return_pct": round(float(best.get("lifetime_return_pct") or 0), 6),
        "conviction": best.get("conviction"),
    }
    agg["worst_pick"] = {
        "ticker": worst["ticker"],
        "week_of": worst.get("week_of"),
        "return_pct": round(float(worst.get("lifetime_return_pct") or 0), 6),
        "conviction": worst.get("conviction"),
    }

    # By-conviction calibration breakdown
    by_conv: dict[str, dict] = {}
    for p in picks:
        conv = str(p.get("conviction") or "medium")
        r = float(p.get("lifetime_return_pct") or 0)
        bucket = by_conv.setdefault(conv, {"count": 0, "wins": 0, "returns": []})
        bucket["count"] += 1
        bucket["returns"].append(r)
        if r > FLAT_BAND:
            bucket["wins"] += 1
    for conv, b in by_conv.items():
        n = b["count"]
        rs = b["returns"]
        by_conv[conv] = {
            "count": n,
            "win_rate": round(b["wins"] / n, 4) if n else 0.0,
            "avg_return_pct": round(sum(rs) / n, 6) if n else 0.0,
        }
    agg["by_conviction"] = by_conv

    return agg


def update_weekly_recaps(sb: dict, today: date) -> None:
    """Append/update this week's recap entry."""
    week_of = today.isoformat()
    picks_this_week = [p for p in sb.get("picks") or [] if p.get("week_of") == week_of]
    if not picks_this_week:
        return

    recap = {
        "week_of": week_of,
        "picks_count": len(picks_this_week),
        "tickers": [p["ticker"] for p in picks_this_week],
        "avg_return_since_pick": (
            sum(float(p.get("lifetime_return_pct") or 0) for p in picks_this_week)
            / len(picks_this_week)
        ),
    }

    recaps = [r for r in (sb.get("weekly_recaps") or []) if r.get("week_of") != week_of]
    recaps.append(recap)
    recaps.sort(key=lambda r: r.get("week_of", ""))
    sb["weekly_recaps"] = recaps
