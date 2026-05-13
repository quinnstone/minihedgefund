"""Influencer scout — FinTwit narrative via Nitter, with graceful degradation.

When Nitter is unavailable (the public-instance world is fragile), the brief
returns `degraded=True` and the rest of the pipeline runs without this signal.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ...collectors.nitter import NitterCollector


_BULLISH_TOKENS = re.compile(r"\b(buy|long|breakout|moon|rocket|squeeze|rip|run|rally|bullish|undervalued|cheap)\b", re.IGNORECASE)
_BEARISH_TOKENS = re.compile(r"\b(sell|short|dump|crash|tank|bagholder|overvalued|bubble|bearish|puts)\b", re.IGNORECASE)


def _quick_polarity(text: str) -> str:
    bulls = len(_BULLISH_TOKENS.findall(text))
    bears = len(_BEARISH_TOKENS.findall(text))
    if bulls > bears + 1:
        return "bullish"
    if bears > bulls + 1:
        return "bearish"
    return "neutral"


def run_influencer_scout(
    universe: list[str],
    nitter: NitterCollector,
    sleep_s: float = 1.5,
) -> dict:
    """Per-ticker influencer chatter. Always returns; flags `degraded` on failure."""
    candidates = []
    any_success = False
    error_notes: list[str] = []

    for ticker in universe:
        result = nitter.search_cashtag(ticker, limit=20)
        if result.degraded or not result.tweets:
            if result.error:
                error_notes.append(f"{ticker}: {result.error}")
            candidates.append({
                "ticker": ticker,
                "mentions": 0,
                "polarity": "unknown",
                "bullish_count": 0,
                "bearish_count": 0,
                "top_handles": [],
                "degraded": True,
            })
            time.sleep(sleep_s)
            continue

        any_success = True
        bull = bear = neutral = 0
        for t in result.tweets:
            p = _quick_polarity(t.body)
            if p == "bullish":
                bull += 1
            elif p == "bearish":
                bear += 1
            else:
                neutral += 1

        polarity = "bullish" if bull > bear + 1 else ("bearish" if bear > bull + 1 else "neutral")
        top_handles = list({t.handle for t in sorted(result.tweets, key=lambda x: x.likes, reverse=True)[:5] if t.handle})

        candidates.append({
            "ticker": ticker,
            "mentions": len(result.tweets),
            "polarity": polarity,
            "bullish_count": bull,
            "bearish_count": bear,
            "top_handles": top_handles,
            "degraded": False,
        })
        time.sleep(sleep_s)

    return {
        "scout": "influencer",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "degraded": not any_success,
        "error_notes": error_notes[:5],
        "candidates": candidates,
    }
