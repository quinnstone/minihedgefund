"""News scout — aggregates broad RSS feeds + per-ticker Yahoo news + NewsAPI.

Per ticker, returns:
  - headline count across all sources
  - polarity (VADER on titles) — saves a Haiku call
  - distinct source count
  - top 3 recent headlines
  - composite_score 0-100 (50 = neutral)

This is the "fundamental news" channel — complements the retail-sentiment
scout. A name with rising headline count + positive polarity + multiple
publishers is a real news catalyst, distinct from social-media buzz.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from ...collectors.reddit import RedditCollector
from ...collectors.rss_news import (
    CnbcCollector,
    MarketWatchCollector,
    NewsItem,
    SeekingAlphaCollector,
    extract_tickers,
)
from ...collectors.yahoo_news import YahooNewsItem, get_news_multi


_vader: Optional[SentimentIntensityAnalyzer] = None


def _vader_instance() -> SentimentIntensityAnalyzer:
    """Lazy singleton — VADER lexicon load takes ~100ms."""
    global _vader
    if _vader is None:
        _vader = SentimentIntensityAnalyzer()
    return _vader


def _polarity_score(text: str) -> float:
    """VADER compound score in [-1, +1], or 0 on empty input."""
    if not text:
        return 0.0
    return _vader_instance().polarity_scores(text)["compound"]


def _bucket(headline_count: int) -> str:
    if headline_count >= 6:
        return "high"
    if headline_count >= 2:
        return "med"
    return "low" if headline_count > 0 else "none"


def _composite(headline_count: int, polarity: float, distinct_sources: int) -> float:
    """0-100 composite. Volume + polarity + source breadth all push it up."""
    if headline_count == 0:
        return 50.0
    score = 50.0
    score += polarity * 25.0                              # ±25 from polarity
    score += min(15.0, headline_count * 1.5)              # up to +15 from volume
    score += min(10.0, (distinct_sources - 1) * 5.0)      # +5 per extra publisher
    return round(max(0.0, min(100.0, score)), 1)


def run_news_scout(universe: list[str]) -> dict:
    """News brief for the universe."""
    excluded = RedditCollector.EXCLUDED_WORDS

    # ─── Broad-feed pull (one-time across all tickers) ───
    broad_items: list[NewsItem] = []
    degraded_sources: list[str] = []
    for cls in (CnbcCollector, MarketWatchCollector, SeekingAlphaCollector):
        c = cls()
        items = c.fetch(limit_per_feed=30)
        if not items:
            degraded_sources.append(c.SOURCE)
        broad_items.extend(items)

    # Annotate broad items with extracted tickers
    universe_set = {t.upper() for t in universe}
    for item in broad_items:
        text = f"{item.title} {item.summary}"
        item.tickers = [t for t in extract_tickers(text, excluded) if t in universe_set]

    # ─── Per-ticker Yahoo news ───
    yahoo_results = get_news_multi(universe, limit_per=8)

    # ─── Aggregate per ticker ───
    per_ticker_items: dict[str, list[NewsItem]] = defaultdict(list)
    for item in broad_items:
        for ticker in item.tickers:
            per_ticker_items[ticker].append(item)

    candidates = []
    for ticker in universe:
        ticker = ticker.upper()
        broad = per_ticker_items.get(ticker, [])
        yahoo = yahoo_results.get(ticker, [])

        all_titles = [it.title for it in broad] + [yn.title for yn in yahoo]
        all_text = " ".join(all_titles)
        sources = {it.source for it in broad} | {yn.publisher for yn in yahoo if yn.publisher}

        headline_count = len(broad) + len(yahoo)
        polarity = _polarity_score(all_text) if all_text else 0.0
        composite = _composite(headline_count, polarity, len(sources))

        # Top 3 recent headlines for the audit + Discord
        top = []
        for it in broad[:2]:
            top.append({"title": it.title, "source": it.source, "link": it.link})
        for yn in yahoo[:max(0, 3 - len(top))]:
            top.append({"title": yn.title, "source": yn.publisher, "link": yn.link})

        candidates.append({
            "ticker": ticker,
            "headline_count": headline_count,
            "broad_count": len(broad),
            "ticker_specific_count": len(yahoo),
            "distinct_sources": len(sources),
            "buzz_level": _bucket(headline_count),
            "polarity_score": round(polarity, 3),
            "top_headlines": top[:3],
            "composite_score": composite,
        })

    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    return {
        "scout": "news",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "broad_items_count": len(broad_items),
        "degraded_sources": degraded_sources,
        "degraded": len(degraded_sources) >= 2,   # majority dark = degraded
        "candidates": candidates,
    }
