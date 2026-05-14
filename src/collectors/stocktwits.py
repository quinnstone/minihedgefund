"""StockTwits collector — cashtag streams, trending symbols, self-tagged sentiment.

StockTwits is a trader-focused social network where users post messages organized
by $TICKER cashtag. About 37% of messages carry an explicit Bullish/Bearish
self-tag, giving us pre-labeled sentiment without LLM inference.

The public API requires no auth. Rate limits are generous (~200 req/hr per IP)
but we sleep between calls to be polite.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.stocktwits.com/api/2"
DEFAULT_TIMEOUT = 10
DEFAULT_SLEEP_S = 1.0

# StockTwits' public API is Cloudflare-protected and 403s from GitHub Actions
# datacenter IPs. We gate the entire collector on an env var so CI (which won't
# set it) skips silently and local runs (where you set it in .env) work normally.
ENABLED_ENV_VAR = "STOCKTWITS_ENABLED"


def _is_enabled() -> bool:
    return os.getenv(ENABLED_ENV_VAR, "").lower() in ("true", "1", "yes", "on")


@dataclass
class StockTwitsMessage:
    message_id: int
    body: str
    created_at: datetime
    user_id: int
    username: str
    user_followers: int
    user_official: bool
    self_sentiment: Optional[str]  # "Bullish" | "Bearish" | None
    symbols: list[str] = field(default_factory=list)
    likes: int = 0


@dataclass
class StockTwitsAggregate:
    """Aggregated sentiment for a single ticker."""

    ticker: str
    message_count: int
    tagged_count: int       # how many had explicit Bullish/Bearish
    bullish_count: int
    bearish_count: int
    bullish_follower_weight: float  # sum of followers across bullish posts
    bearish_follower_weight: float
    raw_score: float                # (bullish - bearish) / tagged_count
    weighted_score: float           # follower-weighted same formula
    top_messages: list[StockTwitsMessage] = field(default_factory=list)


class StockTwitsCollector:
    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        sleep_s: float = DEFAULT_SLEEP_S,
        enabled: Optional[bool] = None,
    ):
        self.timeout = timeout
        self.sleep_s = sleep_s
        # Allow explicit override (mostly for tests); otherwise read env var.
        self.enabled = _is_enabled() if enabled is None else enabled
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "MiniHedgeFund/1.0"})
        if not self.enabled:
            logger.info(
                "StockTwits collector disabled (set %s=true to enable)",
                ENABLED_ENV_VAR,
            )

    def get_cashtag_stream(self, ticker: str, limit: int = 30) -> list[StockTwitsMessage]:
        """Most recent N messages for a ticker. Returns [] on any failure
        or when disabled via env var."""
        if not self.enabled:
            return []
        url = f"{BASE_URL}/streams/symbol/{ticker.upper()}.json"
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("stocktwits cashtag fetch failed for %s: %s", ticker, exc)
            return []

        out: list[StockTwitsMessage] = []
        for m in (data.get("messages") or [])[:limit]:
            try:
                out.append(self._parse_message(m))
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("skipping malformed stocktwits message: %s", exc)
        return out

    def get_trending(self, limit: int = 30, exclude_crypto: bool = True) -> list[str]:
        """Trending symbols across all of StockTwits. .X suffix = crypto token.
        Returns [] when disabled."""
        if not self.enabled:
            return []
        url = f"{BASE_URL}/trending/symbols.json"
        try:
            resp = self._session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("stocktwits trending fetch failed: %s", exc)
            return []

        symbols = data.get("symbols") or []
        out = []
        for s in symbols:
            sym = s.get("symbol", "")
            if not sym:
                continue
            if exclude_crypto and sym.endswith(".X"):
                continue
            out.append(sym.upper())
            if len(out) >= limit:
                break
        return out

    def aggregate(self, ticker: str, messages: list[StockTwitsMessage]) -> StockTwitsAggregate:
        """Build a sentiment summary from a list of messages."""
        bull = [m for m in messages if m.self_sentiment == "Bullish"]
        bear = [m for m in messages if m.self_sentiment == "Bearish"]
        tagged = len(bull) + len(bear)

        bull_followers = sum(m.user_followers for m in bull)
        bear_followers = sum(m.user_followers for m in bear)

        raw = (len(bull) - len(bear)) / tagged if tagged > 0 else 0.0
        total_weight = bull_followers + bear_followers
        weighted = (bull_followers - bear_followers) / total_weight if total_weight > 0 else 0.0

        top = sorted(
            messages,
            key=lambda m: (m.likes, m.user_followers),
            reverse=True,
        )[:5]

        return StockTwitsAggregate(
            ticker=ticker.upper(),
            message_count=len(messages),
            tagged_count=tagged,
            bullish_count=len(bull),
            bearish_count=len(bear),
            bullish_follower_weight=float(bull_followers),
            bearish_follower_weight=float(bear_followers),
            raw_score=raw,
            weighted_score=weighted,
            top_messages=top,
        )

    def get_multiple(
        self,
        tickers: list[str],
        limit_per: int = 30,
    ) -> dict[str, StockTwitsAggregate]:
        """Fetch and aggregate sentiment for a list of tickers, sleeping between calls."""
        out: dict[str, StockTwitsAggregate] = {}
        for i, t in enumerate(tickers):
            messages = self.get_cashtag_stream(t, limit=limit_per)
            out[t.upper()] = self.aggregate(t, messages)
            if i < len(tickers) - 1:
                time.sleep(self.sleep_s)
        return out

    # ----- internal -----

    @staticmethod
    def _parse_message(m: dict) -> StockTwitsMessage:
        user = m.get("user") or {}
        ent = m.get("entities") or {}
        sentiment = (ent.get("sentiment") or {}).get("basic") if isinstance(ent.get("sentiment"), dict) else None
        symbols = [s.get("symbol", "").upper() for s in (m.get("symbols") or []) if s.get("symbol")]

        created_raw = m.get("created_at")
        if isinstance(created_raw, str):
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                created = datetime.utcnow()
        else:
            created = datetime.utcnow()

        return StockTwitsMessage(
            message_id=int(m.get("id", 0)),
            body=m.get("body") or "",
            created_at=created,
            user_id=int(user.get("id", 0)),
            username=user.get("username", ""),
            user_followers=int(user.get("followers", 0) or 0),
            user_official=bool(user.get("official", False)),
            self_sentiment=sentiment,
            symbols=symbols,
            likes=int((m.get("likes") or {}).get("total", 0) or 0),
        )
