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
        """Try RSS first (works from datacenter IPs Reddit blocks for JSON),
        fall back to JSON if RSS returns nothing. Output is normalized to
        the same dict shape regardless of source.
        """
        rss_posts = self._fetch_via_rss(subreddit, limit=limit)
        if rss_posts:
            return rss_posts

        url = f"https://www.reddit.com/r/{subreddit}/hot.json"
        try:
            resp = self._session.get(
                url, params={"limit": limit}, timeout=self.DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("reddit JSON fetch failed for r/%s: %s", subreddit, exc)
            return []

        return [c.get("data", {}) for c in (data.get("data", {}).get("children") or [])]

    def _fetch_via_rss(self, subreddit: str, limit: int = 100) -> list[dict]:
        """Reddit Atom RSS endpoint. Often unblocked on IPs where JSON returns 403.

        Cost: no score / num_comments / upvote_ratio. We zero those out; the
        synthesis still gets buzz volume from mention counts."""
        from bs4 import BeautifulSoup

        url = f"https://www.reddit.com/r/{subreddit}/hot/.rss"
        try:
            resp = self._session.get(
                url, params={"limit": min(limit, 100)}, timeout=self.DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("reddit RSS fetch failed for r/%s: %s", subreddit, exc)
            return []

        try:
            soup = BeautifulSoup(resp.text, "lxml-xml")
        except Exception:
            soup = BeautifulSoup(resp.text, "xml")

        entries = soup.find_all("entry")
        if not entries:
            return []

        out: list[dict] = []
        for entry in entries[:limit]:
            title_el = entry.find("title")
            title = title_el.get_text(strip=True) if title_el else ""
            content_el = entry.find("content")
            # Reddit RSS embeds the post HTML in <content>. Strip to plain text.
            selftext = ""
            if content_el is not None:
                content_soup = BeautifulSoup(content_el.get_text(), "lxml")
                selftext = content_soup.get_text(separator=" ", strip=True)[:1000]
            link_el = entry.find("link")
            permalink = link_el.get("href") if link_el is not None else ""
            updated_el = entry.find("updated") or entry.find("published")
            updated_ts = None
            if updated_el is not None:
                try:
                    s = updated_el.get_text(strip=True).replace("Z", "+00:00")
                    updated_ts = datetime.fromisoformat(s).timestamp()
                except (ValueError, AttributeError):
                    pass

            out.append({
                "title": title,
                "selftext": selftext,
                "score": 0,                # not in RSS
                "num_comments": 0,         # not in RSS
                "created_utc": updated_ts or datetime.utcnow().timestamp(),
                "permalink": permalink.replace("https://www.reddit.com", ""),
                "link_flair_text": None,
                "upvote_ratio": 0.0,
            })

        return out

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
