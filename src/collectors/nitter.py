"""Nitter collector — FinTwit timelines and cashtag search with failover.

Nitter scrapes Twitter/X without auth. Public instances are notoriously
unreliable (X has actively broken guest API access), so this wrapper:

  1. Tries instances in order from a pool
  2. Records failures per instance and trips a circuit breaker after N
     consecutive failures (auto-recovers after `recovery_seconds`)
  3. Never raises — total failure returns NitterResult(degraded=True) so
     the rest of the pipeline runs without the FinTwit signal

Critical invariant: a Nitter outage MUST NOT cascade into a missed weekly
decision. The PM gets a partial signal set and notes the degradation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


DEFAULT_INSTANCES = [
    "https://nitter.tiekoetter.com",
    # Add more instances as they come online. The breaker handles dead ones.
]

DEFAULT_TIMEOUT = 10
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RECOVERY_SECONDS = 300


@dataclass
class NitterTweet:
    handle: str
    body: str
    timestamp: Optional[datetime] = None
    likes: int = 0
    retweets: int = 0
    replies: int = 0


@dataclass
class NitterResult:
    """The output of any Nitter operation. Always populated, never raises."""

    tweets: list[NitterTweet] = field(default_factory=list)
    degraded: bool = False
    error: Optional[str] = None
    instance_used: Optional[str] = None
    instances_tried: list[str] = field(default_factory=list)


class _CircuitBreaker:
    """Per-instance failure tracking. Skip instances that recently failed N+ times."""

    def __init__(self, failure_threshold: int, recovery_seconds: int):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures: dict[str, int] = {}
        self._tripped_at: dict[str, float] = {}

    def record_success(self, instance: str) -> None:
        self._failures.pop(instance, None)
        self._tripped_at.pop(instance, None)

    def record_failure(self, instance: str) -> None:
        self._failures[instance] = self._failures.get(instance, 0) + 1
        if self._failures[instance] >= self.failure_threshold:
            self._tripped_at[instance] = time.time()

    def is_available(self, instance: str) -> bool:
        tripped = self._tripped_at.get(instance)
        if tripped is None:
            return True
        if time.time() - tripped > self.recovery_seconds:
            # cooldown elapsed — give it another shot
            self._failures.pop(instance, None)
            self._tripped_at.pop(instance, None)
            return True
        return False


class NitterCollector:
    def __init__(
        self,
        instances: Optional[list[str]] = None,
        timeout: int = DEFAULT_TIMEOUT,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_seconds: int = DEFAULT_RECOVERY_SECONDS,
    ):
        self.instances = list(instances or DEFAULT_INSTANCES)
        self.timeout = timeout
        self.breaker = _CircuitBreaker(failure_threshold, recovery_seconds)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; MiniHedgeFund/1.0)",
        })

    def get_user_timeline(self, handle: str, limit: int = 20) -> NitterResult:
        """Recent tweets from a single user. Strips '@' if present."""
        h = handle.lstrip("@")
        return self._try_instances(path=f"/{h}", limit=limit)

    def search_cashtag(self, ticker: str, limit: int = 20) -> NitterResult:
        """Search for `$TICKER` mentions across Twitter."""
        return self._try_instances(
            path=f"/search?f=tweets&q=%24{ticker.upper()}",
            limit=limit,
        )

    # ----- internal -----

    def _try_instances(self, path: str, limit: int) -> NitterResult:
        tried: list[str] = []
        last_error: Optional[str] = None

        for instance in self.instances:
            if not self.breaker.is_available(instance):
                continue
            tried.append(instance)
            url = f"{instance.rstrip('/')}{path}"
            try:
                resp = self._session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                tweets = self._parse(resp.text, limit=limit)
                # Empty page is suspicious — count as a failure but still return what we have
                if not tweets:
                    self.breaker.record_failure(instance)
                    last_error = f"{instance} returned 0 tweets (instance may be degraded)"
                    continue
                self.breaker.record_success(instance)
                return NitterResult(
                    tweets=tweets,
                    degraded=False,
                    instance_used=instance,
                    instances_tried=tried,
                )
            except requests.RequestException as exc:
                self.breaker.record_failure(instance)
                last_error = f"{instance}: {type(exc).__name__}: {exc}"
                logger.warning("nitter fetch failed at %s: %s", instance, exc)
            except Exception as exc:
                self.breaker.record_failure(instance)
                last_error = f"{instance}: parse error: {exc}"
                logger.exception("nitter parse failed at %s", instance)

        return NitterResult(
            tweets=[],
            degraded=True,
            error=last_error or "no instances available",
            instances_tried=tried,
        )

    @staticmethod
    def _parse(html: str, limit: int) -> list[NitterTweet]:
        soup = BeautifulSoup(html, "lxml")
        items = soup.select(".timeline-item")
        tweets: list[NitterTweet] = []
        for item in items[:limit]:
            content_el = item.select_one(".tweet-content")
            if content_el is None:
                continue
            handle_el = item.select_one(".username")
            date_el = item.select_one(".tweet-date a")
            stats = item.select(".tweet-stat")

            timestamp = None
            if date_el is not None and date_el.has_attr("title"):
                try:
                    timestamp = datetime.strptime(date_el["title"], "%b %d, %Y · %I:%M %p UTC")
                except ValueError:
                    pass

            def _stat_value(idx: int) -> int:
                if idx >= len(stats):
                    return 0
                txt = stats[idx].get_text(strip=True).replace(",", "")
                if txt.endswith("K"):
                    try:
                        return int(float(txt[:-1]) * 1000)
                    except ValueError:
                        return 0
                try:
                    return int(txt) if txt.isdigit() else 0
                except ValueError:
                    return 0

            tweets.append(NitterTweet(
                handle=(handle_el.get_text(strip=True) if handle_el else "").lstrip("@"),
                body=content_el.get_text(separator=" ", strip=True),
                timestamp=timestamp,
                replies=_stat_value(0),
                retweets=_stat_value(1),
                likes=_stat_value(2),
            ))

        return tweets
