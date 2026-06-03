"""CLI: parse data/actual_entries.txt and regenerate data/actual_book.json.

Triggered by .github/workflows/parse-actual-entries.yml on push to the
entries file. Also runnable locally for debugging.

Fetches current prices for currently-held tickers via yfinance, plus SPY at
inception date and today (for the alpha-vs-SPY benchmark). All network IO
is bounded — no agent calls, no LLM, ~3 second runtime typical.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yfinance as yf                                                     # noqa: E402

from src.tracking.actual_book import (                                    # noqa: E402
    build_actual_book,
    held_tickers_from_entries,
    parse_entries_file,
    save_actual_book,
)
from src.tracking.marking import fetch_price_map                          # noqa: E402
from src.utils import to_yfinance                                         # noqa: E402

logger = logging.getLogger(__name__)


def _spy_price_at(d: date) -> Optional[float]:
    """SPY closing price on date `d` (or the next trading day if d is a weekend)."""
    try:
        hist = yf.Ticker("SPY").history(
            start=d.isoformat(),
            end=(d + timedelta(days=7)).isoformat(),
            auto_adjust=True,
        )
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[0])
    except Exception as exc:
        logger.warning("SPY price fetch failed for %s: %s", d, exc)
        return None


def _spy_current_price() -> Optional[float]:
    try:
        hist = yf.Ticker("SPY").history(period="5d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as exc:
        logger.warning("SPY current price fetch failed: %s", exc)
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    entries, warnings = parse_entries_file()
    for w in warnings:
        print(f"WARN  {w}", file=sys.stderr)
    print(f"Parsed {len(entries)} entries ({len(warnings)} warning(s))")

    if not entries:
        # Save an empty book so the composer knows the file exists but has no data
        save_actual_book({
            "inception_date": None,
            "initial_capital": 10_000.0,
            "cash": 10_000.0,
            "positions_value": 0.0,
            "current_aum": 10_000.0,
            "cumulative_return_pct": 0.0,
            "cumulative_return_usd": 0.0,
            "realized_pnl": 0.0,
            "spy_inception_price": None,
            "spy_current_price": None,
            "spy_return_from_inception_pct": None,
            "alpha_pct": None,
            "positions": [],
            "closed_trades_count": 0,
            "entries_count": 0,
            "leveraged": False,
        })
        print("No entries — saved empty book")
        return 0

    # Current prices for currently-held tickers
    held = held_tickers_from_entries(entries)
    print(f"Currently held: {held}")
    price_map = fetch_price_map(held) if held else {}

    # SPY at inception date + today, for benchmark math
    inception = entries[0].date
    spy_inception = _spy_price_at(inception)
    spy_current = _spy_current_price()
    print(f"SPY: inception ({inception}) = ${spy_inception}, current = ${spy_current}")

    book = build_actual_book(
        entries=entries,
        current_price_map=price_map,
        spy_inception_price=spy_inception,
        spy_current_price=spy_current,
    )
    save_actual_book(book)

    print()
    print(f"  AUM:               ${book['current_aum']:.2f}")
    print(f"  Cumulative:        {book['cumulative_return_pct'] * 100:+.2f}%")
    if book["spy_return_from_inception_pct"] is not None:
        print(f"  SPY since {inception}: {book['spy_return_from_inception_pct'] * 100:+.2f}%")
        print(f"  Alpha:             {book['alpha_pct'] * 100:+.2f}%")
    print(f"  Positions:         {len(book['positions'])}")
    print(f"  Closed trades:     {book['closed_trades_count']}")
    if book["leveraged"]:
        print(f"  ⚠ Cash is negative (${book['cash']:.2f}) — over-deployed vs $10k notional")
    return 0


if __name__ == "__main__":
    sys.exit(main())
