"""Sentiment analysis using VADER."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from ..collectors.reddit import RedditPost, TickerMention
from ..collectors.news import NewsArticle

logger = logging.getLogger(__name__)


@dataclass
class SentimentResult:
    """Sentiment analysis result."""
    text: str
    compound: float
    positive: float
    negative: float
    neutral: float
    label: str  # "bullish", "bearish", or "neutral"


class SentimentAnalyzer:
    """VADER-based sentiment analyzer for financial text."""

    # Financial-specific word adjustments for VADER
    FINANCIAL_LEXICON = {
        "bull": 2.0,
        "bullish": 2.5,
        "bear": -2.0,
        "bearish": -2.5,
        "moon": 2.0,
        "mooning": 2.5,
        "rocket": 1.5,
        "squeeze": 1.0,
        "short": -0.5,
        "calls": 0.5,
        "puts": -0.5,
        "rally": 2.0,
        "crash": -3.0,
        "dump": -2.5,
        "pump": 1.5,
        "dip": -1.0,
        "buy": 1.0,
        "sell": -1.0,
        "hold": 0.5,
        "yolo": 1.0,
        "tendies": 1.5,
        "diamond": 1.5,
        "paper": -1.0,
        "recession": -2.5,
        "inflation": -1.0,
        "growth": 1.5,
        "beat": 2.0,
        "miss": -2.0,
        "upgrade": 2.0,
        "downgrade": -2.0,
        "outperform": 1.5,
        "underperform": -1.5,
        "overweight": 1.0,
        "underweight": -1.0,
        "guidance": 0.5,
        "layoffs": -2.0,
        "restructuring": -1.0,
        "acquisition": 1.0,
        "merger": 0.5,
        "bankruptcy": -3.0,
        "default": -2.5,
        "dividend": 1.0,
        "buyback": 1.5,
        "ath": 2.0,  # all-time high
    }

    # Thresholds for sentiment labels
    BULLISH_THRESHOLD = 0.15
    BEARISH_THRESHOLD = -0.15

    def __init__(self):
        """Initialize VADER with financial lexicon updates."""
        self.analyzer = SentimentIntensityAnalyzer()

        # Add financial-specific words to the lexicon
        self.analyzer.lexicon.update(self.FINANCIAL_LEXICON)

    def analyze_text(self, text: str) -> SentimentResult:
        """Analyze sentiment of a single text."""
        scores = self.analyzer.polarity_scores(text)

        compound = scores["compound"]
        if compound >= self.BULLISH_THRESHOLD:
            label = "bullish"
        elif compound <= self.BEARISH_THRESHOLD:
            label = "bearish"
        else:
            label = "neutral"

        return SentimentResult(
            text=text[:200],
            compound=compound,
            positive=scores["pos"],
            negative=scores["neg"],
            neutral=scores["neu"],
            label=label,
        )

    def analyze_reddit_posts(self, posts: list[RedditPost]) -> list[SentimentResult]:
        """Analyze sentiment of Reddit posts."""
        results = []
        for post in posts:
            text = f"{post.title} {post.selftext}"
            result = self.analyze_text(text)
            results.append(result)
        return results

    def analyze_news_articles(self, articles: list[NewsArticle]) -> list[SentimentResult]:
        """Analyze sentiment of news articles."""
        results = []
        for article in articles:
            text = f"{article.title} {article.description or ''}"
            result = self.analyze_text(text)
            results.append(result)
        return results

    def analyze_ticker_sentiment(
        self,
        ticker_mentions: list[TickerMention],
    ) -> dict[str, float]:
        """Calculate aggregate sentiment for each ticker."""
        ticker_sentiments = {}

        for mention in ticker_mentions:
            sentiments = []
            for post in mention.posts:
                text = f"{post.title} {post.selftext}"
                result = self.analyze_text(text)
                # Weight by post engagement (score + comments)
                weight = max(1, post.score + post.num_comments)
                sentiments.append((result.compound, weight))

            if sentiments:
                total_weight = sum(w for _, w in sentiments)
                weighted_avg = sum(s * w for s, w in sentiments) / total_weight
                ticker_sentiments[mention.ticker] = weighted_avg
                mention.avg_sentiment = weighted_avg

        return ticker_sentiments

    def get_market_mood(self, sentiments: list[SentimentResult]) -> dict:
        """Calculate overall market mood from multiple sentiment results."""
        if not sentiments:
            return {"mood": "neutral", "score": 0, "bullish_pct": 0, "bearish_pct": 0}

        bullish_count = sum(1 for s in sentiments if s.label == "bullish")
        bearish_count = sum(1 for s in sentiments if s.label == "bearish")
        total = len(sentiments)

        avg_compound = sum(s.compound for s in sentiments) / total

        if avg_compound >= self.BULLISH_THRESHOLD:
            mood = "bullish"
        elif avg_compound <= self.BEARISH_THRESHOLD:
            mood = "bearish"
        else:
            mood = "neutral"

        return {
            "mood": mood,
            "score": avg_compound,
            "bullish_pct": (bullish_count / total) * 100,
            "bearish_pct": (bearish_count / total) * 100,
            "neutral_pct": ((total - bullish_count - bearish_count) / total) * 100,
            "total_analyzed": total,
        }
