"""RSS news collectors — CNBC, MarketWatch, Seeking Alpha.

All three share an RSS-parsing shape. They differ only in feed URLs and a
display-name slug. Each `fetch()` returns a flat list of NewsItem with
title, link, published_at, source — ready for ticker extraction by the news
scout.

Reuters was deliberately omitted: their public RSS was shut down in 2023
and no stable replacement exists. See project_future_roadmap.md for the
restoration plan if a workable feed appears.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT = 10


@dataclass
class NewsItem:
    title: str
    link: str
    published_at: Optional[datetime]
    source: str
    summary: str = ""
    tickers: list[str] = None  # populated downstream by the news scout

    def __post_init__(self):
        if self.tickers is None:
            self.tickers = []


# Tickers in news headlines tend to be UPPERCASE 1-5 letters, possibly with $.
# Same pattern as the Reddit collector, but news headlines are shorter and
# noisier — we re-use the same exclusion set there to filter common words.
TICKER_PATTERN = re.compile(r'(?:^|(?<=\s))\$?([A-Z]{2,5})(?=\s|$|[.,;:!?)])')


class _BaseRSSCollector:
    SOURCE: str = "unknown"
    URLS: list[str] = []

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        # Many feeds prefer browser-like UAs to avoid 403/redirects
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; MiniHedgeFund/1.0)",
        })

    def fetch(self, limit_per_feed: int = 30) -> list[NewsItem]:
        items: list[NewsItem] = []
        for url in self.URLS:
            items.extend(self._fetch_one(url, limit_per_feed))
        # Dedupe across feeds within one source by lowercased title
        seen: set[str] = set()
        out: list[NewsItem] = []
        for item in items:
            key = item.title.lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _fetch_one(self, url: str, limit: int) -> list[NewsItem]:
        try:
            resp = self._session.get(url, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("%s RSS fetch failed for %s: %s", self.SOURCE, url, exc)
            return []
        return self._parse(resp.text, limit)

    def _parse(self, text: str, limit: int) -> list[NewsItem]:
        try:
            soup = BeautifulSoup(text, "lxml-xml")
        except Exception:
            soup = BeautifulSoup(text, "xml")

        # Support both Atom (<entry>) and RSS 2.0 (<item>)
        nodes = soup.find_all("entry") or soup.find_all("item")
        out: list[NewsItem] = []
        for node in nodes[:limit]:
            title_el = node.find("title")
            link_el = node.find("link")
            desc_el = node.find("description") or node.find("summary")
            date_el = (
                node.find("pubDate") or node.find("published") or node.find("updated")
            )

            title = title_el.get_text(strip=True) if title_el else ""
            if link_el is None:
                link = ""
            elif link_el.has_attr("href"):
                link = link_el["href"]
            else:
                link = link_el.get_text(strip=True)

            summary = desc_el.get_text(strip=True) if desc_el else ""
            # Strip embedded HTML out of summaries
            if summary and "<" in summary:
                summary = BeautifulSoup(summary, "lxml").get_text(separator=" ", strip=True)
            summary = summary[:500]

            published_at = None
            if date_el is not None:
                raw = date_el.get_text(strip=True)
                for fmt in (
                    "%a, %d %b %Y %H:%M:%S %z",
                    "%a, %d %b %Y %H:%M:%S GMT",
                ):
                    try:
                        published_at = datetime.strptime(raw, fmt)
                        break
                    except ValueError:
                        continue
                if published_at is None:
                    try:
                        published_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError:
                        published_at = None

            if not title:
                continue
            out.append(NewsItem(
                title=title,
                link=link,
                published_at=published_at,
                source=self.SOURCE,
                summary=summary,
            ))
        return out


class CnbcCollector(_BaseRSSCollector):
    SOURCE = "cnbc"
    URLS = [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # top news
        "https://www.cnbc.com/id/15839069/device/rss/rss.html",   # business
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",   # markets
    ]


class MarketWatchCollector(_BaseRSSCollector):
    SOURCE = "marketwatch"
    URLS = [
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://feeds.marketwatch.com/marketwatch/marketpulse/",
        "https://feeds.marketwatch.com/marketwatch/realtimeheadlines/",
    ]


class SeekingAlphaCollector(_BaseRSSCollector):
    SOURCE = "seeking_alpha"
    URLS = [
        "https://seekingalpha.com/market_currents.xml",   # market news / breaking
        "https://seekingalpha.com/feed.xml",              # main editorial feed
    ]


def extract_tickers(text: str, excluded: set[str]) -> list[str]:
    """Pull likely tickers out of a headline + summary.

    Reuses the Reddit collector's excluded-words list to filter false positives
    like THE / AND / CEO / etc.
    """
    found = TICKER_PATTERN.findall(text)
    seen: set[str] = set()
    out: list[str] = []
    for t in found:
        if t in excluded:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out
