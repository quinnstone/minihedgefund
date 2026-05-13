"""Schwab execution realism — fractional-share rules + slippage modeling.

Schwab Stock Slices supports fractional shares only for S&P 500 stocks. ETFs,
ADRs, OTC names, and non-S&P stocks must trade in whole shares. The simulation
honors this so simulated returns match what would actually fill in a real account.

Slippage is modeled as a flat bps adjustment in the adverse direction on each
fill (buys execute slightly above mid, sells slightly below). On a $10k retail
account in liquid names, 5 bps is a conservative upper bound — real fills are
usually 1–2 bps.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_SLIPPAGE_BPS = 5  # 0.05% adverse on each fill
DEFAULT_SP500_FILE = Path(__file__).with_name("sp500_snapshot.txt")


def _load_sp500_set(path: Path = DEFAULT_SP500_FILE) -> set[str]:
    if not path.exists():
        return set()
    tickers: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tickers.add(line.upper())
    return tickers


@dataclass
class FillResult:
    """The outcome of a simulated order."""

    ticker: str
    side: str            # "buy" or "sell"
    requested_dollars: float
    fill_price: float    # market price ± slippage
    fill_shares: float   # what actually transacted
    fill_dollars: float  # shares * fill_price
    cash_residual: float # only nonzero on whole-share buys
    fractional_eligible: bool


class SchwabRealism:
    """Apply Schwab Stock Slices constraints + slippage to simulated fills."""

    def __init__(
        self,
        sp500_tickers: Optional[set[str]] = None,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        sp500_file: Optional[Path] = None,
    ):
        if sp500_tickers is not None:
            self.sp500 = {t.upper() for t in sp500_tickers}
        else:
            path = sp500_file or DEFAULT_SP500_FILE
            self.sp500 = _load_sp500_set(path)
        self.slippage_bps = slippage_bps

    def is_fractional_eligible(self, ticker: str) -> bool:
        """True if Schwab Stock Slices supports fractional shares for this name.

        Conservative default: names not in our snapshot are treated as
        whole-share-only. Misses a few S&P 500 names but never falsely
        promises fractional support."""
        return ticker.upper() in self.sp500

    def _apply_slippage(self, market_price: float, side: str) -> float:
        adj = self.slippage_bps / 10_000.0
        return market_price * (1 + adj) if side == "buy" else market_price * (1 - adj)

    def buy(
        self,
        ticker: str,
        target_dollars: float,
        market_price: float,
    ) -> FillResult:
        if target_dollars <= 0:
            raise ValueError(f"target_dollars must be > 0, got {target_dollars}")
        if market_price <= 0:
            raise ValueError(f"market_price must be > 0, got {market_price}")

        fill_price = self._apply_slippage(market_price, "buy")
        eligible = self.is_fractional_eligible(ticker)

        if eligible:
            fill_shares = target_dollars / fill_price
            fill_dollars = target_dollars
            residual = 0.0
        else:
            fill_shares = math.floor(target_dollars / fill_price)
            fill_dollars = fill_shares * fill_price
            residual = target_dollars - fill_dollars

        return FillResult(
            ticker=ticker,
            side="buy",
            requested_dollars=target_dollars,
            fill_price=fill_price,
            fill_shares=fill_shares,
            fill_dollars=fill_dollars,
            cash_residual=residual,
            fractional_eligible=eligible,
        )

    def sell(
        self,
        ticker: str,
        shares_to_sell: float,
        market_price: float,
    ) -> FillResult:
        if shares_to_sell <= 0:
            raise ValueError(f"shares_to_sell must be > 0, got {shares_to_sell}")
        if market_price <= 0:
            raise ValueError(f"market_price must be > 0, got {market_price}")

        fill_price = self._apply_slippage(market_price, "sell")
        eligible = self.is_fractional_eligible(ticker)
        fill_shares = shares_to_sell if eligible else math.floor(shares_to_sell)
        fill_dollars = fill_shares * fill_price

        return FillResult(
            ticker=ticker,
            side="sell",
            requested_dollars=fill_dollars,
            fill_price=fill_price,
            fill_shares=fill_shares,
            fill_dollars=fill_dollars,
            cash_residual=0.0,
            fractional_eligible=eligible,
        )
