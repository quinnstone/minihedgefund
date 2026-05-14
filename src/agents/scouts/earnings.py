"""Earnings scout — recent beat/miss, post-earnings drift, upcoming dates.

ETFs are skipped cleanly with `is_etf: True` — they don't have earnings.
Ticker is normalized to yfinance format (BRK.B → BRK-B) before any yf calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import yfinance as yf

from ...collectors.market import MarketCollector
from ...utils import is_etf, to_yfinance


def _drift_score(drift_pct: Optional[float]) -> float:
    """Positive drift = momentum-friendly setup. Maps to [10, 90]; 50 = no drift."""
    if drift_pct is None:
        return 50.0
    # ±10% drift saturates at the edges
    capped = max(-10.0, min(10.0, drift_pct))
    return 50.0 + capped * 4.0


def _composite(result: Optional[str], drift_pct: Optional[float], days_since: Optional[int]) -> float:
    base = _drift_score(drift_pct)
    if result == "beat":
        base += 5.0
    elif result == "miss":
        base -= 5.0
    # Recency decay — beyond ~30 days drift signal stales
    if days_since is not None:
        if days_since > 45:
            base = 50.0 + (base - 50.0) * 0.3
        elif days_since > 30:
            base = 50.0 + (base - 50.0) * 0.6
    return round(max(0.0, min(100.0, base)), 1)


def _recent_earnings_event(ticker: str, max_lookback_days: int = 60) -> dict:
    """Pull the most recent earnings event from yfinance.

    Returns a dict with: result (beat/miss/in_line/None), days_since, surprise_pct.
    """
    out = {"result": None, "days_since": None, "surprise_pct": None}
    try:
        t = yf.Ticker(to_yfinance(ticker))
        df = t.earnings_dates
    except Exception:
        return out
    if df is None or df.empty:
        return out

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_lookback_days)
        df = df.reset_index()
        for _, row in df.iterrows():
            ed = row.get("Earnings Date")
            ed_dt = ed.to_pydatetime() if hasattr(ed, "to_pydatetime") else None
            if ed_dt is None:
                continue
            if ed_dt.tzinfo is None:
                ed_dt = ed_dt.replace(tzinfo=timezone.utc)
            if ed_dt > datetime.now(timezone.utc):
                continue  # future
            if ed_dt < cutoff:
                break  # too old

            actual = row.get("Reported EPS")
            estimate = row.get("EPS Estimate")
            surprise = row.get("Surprise(%)")

            if actual is not None and estimate is not None and not _is_nan(actual) and not _is_nan(estimate):
                if actual > estimate * 1.02:
                    result = "beat"
                elif actual < estimate * 0.98:
                    result = "miss"
                else:
                    result = "in_line"
            else:
                result = None

            out = {
                "result": result,
                "days_since": (datetime.now(timezone.utc) - ed_dt).days,
                "surprise_pct": float(surprise) if surprise is not None and not _is_nan(surprise) else None,
            }
            break
    except Exception:
        pass

    return out


def _next_earnings_in_days(ticker: str) -> Optional[int]:
    try:
        t = yf.Ticker(to_yfinance(ticker))
        cal = t.calendar
    except Exception:
        return None
    if cal is None:
        return None
    try:
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, list) and ed:
                first = ed[0]
                next_dt = first if isinstance(first, datetime) else None
            else:
                next_dt = ed if isinstance(ed, datetime) else None
        else:
            next_dt = None
        if next_dt is None:
            return None
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=timezone.utc)
        delta = (next_dt - datetime.now(timezone.utc)).days
        return delta if delta >= 0 else None
    except Exception:
        return None


def _is_nan(x) -> bool:
    try:
        return x != x
    except Exception:
        return False


def run_earnings_scout(
    universe: list[str],
    market: MarketCollector,
) -> dict:
    """Earnings brief for the universe. ETFs get a clean "n/a" entry."""
    candidates = []

    for ticker in universe:
        if is_etf(ticker):
            candidates.append({
                "ticker": ticker,
                "recent_result": "n/a (ETF)",
                "days_since_earnings": None,
                "surprise_pct": None,
                "post_earnings_drift_pct": None,
                "next_earnings_in_days": None,
                "composite_score": 50.0,
                "is_etf": True,
            })
            continue

        evt = _recent_earnings_event(ticker)
        upcoming = _next_earnings_in_days(ticker)

        # Use price change since the earnings date as post-earnings drift
        drift_pct = None
        if evt["result"] is not None and evt["days_since"] is not None:
            try:
                hist = market.get_stock_data(ticker, period="3mo")
                if hist and hist.price_history is not None and len(hist.price_history) > evt["days_since"]:
                    # close on earnings day vs latest close
                    df = hist.price_history
                    earnings_close = df["Close"].iloc[-evt["days_since"] - 1] if evt["days_since"] + 1 < len(df) else None
                    latest_close = df["Close"].iloc[-1]
                    if earnings_close and earnings_close > 0:
                        drift_pct = (latest_close - earnings_close) / earnings_close * 100
            except Exception:
                pass

        composite = _composite(evt["result"], drift_pct, evt["days_since"])

        candidates.append({
            "ticker": ticker,
            "recent_result": evt["result"],
            "days_since_earnings": evt["days_since"],
            "surprise_pct": evt["surprise_pct"],
            "post_earnings_drift_pct": round(drift_pct, 2) if drift_pct is not None else None,
            "next_earnings_in_days": upcoming,
            "composite_score": composite,
        })

    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    return {
        "scout": "earnings",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "degraded": False,
        "candidates": candidates,
    }
