"""Reddit collector — unauthenticated JSON endpoint.

We use Reddit's public `/r/{sub}/hot.json` endpoint instead of PRAW. Our weekly
volume (5 subs × 100 posts) is two orders of magnitude under the unauth rate
limit, and the JSON schema is stable and well-documented. Drops PRAW as a
dependency and removes the need for OAuth credentials.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

from ..config import RedditConfig

logger = logging.getLogger(__name__)


@dataclass
class RedditPost:
    title: str
    subreddit: str
    score: int
    num_comments: int
    url: str
    created_utc: datetime
    selftext: str
    tickers: list[str]
    flair: Optional[str] = None
    upvote_ratio: float = 0.0


@dataclass
class TickerMention:
    ticker: str
    mention_count: int
    total_score: int
    total_comments: int
    posts: list[RedditPost] = field(default_factory=list)
    avg_sentiment: float = 0.0


class RedditCollector:
    """Collects financial posts from a curated set of subreddits."""

    SUBREDDITS = ["wallstreetbets", "stocks", "investing", "stockmarket", "options"]

    TICKER_PATTERN = re.compile(r'(?:^|(?<=\s))\$?([A-Z]{2,5})(?=\s|$|[.,;:!?)])')

    EXCLUDED_WORDS = {
        "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HER",
        "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM", "HIS", "HOW",
        "ITS", "LET", "MAY", "NEW", "NOW", "OLD", "SEE", "TWO", "WAY", "WHO",
        "BOY", "DID", "OWN", "SAY", "SHE", "TOO", "USE", "CEO", "CFO", "IPO",
        "ETF", "GDP", "CPI", "FED", "SEC", "NYSE", "IMO", "YOLO", "FOMO",
        "DD", "PT", "TA", "EPS", "PE", "PS", "PB", "ROE", "ROI", "ATH", "ATL",
        "ITM", "OTM", "IV", "DTE", "OP", "TL", "DR", "TLDR", "EDIT", "USA",
        "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "BTC", "ETH", "NFT", "AI",
        "IS", "UP", "SO", "DO", "IF", "OR", "BY", "AT", "TO", "ON", "IN",
        "AN", "AS", "BE", "NO", "GO", "MY", "OF", "WE", "US", "AM",
        "JUST", "LIKE", "THIS", "THAT", "THEY", "BEEN", "HAVE", "WILL",
        "WITH", "FROM", "THAN", "WHAT", "WHEN", "VERY", "MUCH", "MOST",
        "LONG", "HIGH", "OVER", "ONLY", "DOWN", "SOME", "EVEN", "GOOD",
        "ALSO", "BACK", "WELL", "MORE", "YEAR", "HERE", "GOING", "NEXT",
        "BUY", "SELL", "HOLD", "CALL", "PUT", "KEEP", "MADE", "MAKE",
        "LOOK", "TAKE", "COME", "WANT", "GIVE", "FIND", "TELL", "HELP",
        "SHOW", "MOVE", "LIVE", "REAL", "FEEL", "WORK", "NEED", "SURE",
        "EACH", "BEST", "LAST", "MANY", "SAME", "DONE", "LOST", "STAY",
    }

    DEFAULT_TIMEOUT = 10
    DEFAULT_SLEEP_S = 0.5

    def __init__(self, config: Optional[RedditConfig] = None):
        ua = config.user_agent if config and config.user_agent else "MiniHedgeFund/1.0"
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": ua})

    def _extract_tickers(self, text: str) -> list[str]:
        matches = self.TICKER_PATTERN.findall(text)
        return [t for t in matches if t not in self.EXCLUDED_WORDS]

    def _fetch_subreddit_hot(self, subreddit: str, limit: int = 100) -> list[dict]:
        """One unauth JSON page; reddit caps `limit` at ~100 per request."""
        url = f"https://www.reddit.com/r/{subreddit}/hot.json"
        try:
            resp = self._session.get(
                url, params={"limit": limit}, timeout=self.DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("reddit fetch failed for r/%s: %s", subreddit, exc)
            return []

        return [c.get("data", {}) for c in (data.get("data", {}).get("children") or [])]

    def collect_posts(
        self,
        days_back: int = 7,
        limit_per_subreddit: int = 100,
    ) -> list[RedditPost]:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        posts: list[RedditPost] = []

        for i, sub in enumerate(self.SUBREDDITS):
            raw = self._fetch_subreddit_hot(sub, limit=limit_per_subreddit)
            kept = 0
            for r in raw:
                try:
                    created = datetime.utcfromtimestamp(float(r.get("created_utc") or 0))
                except (TypeError, ValueError):
                    continue
                if created < cutoff:
                    continue

                title = r.get("title") or ""
                selftext = r.get("selftext") or ""
                tickers = self._extract_tickers(f"{title} {selftext}")
                if not tickers:
                    continue

                permalink = r.get("permalink") or ""
                posts.append(RedditPost(
                    title=title,
                    subreddit=sub,
                    score=int(r.get("score") or 0),
                    num_comments=int(r.get("num_comments") or 0),
                    url=f"https://reddit.com{permalink}",
                    created_utc=created,
                    selftext=selftext[:1000],
                    tickers=tickers,
                    flair=r.get("link_flair_text"),
                    upvote_ratio=float(r.get("upvote_ratio") or 0.0),
                ))
                kept += 1
            logger.info("collected %d ticker-bearing posts from r/%s", kept, sub)

            if i < len(self.SUBREDDITS) - 1:
                time.sleep(self.DEFAULT_SLEEP_S)

        return posts

    def aggregate_ticker_mentions(
        self,
        posts: list[RedditPost],
        min_mentions: int = 3,
    ) -> list[TickerMention]:
        ticker_data: dict[str, TickerMention] = {}

        for post in posts:
            for ticker in post.tickers:
                m = ticker_data.setdefault(ticker, TickerMention(
                    ticker=ticker, mention_count=0, total_score=0, total_comments=0, posts=[],
                ))
                m.mention_count += 1
                m.total_score += post.score
                m.total_comments += post.num_comments
                m.posts.append(post)

        mentions = [m for m in ticker_data.values() if m.mention_count >= min_mentions]
        mentions.sort(key=lambda m: m.total_score + m.total_comments, reverse=True)
        return mentions

    def get_trending_tickers(
        self,
        days_back: int = 7,
        top_n: int = 20,
    ) -> list[TickerMention]:
        posts = self.collect_posts(days_back=days_back)
        return self.aggregate_ticker_mentions(posts)[:top_n]
