"""Sentiment scout — Reddit retail buzz + StockTwits self-tagged sentiment.

Per ticker, returns: mention volume buckets, StockTwits raw + follower-weighted
sentiment, Reddit engagement score, and a 0–100 composite where 50 = neutral.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ...collectors.reddit import RedditCollector, TickerMention
from ...collectors.stocktwits import StockTwitsCollector, StockTwitsAggregate


def _buzz_level(mention_count: int) -> str:
    if mention_count >= 15:
        return "high"
    if mention_count >= 5:
        return "med"
    return "low"


def _reddit_score(mention: Optional[TickerMention]) -> float:
    """0–100 score from Reddit engagement. Neutral = 50."""
    if not mention:
        return 50.0
    engagement = mention.total_score + mention.total_comments
    # Saturating function — engagement above 10k tops out, below 100 stays near 50
    if engagement <= 100:
        return 50.0
    boost = min(40.0, 40.0 * (engagement - 100) / 9_900)
    return 50.0 + boost  # all positive — Reddit can't be "bearish" by mention count alone


def _stocktwits_score(agg: Optional[StockTwitsAggregate]) -> float:
    """0–100 score. 50 = neutral. Built from follower-weighted self-tags."""
    if not agg or agg.tagged_count < 3:
        return 50.0
    # weighted_score is in [-1, +1]; rescale to [10, 90]
    return 50.0 + agg.weighted_score * 40.0


def _composite(reddit_score: float, st_score: float, buzz: str) -> float:
    """Blend, then dampen if volume is low."""
    base = (reddit_score + st_score) / 2.0
    if buzz == "low":
        # pull toward neutral when there isn't enough chatter to trust
        base = 50.0 + (base - 50.0) * 0.5
    return round(max(0.0, min(100.0, base)), 1)


def run_sentiment_scout(
    universe: list[str],
    reddit: RedditCollector,
    stocktwits: StockTwitsCollector,
    lookback_days: int = 7,
) -> dict:
    """Build the sentiment brief for a candidate universe.

    Universe is the set of tickers the scout assesses. If empty, the scout
    falls back to the union of Reddit-trending + StockTwits-trending names.
    """
    if not universe:
        st_trending = stocktwits.get_trending(limit=15, exclude_crypto=True)
        rd_trending = [m.ticker for m in reddit.get_trending_tickers(days_back=lookback_days, top_n=15)]
        universe = sorted(set(st_trending) | set(rd_trending))[:25]

    reddit_posts = reddit.collect_posts(days_back=lookback_days)
    reddit_mentions = {m.ticker: m for m in reddit.aggregate_ticker_mentions(reddit_posts, min_mentions=1)}

    st_results = stocktwits.get_multiple(universe, limit_per=30)

    candidates = []
    for ticker in universe:
        rd = reddit_mentions.get(ticker)
        st = st_results.get(ticker.upper())
        mention_count = (rd.mention_count if rd else 0)
        buzz = _buzz_level(mention_count + (st.message_count if st else 0))

        rd_score = _reddit_score(rd)
        st_score = _stocktwits_score(st)
        composite = _composite(rd_score, st_score, buzz)

        candidates.append({
            "ticker": ticker,
            "buzz_level": buzz,
            "reddit_mentions": mention_count,
            "reddit_engagement": (rd.total_score + rd.total_comments) if rd else 0,
            "reddit_score": round(rd_score, 1),
            "stocktwits_messages": st.message_count if st else 0,
            "stocktwits_tagged": st.tagged_count if st else 0,
            "stocktwits_bullish": st.bullish_count if st else 0,
            "stocktwits_bearish": st.bearish_count if st else 0,
            "stocktwits_weighted_score": round(st.weighted_score, 3) if st else 0.0,
            "stocktwits_score": round(st_score, 1),
            "composite_score": composite,
        })

    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    return {
        "scout": "sentiment",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "lookback_days": lookback_days,
        "universe_size": len(universe),
        "degraded": False,
        "candidates": candidates,
    }
