"""Analyst collector — sell-side ratings, upgrades/downgrades, price targets.

Pulled from yfinance. Free, no auth, but the underlying scrape can be patchy
on individual names. All methods return safe empty defaults on failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf

from ..utils import to_yfinance

logger = logging.getLogger(__name__)


@dataclass
class AnalystRating:
    firm: str
    rating: str             # "Buy" / "Hold" / "Sell" / "Outperform" / etc.
    date: datetime


@dataclass
class RatingChange:
    firm: str
    from_rating: Optional[str]
    to_rating: str
    date: datetime
    direction: str          # "upgrade" | "downgrade" | "init" | "reiterate"


@dataclass
class PriceTargets:
    ticker: str
    target_mean: Optional[float] = None
    target_median: Optional[float] = None
    target_high: Optional[float] = None
    target_low: Optional[float] = None
    analyst_count: int = 0
    recommendation: Optional[str] = None    # yfinance's roll-up label
    current_price: Optional[float] = None


@dataclass
class AnalystSummary:
    """All analyst signals for a ticker in one bundle."""

    ticker: str
    recent_changes: list[RatingChange] = field(default_factory=list)
    price_targets: Optional[PriceTargets] = None

    @property
    def net_change_signal(self) -> int:
        """+upgrades - downgrades over the lookback window. Initiations excluded."""
        net = 0
        for c in self.recent_changes:
            if c.direction == "upgrade":
                net += 1
            elif c.direction == "downgrade":
                net -= 1
        return net


_UP = {"upgrade", "init - buy", "init - outperform", "raised", "reiterated buy"}
_DOWN = {"downgrade", "init - sell", "init - underperform", "lowered"}


def _classify_change(action: str, from_rating: Optional[str], to_rating: str) -> str:
    a = (action or "").lower().strip()
    if a in {"up", "upgrade"}:
        return "upgrade"
    if a in {"down", "downgrade"}:
        return "downgrade"
    if a in {"init", "main"}:
        return "init"
    if a in {"reit", "reiterate"}:
        return "reiterate"
    # Heuristic fallback: compare rating strings if obvious
    bull = {"buy", "strong buy", "outperform", "overweight"}
    bear = {"sell", "strong sell", "underperform", "underweight"}
    f, t = (from_rating or "").lower(), (to_rating or "").lower()
    if t in bull and f in bear:
        return "upgrade"
    if t in bear and f in bull:
        return "downgrade"
    return "reiterate"


class AnalystCollector:
    def __init__(self):
        pass

    def get_recent_changes(self, ticker: str, days: int = 14) -> list[RatingChange]:
        """Recent upgrades/downgrades. Returns [] on any fetch failure."""
        try:
            t = yf.Ticker(to_yfinance(ticker))
            df = t.upgrades_downgrades
        except Exception as exc:
            logger.warning("yfinance upgrades_downgrades failed for %s: %s", ticker, exc)
            return []

        if df is None or df.empty:
            return []

        cutoff = datetime.utcnow() - timedelta(days=days)
        out: list[RatingChange] = []
        try:
            df = df.reset_index()  # 'GradeDate' becomes a column
            for _, row in df.iterrows():
                d = row.get("GradeDate")
                date_val = d.to_pydatetime() if hasattr(d, "to_pydatetime") else None
                if date_val is None or date_val < cutoff:
                    continue
                from_r = row.get("FromGrade") or None
                to_r = row.get("ToGrade") or ""
                action = row.get("Action") or ""
                out.append(RatingChange(
                    firm=row.get("Firm") or "",
                    from_rating=str(from_r) if from_r else None,
                    to_rating=str(to_r),
                    date=date_val,
                    direction=_classify_change(action, from_r, to_r),
                ))
        except Exception as exc:
            logger.warning("parsing analyst changes for %s failed: %s", ticker, exc)

        return out

    def get_price_targets(self, ticker: str) -> Optional[PriceTargets]:
        try:
            info = yf.Ticker(to_yfinance(ticker)).info or {}
        except Exception as exc:
            logger.warning("yfinance info failed for %s: %s", ticker, exc)
            return None

        return PriceTargets(
            ticker=ticker.upper(),
            target_mean=info.get("targetMeanPrice"),
            target_median=info.get("targetMedianPrice"),
            target_high=info.get("targetHighPrice"),
            target_low=info.get("targetLowPrice"),
            analyst_count=int(info.get("numberOfAnalystOpinions") or 0),
            recommendation=info.get("recommendationKey"),
            current_price=info.get("currentPrice") or info.get("regularMarketPrice"),
        )

    def get_summary(self, ticker: str, lookback_days: int = 14) -> AnalystSummary:
        return AnalystSummary(
            ticker=ticker.upper(),
            recent_changes=self.get_recent_changes(ticker, days=lookback_days),
            price_targets=self.get_price_targets(ticker),
        )

    def get_multiple(self, tickers: list[str], lookback_days: int = 14) -> dict[str, AnalystSummary]:
        return {t.upper(): self.get_summary(t, lookback_days=lookback_days) for t in tickers}
