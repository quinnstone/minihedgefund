"""Candidate universe builder.

Universe = current positions ∪ core watchlist ∪ scout-discovered names.
Capped to keep scout/synthesis cost predictable.
"""

from __future__ import annotations

from typing import Optional


CORE_WATCHLIST = [
    # Mega-cap tech
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    # Semis + AI infra
    "AMD", "AVGO", "MU", "SMCI",
    # Other large caps
    "BRK.B", "JPM", "V", "UNH", "WMT", "COST",
    # Broad index ETFs
    "SPY", "QQQ", "IWM", "DIA",
    # Sector ETFs
    "XLK", "XLF", "XLV", "XLE", "XLY", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC",
    # Crypto exposure (via ETFs)
    "IBIT", "ETHA",
    # Bonds (defensive)
    "TLT", "SHY",
    # Gold (defensive)
    "GLD",
]

DEFAULT_MAX_UNIVERSE = 30


def build_universe(
    current_positions: list[str],
    discovered: Optional[list[str]] = None,
    extras: Optional[list[str]] = None,
    max_size: int = DEFAULT_MAX_UNIVERSE,
) -> list[str]:
    """Compose the week's candidate set.

    Always includes:
      - current positions (so the PM can decide HOLD/TRIM/CLOSE on each)
      - core watchlist (consistent benchmark set across weeks)
    Plus:
      - `discovered`: heat-discovery from scouts (trending tickers)
      - `extras`: any explicit user-pinned tickers
    """
    seen: set[str] = set()
    out: list[str] = []

    # Current positions go first — they always survive the cap
    for t in current_positions:
        t = t.upper()
        if t not in seen:
            seen.add(t)
            out.append(t)

    forced_keep = len(out)  # never trimmed below this count

    for t in (extras or []):
        t = t.upper()
        if t not in seen:
            seen.add(t)
            out.append(t)

    for t in CORE_WATCHLIST:
        if t not in seen:
            seen.add(t)
            out.append(t)

    for t in (discovered or []):
        t = t.upper()
        if t not in seen:
            seen.add(t)
            out.append(t)

    if len(out) > max_size:
        out = out[:max(forced_keep, max_size)]

    return out
