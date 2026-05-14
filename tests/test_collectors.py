"""Smoke tests for collectors — pure parsing/aggregation, no network."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.collectors.reddit import RedditCollector
from src.collectors.stocktwits import StockTwitsAggregate, StockTwitsCollector, StockTwitsMessage
from src.collectors.nitter import NitterCollector
from src.config import RedditConfig


class TestRedditTickerExtraction:
    def setup_method(self):
        self.c = RedditCollector(RedditConfig(client_id="", client_secret="", user_agent="test"))

    def test_dollar_prefix(self):
        assert self.c._extract_tickers("$AAPL is going up") == ["AAPL"]

    def test_bare_tickers(self):
        assert self.c._extract_tickers("Buy MSFT and GOOGL") == ["MSFT", "GOOGL"]

    def test_no_tickers(self):
        assert self.c._extract_tickers("no tickers here") == []

    def test_excluded_words_filtered(self):
        assert self.c._extract_tickers("THE CEO said BUY") == []

    def test_mixed(self):
        result = self.c._extract_tickers("$AAPL $MSFT NVDA are trending")
        assert "AAPL" in result
        assert "MSFT" in result
        assert "NVDA" in result


class TestRedditFetchParsing:
    def setup_method(self):
        self.c = RedditCollector(RedditConfig(client_id="", client_secret="", user_agent="test"))

    @patch.object(RedditCollector, "_fetch_via_rss", return_value=[])
    @patch("src.collectors.reddit.requests.Session.get")
    def test_parses_json_response_when_rss_empty(self, mock_get, _mock_rss):
        """RSS returns nothing → falls back to JSON path."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {"children": [
                {"data": {
                    "title": "$NVDA to the moon",
                    "selftext": "Reasons...",
                    "score": 1500,
                    "num_comments": 200,
                    "created_utc": datetime.utcnow().timestamp(),
                    "permalink": "/r/wallstreetbets/comments/abc/",
                    "link_flair_text": "DD",
                    "upvote_ratio": 0.95,
                }}
            ]}
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        raw = self.c._fetch_subreddit_hot("wallstreetbets", limit=10)
        assert len(raw) == 1
        assert raw[0]["title"] == "$NVDA to the moon"
        assert raw[0]["score"] == 1500   # JSON path preserves engagement

    @patch.object(RedditCollector, "_fetch_via_rss", return_value=[])
    @patch("src.collectors.reddit.requests.Session.get")
    def test_network_failure_returns_empty(self, mock_get, _mock_rss):
        import requests
        mock_get.side_effect = requests.RequestException("connection failed")
        raw = self.c._fetch_subreddit_hot("wallstreetbets")
        assert raw == []


class TestStockTwitsAggregation:
    def test_balanced_sentiment(self):
        c = StockTwitsCollector()
        msgs = [
            StockTwitsMessage(1, "moon", datetime.utcnow(), 1, "u1", 100, False, "Bullish", ["NVDA"], 5),
            StockTwitsMessage(2, "rip", datetime.utcnow(), 2, "u2", 200, False, "Bearish", ["NVDA"], 3),
        ]
        agg = c.aggregate("NVDA", msgs)
        assert agg.bullish_count == 1
        assert agg.bearish_count == 1
        # Follower-weighted: 100 - 200 = -100 / 300 = -0.333
        assert abs(agg.weighted_score + 100.0 / 300.0) < 1e-6

    def test_no_tagged_returns_zero(self):
        c = StockTwitsCollector()
        msgs = [
            StockTwitsMessage(1, "post", datetime.utcnow(), 1, "u1", 100, False, None, ["NVDA"], 0),
        ]
        agg = c.aggregate("NVDA", msgs)
        assert agg.tagged_count == 0
        assert agg.raw_score == 0.0
        assert agg.weighted_score == 0.0

    def test_message_parser_handles_missing_sentiment(self):
        m = StockTwitsCollector._parse_message({
            "id": 42, "body": "hello $NVDA", "created_at": "2026-05-13T12:00:00Z",
            "user": {"id": 1, "username": "u1", "followers": 50},
            "entities": {},   # no sentiment
            "symbols": [{"symbol": "NVDA"}],
            "likes": {"total": 3},
        })
        assert m.self_sentiment is None
        assert m.symbols == ["NVDA"]
        assert m.user_followers == 50


class TestNitterDegradation:
    def test_all_dead_pool_returns_degraded(self):
        # Use a guaranteed-unreachable instance
        c = NitterCollector(instances=["https://definitely-not-real-12345.invalid"], timeout=2)
        result = c.get_user_timeline("anyhandle", limit=5)
        assert result.degraded is True
        assert result.tweets == []
        assert result.instance_used is None
