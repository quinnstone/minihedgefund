"""Mechanical stop-loss + trailing-stop discipline.

Added 2026-07-20 in response to Path A findings:
  - AVGO closed at -14.5%, SMCI -12.9%, NVDA -10.1%, MSFT -11.8% — losers
    ridden well past a reasonable cut point
  - MU +33.4% closed after only 35 days — winner locked in early with no
    trailing-stop discipline to let it run further

No stop-loss logic existed anywhere; the PM's "cut losers" was purely
qualitative. This module adds:

  Hard stop: any position ≥10% below its cost basis is a MUST_CLOSE
             candidate flagged to the PM. The PM may override but must
             document the reason (goes into `overrides`).

  Trailing stop: any position that has EVER hit +10% from cost basis and
                 subsequently dropped 5% from its post-entry peak is a
                 MUST_CLOSE candidate. Locks in >5% gain on any name that
                 ran and reversed.

Peak price is queried from yfinance daily-close history since acquisition
date (~1 API call per open position per cycle). No new state to persist.

`must_close` is added to the risk_brief; the PM prompt is updated to
treat these as strong recommendations, closable only with logged reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import yfinance as yf

from ..portfolio.state import PortfolioState
from ..utils import to_yfinance

logger = logging.getLogger(__name__)

HARD_STOP_LOSS_PCT = -0.10   # -10% from cost basis
TRAILING_STOP_TRIGGER = 0.10  # position must have hit +10% at some point
TRAILING_STOP_DROP = 0.05    # then close if it drops 5% from peak


@dataclass
class StopSignal:
    ticker: str
    kind: str                        # "hard_stop" | "trailing_stop"
    cost_basis: float
    current_price: float
    peak_price: float                # peak since acquisition
    pnl_pct_from_cost: float         # negative for hard stop
    pnl_pct_from_peak: Optional[float]  # only set for trailing stop
    reason: str                      # human-readable

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "kind": self.kind,
            "cost_basis": round(self.cost_basis, 4),
            "current_price": round(self.current_price, 4),
            "peak_price": round(self.peak_price, 4),
            "pnl_pct_from_cost": round(self.pnl_pct_from_cost, 6),
            "pnl_pct_from_peak": round(self.pnl_pct_from_peak, 6) if self.pnl_pct_from_peak is not None else None,
            "reason": self.reason,
        }


def _fetch_peak_since(ticker: str, since: date) -> Optional[float]:
    """Max daily close for `ticker` between `since` and today.

    Uses adjusted close so splits/dividends don't create false peaks.
    Returns None on any yfinance failure; caller treats as "no peak known"
    and defaults trailing-stop check to no-op.
    """
    try:
        end = (date.today() + timedelta(days=1)).isoformat()
        hist = yf.Ticker(to_yfinance(ticker)).history(
            start=since.isoformat(),
            end=end,
            auto_adjust=True,
        )
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].max())
    except Exception as exc:
        logger.warning("peak-price fetch failed for %s since %s: %s", ticker, since, exc)
        return None


def evaluate_stops(
    portfolio: PortfolioState,
    price_map: dict[str, float],
    today: Optional[date] = None,
) -> list[StopSignal]:
    """Return every open position that has triggered a hard or trailing stop.

    Hard stop is checked first (cheaper — no peak fetch needed). Trailing
    stop only triggers if hard stop didn't already fire.
    """
    today = today or date.today()
    signals: list[StopSignal] = []

    for ticker, position in portfolio.positions.items():
        current = price_map.get(ticker)
        if current is None or position.total_shares <= 0:
            continue

        avg_cost = position.avg_cost_per_share
        if avg_cost <= 0:
            continue

        pnl_pct = (current - avg_cost) / avg_cost

        # Hard stop first
        if pnl_pct <= HARD_STOP_LOSS_PCT:
            signals.append(StopSignal(
                ticker=ticker,
                kind="hard_stop",
                cost_basis=avg_cost,
                current_price=current,
                peak_price=current,  # not applicable; use current
                pnl_pct_from_cost=pnl_pct,
                pnl_pct_from_peak=None,
                reason=f"hit hard stop: {pnl_pct * 100:.1f}% below cost basis ${avg_cost:.2f}",
            ))
            continue

        # Trailing stop: need peak since first acquisition
        first_acq = position.oldest_lot_date()
        if first_acq is None:
            continue
        peak = _fetch_peak_since(ticker, first_acq)
        if peak is None or peak <= 0:
            continue

        # Did the position ever hit the trailing-stop trigger?
        peak_gain_pct = (peak - avg_cost) / avg_cost
        if peak_gain_pct < TRAILING_STOP_TRIGGER:
            continue

        drop_pct = (current - peak) / peak
        if drop_pct <= -TRAILING_STOP_DROP:
            signals.append(StopSignal(
                ticker=ticker,
                kind="trailing_stop",
                cost_basis=avg_cost,
                current_price=current,
                peak_price=peak,
                pnl_pct_from_cost=pnl_pct,
                pnl_pct_from_peak=drop_pct,
                reason=(
                    f"trailing stop: peaked at ${peak:.2f} (+{peak_gain_pct * 100:.1f}%), "
                    f"now ${current:.2f} ({drop_pct * 100:.1f}% from peak). "
                    f"Locks in {pnl_pct * 100:+.1f}% gain."
                ),
            ))

    return signals


def stops_summary_for_brief(signals: list[StopSignal]) -> dict:
    """Compact form suitable for embedding in risk_brief."""
    return {
        "must_close": [s.to_dict() for s in signals],
        "count": len(signals),
        "hard_stops": sum(1 for s in signals if s.kind == "hard_stop"),
        "trailing_stops": sum(1 for s in signals if s.kind == "trailing_stop"),
    }
