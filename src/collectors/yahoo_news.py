"""Yahoo per-ticker news via yfinance.

`yfinance.Ticker(t).news` returns a list of recent headlines per ticker —
already structured (title, publisher, link, providerPublishTime). Free,
no extra dependency. Perfect complement to the broad RSS feeds because it's
ticker-targeted instead of relying on title pattern matching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

from ..utils import to_yfinance

logger = logging.getLogger(__name__)


@dataclass
class YahooNewsItem:
    ticker: str
    title: str
    link: str
    publisher: str
    published_at: Optional[datetime]
    summary: str = ""


def _parse_item(ticker: str, raw: dict) -> Optional[YahooNewsItem]:
    # yfinance changed its news schema in 2024; supports both legacy and current
    title = raw.get("title") or (raw.get("content") or {}).get("title")
    link = raw.get("link") or (
        (raw.get("content") or {}).get("canonicalUrl") or {}
    ).get("url")
    publisher = raw.get("publisher") or (
        (raw.get("content") or {}).get("provider") or {}
    ).get("displayName") or ""
    summary = raw.get("summary") or (raw.get("content") or {}).get("summary") or ""

    ts_raw = raw.get("providerPublishTime") or (raw.get("content") or {}).get("pubDate")
    published_at: Optional[datetime] = None
    if isinstance(ts_raw, (int, float)):
        try:
            published_at = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
        except (OSError, ValueError):
            pass
    elif isinstance(ts_raw, str):
        try:
            published_at = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            pass

    if not title:
        return None
    return YahooNewsItem(
        ticker=ticker.upper(),
        title=title,
        link=link or "",
        publisher=publisher,
        published_at=published_at,
        summary=str(summary)[:500],
    )


def get_ticker_news(ticker: str, limit: int = 10) -> list[YahooNewsItem]:
    """Recent news for one ticker. Returns [] on any failure."""
    try:
        items = yf.Ticker(to_yfinance(ticker)).news or []
    except Exception as exc:
        logger.warning("yahoo news fetch failed for %s: %s", ticker, exc)
        return []

    out: list[YahooNewsItem] = []
    for raw in items[:limit]:
        item = _parse_item(ticker, raw)
        if item is not None:
            out.append(item)
    return out


def get_news_multi(tickers: list[str], limit_per: int = 10) -> dict[str, list[YahooNewsItem]]:
    """Fetch news for multiple tickers. Tickers with no news map to []."""
    return {t.upper(): get_ticker_news(t, limit=limit_per) for t in tickers}
