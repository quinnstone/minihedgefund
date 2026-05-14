"""Mark-to-market — fetch current prices and snapshot portfolio against SPY.

Runs at the top of every Monday cycle so the scoreboard reflects accurate AUM
before any decisions are made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import yfinance as yf

from ..portfolio.state import PortfolioState
from ..utils import to_yfinance

logger = logging.getLogger(__name__)


SPY_TICKER = "SPY"


@dataclass
class MarkSnapshot:
    as_of: date
    price_map: dict[str, float] = field(default_factory=dict)
    positions: list[dict] = field(default_factory=list)
    cash: float = 0.0
    aum: float = 0.0
    weekly_return_pct: Optional[float] = None
    weekly_return_usd: Optional[float] = None
    spy_weekly_return_pct: Optional[float] = None
    alpha_pct: Optional[float] = None
    prior_aum: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "price_map": self.price_map,
            "positions": self.positions,
            "cash": round(self.cash, 4),
            "aum": round(self.aum, 4),
            "prior_aum": round(self.prior_aum, 4) if self.prior_aum is not None else None,
            "weekly_return_pct": round(self.weekly_return_pct, 6) if self.weekly_return_pct is not None else None,
            "weekly_return_usd": round(self.weekly_return_usd, 4) if self.weekly_return_usd is not None else None,
            "spy_weekly_return_pct": round(self.spy_weekly_return_pct, 6) if self.spy_weekly_return_pct is not None else None,
            "alpha_pct": round(self.alpha_pct, 6) if self.alpha_pct is not None else None,
        }


def _fetch_latest_price(ticker: str) -> Optional[float]:
    try:
        hist = yf.Ticker(to_yfinance(ticker)).history(period="5d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as exc:
        logger.warning("price fetch failed for %s: %s", ticker, exc)
        return None


def _fetch_price_map(tickers: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in tickers:
        p = _fetch_latest_price(t)
        if p is not None:
            out[t] = p
    return out


def _spy_return_over_window(start: date, end: date) -> Optional[float]:
    """SPY total return between two dates (inclusive on both sides)."""
    try:
        hist = yf.Ticker(SPY_TICKER).history(
            start=start.isoformat(),
            end=(end.toordinal() - start.toordinal() < 1 and end.isoformat()) or end.isoformat(),
            auto_adjust=True,
        )
    except Exception:
        return None
    if hist is None or hist.empty or len(hist) < 2:
        return None
    try:
        first = float(hist["Close"].iloc[0])
        last = float(hist["Close"].iloc[-1])
        if first <= 0:
            return None
        return (last - first) / first
    except Exception:
        return None


def mark_portfolio(
    portfolio: PortfolioState,
    prior_marks: Optional[dict] = None,
    today: Optional[date] = None,
) -> MarkSnapshot:
    """Mark current positions to market and compute weekly return vs SPY.

    `prior_marks` should be the most recent mark dict; weekly return is
    computed against its `aum`. SPY is read over the same date window.
    """
    today = today or date.today()
    tickers = sorted(portfolio.positions.keys())
    price_map = _fetch_price_map(tickers + [SPY_TICKER])
    spy_price = price_map.pop(SPY_TICKER, None)

    snap = MarkSnapshot(as_of=today, price_map=price_map, cash=portfolio.cash)

    positions_value = 0.0
    for ticker, pos in portfolio.positions.items():
        price = price_map.get(ticker)
        if price is None:
            continue
        mv = pos.market_value(price)
        positions_value += mv
        snap.positions.append({
            "ticker": ticker,
            "shares": round(pos.total_shares, 6),
            "avg_cost_per_share": round(pos.avg_cost_per_share, 4),
            "current_price": round(price, 4),
            "market_value": round(mv, 4),
            "unrealized_pnl": round(pos.unrealized_pnl(price), 4),
            "unrealized_pnl_pct": round(pos.unrealized_pnl_pct(price), 6),
            "oldest_lot_date": pos.oldest_lot_date().isoformat() if pos.oldest_lot_date() else None,
            "days_held": pos.days_held(today),
            "n_lots": len(pos.lots),
        })

    snap.aum = portfolio.cash + positions_value

    if prior_marks and prior_marks.get("aum"):
        snap.prior_aum = float(prior_marks["aum"])
        if snap.prior_aum > 0:
            snap.weekly_return_pct = (snap.aum - snap.prior_aum) / snap.prior_aum
            snap.weekly_return_usd = snap.aum - snap.prior_aum

        # SPY return over same window
        try:
            prior_date = date.fromisoformat(prior_marks["as_of"])
            snap.spy_weekly_return_pct = _spy_return_over_window(prior_date, today)
            if snap.weekly_return_pct is not None and snap.spy_weekly_return_pct is not None:
                snap.alpha_pct = snap.weekly_return_pct - snap.spy_weekly_return_pct
        except Exception as exc:
            logger.warning("SPY return computation failed: %s", exc)

    return snap
