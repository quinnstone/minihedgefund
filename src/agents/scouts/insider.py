"""Insider scout — SEC Form 4 buys + sales aggregated per ticker.

Insider buying is one of the better-documented quant signals; cluster buys
(≥3 distinct insiders within a short window) are especially predictive.
Insider selling is a weaker signal because there are non-bearish reasons
to sell (taxes, diversification, lifestyle).

ETFs have no insider concept and are skipped cleanly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ...collectors.edgar import EdgarCollector
from ...utils import is_etf


def run_insider_scout(universe: list[str], edgar: EdgarCollector) -> dict:
    candidates = []
    skipped_etfs = 0

    for ticker in universe:
        if is_etf(ticker):
            candidates.append({
                "ticker": ticker,
                "is_etf": True,
                "buy_count": 0,
                "sell_count": 0,
                "distinct_buyers": 0,
                "distinct_sellers": 0,
                "net_value_usd": 0.0,
                "cluster_buy": False,
                "composite_score": 50.0,
                "top_transactions": [],
            })
            skipped_etfs += 1
            continue

        act = edgar.get_insider_activity(ticker)
        candidates.append({
            "ticker": ticker,
            "is_etf": False,
            "buy_count": act.buy_count,
            "sell_count": act.sell_count,
            "distinct_buyers": act.distinct_buyers,
            "distinct_sellers": act.distinct_sellers,
            "buy_value_usd": round(act.buy_value_usd, 2),
            "sell_value_usd": round(act.sell_value_usd, 2),
            "net_value_usd": round(act.net_value_usd, 2),
            "cluster_buy": act.cluster_buy,
            "composite_score": act.composite_score,
            "top_transactions": [
                {
                    "filer": t.filer_name,
                    "role": t.relationship,
                    "direction": t.direction,
                    "shares": t.shares,
                    "price": t.price_per_share,
                    "value_usd": round(t.value_usd, 2),
                    "date": t.transaction_date.isoformat(),
                }
                for t in act.transactions[:5]
            ],
        })

    candidates.sort(key=lambda c: c["composite_score"], reverse=True)

    return {
        "scout": "insider",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(universe),
        "etfs_skipped": skipped_etfs,
        "degraded": False,
        "candidates": candidates,
    }
