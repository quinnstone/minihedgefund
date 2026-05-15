"""Tests for the news + insider scouts and their downstream wiring."""

from datetime import date
from unittest.mock import MagicMock, patch

from src.agents.scouts.news import _bucket, _composite, _polarity_score
from src.agents.scouts.insider import run_insider_scout
from src.collectors.edgar import EdgarCollector, InsiderActivity, InsiderTransaction
from src.discord.composer import _insider_embed


class TestNewsCompositeMath:
    def test_zero_headlines_returns_neutral(self):
        assert _composite(0, 0.0, 0) == 50.0

    def test_positive_polarity_pushes_up(self):
        # 5 headlines, +0.5 polarity, 3 sources → 50 + 5*1.5 + 12.5 + 10 = 80
        assert _composite(5, 0.5, 3) == 80.0

    def test_negative_polarity_pushes_down(self):
        # 5 headlines, -0.5 polarity, 1 source → 50 + 7.5 - 12.5 + 0 = 45
        assert _composite(5, -0.5, 1) == 45.0

    def test_bucket(self):
        assert _bucket(0) == "none"
        assert _bucket(1) == "low"
        assert _bucket(3) == "med"
        assert _bucket(10) == "high"

    def test_polarity_safe_on_empty(self):
        assert _polarity_score("") == 0.0


class TestInsiderScout:
    def test_etf_short_circuits(self):
        # Mock EDGAR — should never be called for ETFs
        edgar = MagicMock(spec=EdgarCollector)
        edgar.get_insider_activity = MagicMock()

        brief = run_insider_scout(["SPY", "QQQ", "IBIT"], edgar)

        assert edgar.get_insider_activity.call_count == 0
        assert brief["etfs_skipped"] == 3
        assert all(c["is_etf"] for c in brief["candidates"])
        assert all(c["composite_score"] == 50.0 for c in brief["candidates"])

    def test_non_etf_calls_edgar_once_per_ticker(self):
        edgar = MagicMock(spec=EdgarCollector)
        edgar.get_insider_activity = MagicMock(return_value=InsiderActivity(ticker="NVDA"))

        brief = run_insider_scout(["NVDA", "AAPL", "MSFT"], edgar)

        assert edgar.get_insider_activity.call_count == 3
        assert brief["etfs_skipped"] == 0

    def test_aggregation_passes_through(self):
        act = InsiderActivity(
            ticker="AAPL",
            buy_count=0,
            sell_count=3,
            distinct_buyers=0,
            distinct_sellers=2,
            buy_value_usd=0,
            sell_value_usd=71_559_182,
            net_value_usd=-71_559_182,
            cluster_buy=False,
        )
        edgar = MagicMock(spec=EdgarCollector)
        edgar.get_insider_activity = MagicMock(return_value=act)

        brief = run_insider_scout(["AAPL"], edgar)
        c = brief["candidates"][0]
        assert c["ticker"] == "AAPL"
        assert c["net_value_usd"] == -71_559_182
        # ±$1M caps the linear scale → score 25 (50 - 25 from net direction)
        assert c["composite_score"] == 25.0


class TestInsiderEmbed:
    def test_skips_when_no_notable_activity(self):
        brief = {"candidates": [
            {"ticker": "NVDA", "is_etf": False, "net_value_usd": 100, "cluster_buy": False,
             "buy_count": 1, "sell_count": 0, "distinct_buyers": 1, "distinct_sellers": 0},
        ]}
        assert _insider_embed(brief) is None

    def test_includes_cluster_buy_even_at_low_value(self):
        brief = {"candidates": [
            {"ticker": "NVDA", "is_etf": False, "net_value_usd": 1000, "cluster_buy": True,
             "buy_count": 3, "sell_count": 0, "distinct_buyers": 3, "distinct_sellers": 0},
        ]}
        embed = _insider_embed(brief)
        assert embed is not None
        assert "NVDA" in embed["description"]
        assert "cluster-buy" in embed["description"]

    def test_skips_etfs(self):
        brief = {"candidates": [
            {"ticker": "SPY", "is_etf": True, "net_value_usd": 1_000_000, "cluster_buy": True},
        ]}
        assert _insider_embed(brief) is None

    def test_sorts_by_absolute_net(self):
        brief = {"candidates": [
            {"ticker": "AAA", "is_etf": False, "net_value_usd": 100_000, "cluster_buy": False,
             "buy_count": 1, "sell_count": 0, "distinct_buyers": 1, "distinct_sellers": 0},
            {"ticker": "BBB", "is_etf": False, "net_value_usd": -500_000, "cluster_buy": False,
             "buy_count": 0, "sell_count": 1, "distinct_buyers": 0, "distinct_sellers": 1},
        ]}
        embed = _insider_embed(brief)
        # BBB has bigger abs net, should appear first
        bbb_idx = embed["description"].find("BBB")
        aaa_idx = embed["description"].find("AAA")
        assert 0 <= bbb_idx < aaa_idx
