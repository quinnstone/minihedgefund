"""Shared utilities — ticker normalization, ETF detection, etc."""

from __future__ import annotations


# Canonical ETF tickers we trade. Used to skip earnings checks on funds.
KNOWN_ETFS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV",
    "XLK", "XLF", "XLV", "XLE", "XLY", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "TLT", "SHY", "IEF", "TIP",
    "GLD", "SLV",
    "IBIT", "ETHA", "BITO", "FBTC",
    "ARKK", "SOXX", "SMH", "IGV", "XBI",
    "VEA", "VWO", "EFA", "EEM",
    "HYG", "LQD", "AGG", "BND",
    "USO", "UNG",
    "VNQ", "REM",
})


def to_yfinance(ticker: str) -> str:
    """Canonical ticker (BRK.B) → yfinance format (BRK-B).

    Yahoo Finance uses dash separators for share-class tickers; the rest
    of our data sources (StockTwits, Reddit, NewsAPI) use dots. Convert
    only when about to hit yfinance.
    """
    return ticker.upper().replace(".", "-")


def from_yfinance(ticker: str) -> str:
    """Yahoo format (BRK-B) → canonical (BRK.B).

    Use when consuming yfinance output that needs to be matched against
    our canonical ticker keys."""
    # Heuristic: only single-letter share-class suffixes get the dot back.
    # AAPL-USD (a crypto pair) should NOT become AAPL.USD.
    parts = ticker.upper().split("-")
    if len(parts) == 2 and len(parts[1]) == 1:
        return f"{parts[0]}.{parts[1]}"
    return ticker.upper()


def is_etf(ticker: str) -> bool:
    """True if ticker is in our known ETF list.

    Conservative — heuristics like "starts with X" would catch sector
    ETFs but also false-positive on XOM, XPO, etc. Use the known-list
    plus runtime quoteType checks where stronger detection is needed.
    """
    return ticker.upper() in KNOWN_ETFS
