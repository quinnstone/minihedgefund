"""News collector using NewsAPI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from newsapi import NewsApiClient

from ..config import NewsConfig

logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    """Represents a news article."""
    title: str
    description: str
    source: str
    url: str
    published_at: datetime
    content: Optional[str] = None
    image_url: Optional[str] = None


class NewsCollector:
    """Collects financial news from NewsAPI."""

    # Major financial news sources
    SOURCES = [
        "the-wall-street-journal",
        "bloomberg",
        "reuters",
        "cnbc",
        "financial-times",
        "business-insider",
        "fortune",
        "the-economist",
    ]

    # Financial keywords for search
    KEYWORDS = [
        "stock market",
        "earnings",
        "federal reserve",
        "inflation",
        "interest rates",
        "GDP",
        "unemployment",
        "S&P 500",
        "Dow Jones",
        "NASDAQ",
        "cryptocurrency",
        "bitcoin",
        "merger acquisition",
        "IPO",
    ]

    def __init__(self, config: NewsConfig):
        """Initialize NewsAPI client."""
        self.client = NewsApiClient(api_key=config.api_key)

    def _parse_datetime(self, date_str: str) -> datetime:
        """Parse ISO datetime string."""
        if not date_str:
            return datetime.utcnow()
        try:
            # Handle various ISO formats
            date_str = date_str.replace("Z", "+00:00")
            if "+" in date_str:
                date_str = date_str.split("+")[0]
            return datetime.fromisoformat(date_str)
        except ValueError:
            return datetime.utcnow()

    def collect_top_headlines(
        self,
        category: str = "business",
        country: str = "us",
        page_size: int = 50,
    ) -> list[NewsArticle]:
        """Collect top business headlines."""
        articles = []

        try:
            response = self.client.get_top_headlines(
                category=category,
                country=country,
                page_size=page_size,
            )

            for item in response.get("articles", []):
                articles.append(NewsArticle(
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    source=item.get("source", {}).get("name", "Unknown"),
                    url=item.get("url", ""),
                    published_at=self._parse_datetime(item.get("publishedAt")),
                    content=item.get("content"),
                    image_url=item.get("urlToImage"),
                ))

            logger.info(f"Collected {len(articles)} top headlines")

        except Exception as e:
            logger.error(f"Error collecting top headlines: {e}")

        return articles

    def search_financial_news(
        self,
        query: Optional[str] = None,
        days_back: int = 7,
        page_size: int = 50,
    ) -> list[NewsArticle]:
        """Search for financial news articles."""
        articles = []
        from_date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        # Use provided query or default financial keywords
        search_query = query or " OR ".join(self.KEYWORDS[:5])

        try:
            response = self.client.get_everything(
                q=search_query,
                from_param=from_date,
                language="en",
                sort_by="relevancy",
                page_size=page_size,
            )

            for item in response.get("articles", []):
                articles.append(NewsArticle(
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    source=item.get("source", {}).get("name", "Unknown"),
                    url=item.get("url", ""),
                    published_at=self._parse_datetime(item.get("publishedAt")),
                    content=item.get("content"),
                    image_url=item.get("urlToImage"),
                ))

            logger.info(f"Collected {len(articles)} articles for query: {search_query[:50]}")

        except Exception as e:
            logger.error(f"Error searching news: {e}")

        return articles

    def collect_from_sources(
        self,
        days_back: int = 7,
        page_size: int = 20,
    ) -> list[NewsArticle]:
        """Collect articles from specific financial sources."""
        articles = []
        from_date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        # NewsAPI free tier limits sources per request
        sources_str = ",".join(self.SOURCES[:5])

        try:
            response = self.client.get_everything(
                sources=sources_str,
                from_param=from_date,
                language="en",
                sort_by="publishedAt",
                page_size=page_size,
            )

            for item in response.get("articles", []):
                articles.append(NewsArticle(
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    source=item.get("source", {}).get("name", "Unknown"),
                    url=item.get("url", ""),
                    published_at=self._parse_datetime(item.get("publishedAt")),
                    content=item.get("content"),
                    image_url=item.get("urlToImage"),
                ))

            logger.info(f"Collected {len(articles)} articles from sources")

        except Exception as e:
            logger.error(f"Error collecting from sources: {e}")

        return articles

    def get_all_financial_news(self, days_back: int = 7) -> list[NewsArticle]:
        """Collect all financial news from various sources."""
        all_articles = []

        # Get top headlines
        all_articles.extend(self.collect_top_headlines())

        # Search for market-related news
        all_articles.extend(self.search_financial_news(
            query="stock market OR earnings OR Federal Reserve",
            days_back=days_back,
        ))

        # Deduplicate by URL
        seen_urls = set()
        unique_articles = []
        for article in all_articles:
            if article.url not in seen_urls:
                seen_urls.add(article.url)
                unique_articles.append(article)

        # Sort by publish date
        unique_articles.sort(key=lambda a: a.published_at, reverse=True)

        logger.info(f"Total unique articles collected: {len(unique_articles)}")
        return unique_articles
