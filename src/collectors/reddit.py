"""Reddit data collector using PRAW."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import praw

from ..config import RedditConfig

logger = logging.getLogger(__name__)


@dataclass
class RedditPost:
    """Represents a Reddit post with financial content."""
    title: str
    subreddit: str
    score: int
    num_comments: int
    url: str
    created_utc: datetime
    selftext: str
    tickers: list[str]
    flair: Optional[str] = None


@dataclass
class TickerMention:
    """Aggregated ticker mention data."""
    ticker: str
    mention_count: int
    total_score: int
    total_comments: int
    posts: list[RedditPost]
    avg_sentiment: float = 0.0


class RedditCollector:
    """Collects financial data from Reddit using PRAW."""

    SUBREDDITS = ["wallstreetbets", "stocks", "investing", "stockmarket", "options"]

    # Common ticker pattern: $AAPL or standalone AAPL (2-5 uppercase letters)
    TICKER_PATTERN = re.compile(r'(?:^|(?<=\s))\$?([A-Z]{2,5})(?=\s|$|[.,;:!?)])')

    # Words that look like tickers but aren't
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

    def __init__(self, config: RedditConfig):
        """Initialize Reddit collector with PRAW."""
        self.reddit = praw.Reddit(
            client_id=config.client_id,
            client_secret=config.client_secret,
            user_agent=config.user_agent,
        )
        self.reddit.read_only = True

    def _extract_tickers(self, text: str) -> list[str]:
        """Extract stock tickers from text."""
        matches = self.TICKER_PATTERN.findall(text)
        return [t for t in matches if t not in self.EXCLUDED_WORDS]

    def collect_posts(
        self,
        days_back: int = 7,
        limit_per_subreddit: int = 100,
    ) -> list[RedditPost]:
        """Collect posts from financial subreddits."""
        posts = []
        cutoff = datetime.utcnow() - timedelta(days=days_back)

        for subreddit_name in self.SUBREDDITS:
            try:
                subreddit = self.reddit.subreddit(subreddit_name)

                for post in subreddit.hot(limit=limit_per_subreddit):
                    created = datetime.utcfromtimestamp(post.created_utc)

                    if created < cutoff:
                        continue

                    # Extract tickers from title and body
                    text = f"{post.title} {post.selftext}"
                    tickers = self._extract_tickers(text)

                    if not tickers:
                        continue

                    posts.append(RedditPost(
                        title=post.title,
                        subreddit=subreddit_name,
                        score=post.score,
                        num_comments=post.num_comments,
                        url=f"https://reddit.com{post.permalink}",
                        created_utc=created,
                        selftext=post.selftext[:1000] if post.selftext else "",
                        tickers=tickers,
                        flair=post.link_flair_text,
                    ))

                logger.info(f"Collected {len(posts)} posts from r/{subreddit_name}")

            except Exception as e:
                logger.error(f"Error collecting from r/{subreddit_name}: {e}")
                continue

        return posts

    def aggregate_ticker_mentions(
        self,
        posts: list[RedditPost],
        min_mentions: int = 3,
    ) -> list[TickerMention]:
        """Aggregate posts by ticker and calculate metrics."""
        ticker_data: dict[str, TickerMention] = {}

        for post in posts:
            for ticker in post.tickers:
                if ticker not in ticker_data:
                    ticker_data[ticker] = TickerMention(
                        ticker=ticker,
                        mention_count=0,
                        total_score=0,
                        total_comments=0,
                        posts=[],
                    )

                mention = ticker_data[ticker]
                mention.mention_count += 1
                mention.total_score += post.score
                mention.total_comments += post.num_comments
                mention.posts.append(post)

        # Filter by minimum mentions and sort by total engagement
        mentions = [
            m for m in ticker_data.values()
            if m.mention_count >= min_mentions
        ]
        mentions.sort(key=lambda m: m.total_score + m.total_comments, reverse=True)

        return mentions

    def get_trending_tickers(
        self,
        days_back: int = 7,
        top_n: int = 20,
    ) -> list[TickerMention]:
        """Get the top trending tickers from Reddit."""
        posts = self.collect_posts(days_back=days_back)
        mentions = self.aggregate_ticker_mentions(posts)
        return mentions[:top_n]
