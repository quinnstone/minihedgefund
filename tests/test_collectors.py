"""Tests for data collectors."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.collectors.reddit import RedditCollector, RedditPost, TickerMention
from src.collectors.news import NewsCollector, NewsArticle
from src.collectors.market import MarketCollector, StockData, IndexData, SectorPerformance
from src.collectors.macro import MacroCollector, MacroIndicator
from src.config import RedditConfig, NewsConfig, FredConfig


# --- Reddit Collector Tests ---

class TestRedditCollector:

    def test_extract_tickers(self):
        config = RedditConfig(
            client_id="test", client_secret="test", user_agent="test"
        )
        with patch("praw.Reddit"):
            collector = RedditCollector(config)

        assert collector._extract_tickers("$AAPL is going up") == ["AAPL"]
        assert collector._extract_tickers("Buy MSFT and GOOGL") == ["MSFT", "GOOGL"]
        assert collector._extract_tickers("no tickers here") == []
        # Excluded words should be filtered
        assert collector._extract_tickers("THE CEO said BUY") == []

    def test_extract_tickers_multiple(self):
        config = RedditConfig(
            client_id="test", client_secret="test", user_agent="test"
        )
        with patch("praw.Reddit"):
            collector = RedditCollector(config)

        result = collector._extract_tickers("$AAPL $MSFT NVDA are trending")
        assert "AAPL" in result
        assert "MSFT" in result
        assert "NVDA" in result

    def test_aggregate_ticker_mentions(self):
        config = RedditConfig(
            client_id="test", client_secret="test", user_agent="test"
        )
        with patch("praw.Reddit"):
            collector = RedditCollector(config)

        posts = [
            RedditPost(
                title="AAPL earnings",
                subreddit="stocks",
                score=100,
                num_comments=50,
                url="https://reddit.com/1",
                created_utc=datetime.utcnow(),
                selftext="",
                tickers=["AAPL"],
            ),
            RedditPost(
                title="AAPL guidance",
                subreddit="stocks",
                score=200,
                num_comments=100,
                url="https://reddit.com/2",
                created_utc=datetime.utcnow(),
                selftext="",
                tickers=["AAPL"],
            ),
            RedditPost(
                title="AAPL technical",
                subreddit="stocks",
                score=50,
                num_comments=20,
                url="https://reddit.com/3",
                created_utc=datetime.utcnow(),
                selftext="",
                tickers=["AAPL"],
            ),
        ]

        mentions = collector.aggregate_ticker_mentions(posts, min_mentions=2)
        assert len(mentions) == 1
        assert mentions[0].ticker == "AAPL"
        assert mentions[0].mention_count == 3
        assert mentions[0].total_score == 350


# --- News Collector Tests ---

class TestNewsCollector:

    def test_parse_datetime(self):
        config = NewsConfig(api_key="test")
        with patch("newsapi.NewsApiClient"):
            collector = NewsCollector(config)

        result = collector._parse_datetime("2024-01-15T10:30:00Z")
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_parse_datetime_empty(self):
        config = NewsConfig(api_key="test")
        with patch("newsapi.NewsApiClient"):
            collector = NewsCollector(config)

        result = collector._parse_datetime("")
        assert isinstance(result, datetime)

    @patch("src.collectors.news.NewsApiClient")
    def test_collect_top_headlines(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_top_headlines.return_value = {
            "articles": [
                {
                    "title": "Markets Rally",
                    "description": "S&P 500 hits new high",
                    "source": {"name": "CNBC"},
                    "url": "https://example.com/1",
                    "publishedAt": "2024-01-15T10:00:00Z",
                    "content": "Full content here",
                    "urlToImage": None,
                }
            ]
        }
        mock_client_cls.return_value = mock_client

        collector = NewsCollector(NewsConfig(api_key="test"))
        articles = collector.collect_top_headlines()

        assert len(articles) == 1
        assert articles[0].title == "Markets Rally"
        assert articles[0].source == "CNBC"


# --- Market Collector Tests ---

class TestMarketCollector:

    @patch("yfinance.Ticker")
    def test_get_stock_data(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "regularMarketPrice": 150.0,
            "regularMarketPreviousClose": 145.0,
            "shortName": "Apple Inc.",
            "regularMarketVolume": 50000000,
            "marketCap": 2500000000000,
            "trailingPE": 28.5,
            "fiftyTwoWeekHigh": 160.0,
            "fiftyTwoWeekLow": 120.0,
        }
        mock_ticker_cls.return_value = mock_ticker

        collector = MarketCollector()
        data = collector.get_stock_data("AAPL")

        assert data is not None
        assert data.ticker == "AAPL"
        assert data.current_price == 150.0
        assert abs(data.change_percent - 3.448) < 0.01

    @patch("yfinance.Ticker")
    def test_get_stock_data_missing_info(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker_cls.return_value = mock_ticker

        collector = MarketCollector()
        data = collector.get_stock_data("INVALID")
        assert data is None


# --- Macro Collector Tests ---

class TestMacroCollector:

    @patch("src.collectors.macro.Fred")
    def test_get_indicator(self, mock_fred_cls):
        import pandas as pd

        mock_fred = MagicMock()
        dates = pd.date_range("2024-01-01", periods=12, freq="MS")
        mock_fred.get_series.return_value = pd.Series(
            [5.25, 5.25, 5.25, 5.25, 5.25, 5.25,
             5.25, 5.25, 5.25, 5.25, 5.50, 5.50],
            index=dates,
        )
        mock_fred_cls.return_value = mock_fred

        collector = MacroCollector(FredConfig(api_key="test"))
        indicator = collector.get_indicator("fed_funds_rate")

        assert indicator is not None
        assert indicator.name == "Federal Funds Rate"
        assert indicator.current_value == 5.50

    @patch("src.collectors.macro.Fred")
    def test_get_indicator_unknown_series(self, mock_fred_cls):
        mock_fred_cls.return_value = MagicMock()
        collector = MacroCollector(FredConfig(api_key="test"))
        result = collector.get_indicator("nonexistent_key")
        assert result is None
