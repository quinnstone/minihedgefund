"""Technical scout — MA stack, RSI, momentum, 20-day breakout.

Pure deterministic indicators on OHLCV from yfinance. No LLM call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

from ...utils import to_yfinance


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(0.0, change)
        loss = max(0.0, -change)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ma(closes: list[float], window: int) -> Optional[float]:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _momentum_pct(closes: list[float], lookback: int) -> Optional[float]:
    if len(closes) <= lookback:
        return None
    base = closes[-lookback - 1]
    if base <= 0:
        return None
    return (closes[-1] - base) / base * 100.0


def _composite(
    ma_stack: str,
    rsi: Optional[float],
    momentum_1w: Optional[float],
    momentum_1m: Optional[float],
    breakout_20d: bool,
) -> float:
    """0–100 technical posture score. 50 = neutral."""
    score = 50.0

    if ma_stack == "bull":
        score += 12
    elif ma_stack == "bear":
        score -= 12

    if rsi is not None:
        if 40 <= rsi <= 65:
            score += 6  # healthy uptrend zone
        elif rsi < 30:
            score -= 4  # oversold panic — wait for confirmation
        elif rsi > 75:
            score -= 8  # overbought — risk of mean reversion

    if momentum_1m is not None:
        score += max(-10.0, min(10.0, momentum_1m / 2.0))

    if momentum_1w is not None:
        score += max(-5.0, min(5.0, momentum_1w))

    if breakout_20d:
        score += 8

    return round(max(0.0, min(100.0, score)), 1)


def _analyze_ticker(ticker: str) -> dict:
    closes: list[float] = []
    try:
        hist = yf.Ticker(to_yfinance(ticker)).history(period="3mo", auto_adjust=True)
        if hist is not None and not hist.empty:
            closes = [float(c) for c in hist["Close"].tolist()]
    except Exception:
        pass

    if len(closes) < 30:
        return {
            "ticker": ticker,
            "ma_stack": "unknown",
            "rsi": None,
            "momentum_1w_pct": None,
            "momentum_1m_pct": None,
            "twentyd_breakout": False,
            "composite_score": 50.0,
            "data_available": False,
        }

    ma20 = _ma(closes, 20)
    ma50 = _ma(closes, 50)
    rsi = _rsi(closes, 14)
    mom_1w = _momentum_pct(closes, 5)
    mom_1m = _momentum_pct(closes, 21)

    last = closes[-1]
    if ma20 is not None and ma50 is not None:
        if last > ma20 > ma50:
            ma_stack = "bull"
        elif last < ma20 < ma50:
            ma_stack = "bear"
        else:
            ma_stack = "mixed"
    else:
        ma_stack = "unknown"

    if len(closes) >= 20:
        prior_20d_high = max(closes[-21:-1])
        breakout = last > prior_20d_high
    else:
        breakout = False

    composite = _composite(ma_stack, rsi, mom_1w, mom_1m, breakout)

    return {
        "ticker": ticker,
        "last_close": round(last, 2),
        "ma20": round(ma20, 2) if ma20 is not None else None,
        "ma50": round(ma50, 2) if ma50 is not None else None,
        "ma_stack": ma_stack,
        "rsi": round(rsi, 1) if rsi is not None else None,
        "momentum_1w_pct": round(mom_1w, 2) if mom_1w is not None else None,
        "momentum_1m_pct": round(mom_1m, 2) if mom_1m is not None else None,
        "twentyd_breakout": breakout,
        "composite_score": composite,
        "data_available": True,
    }


def run_technical_scout(universe: list[str]) -> dict:
    candidates = [_analyze_ticker(t) for t in universe]
    candidates.sort(key=lambda c: c["composite_score"], reverse=True)
    return {
        "scout": "technical",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "degraded": False,
        "candidates": candidates,
    }
