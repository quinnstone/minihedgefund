"""Scouts — deterministic data-assembly modules.

Each scout pulls raw signals from its domain (sentiment, earnings, technical,
macro, FinTwit) and emits a structured brief. The synthesis agent later
merges these briefs across tickers. Scouts do not call the LLM directly;
that keeps every per-week decision deterministic from the same scout inputs
and cheap to replay during backtests.
"""

from .sentiment import run_sentiment_scout
from .earnings import run_earnings_scout
from .technical import run_technical_scout
from .macro import run_macro_scout
from .influencer import run_influencer_scout
from .news import run_news_scout
from .insider import run_insider_scout

__all__ = [
    "run_sentiment_scout",
    "run_earnings_scout",
    "run_technical_scout",
    "run_macro_scout",
    "run_influencer_scout",
    "run_news_scout",
    "run_insider_scout",
]
