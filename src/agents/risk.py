"""Risk agent — deterministic per-candidate risk flags + portfolio-level view.

No LLM call. Risk is mechanical: volatility, liquidity, correlation with
existing book, sector concentration, single-name cap. The PM agent reads
the risk view and may override, but every override is logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

from ..portfolio.state import PortfolioState
from ..utils import to_yfinance


MAX_SINGLE_NAME_PCT = 0.20      # 20% cap on any one position
MAX_SECTOR_PCT = 0.40           # 40% cap on any sector
MIN_AVG_DOLLAR_VOLUME = 5_000_000   # liquidity floor
HIGH_VOL_ANNUALIZED = 0.50      # 50% annualized vol = "high vol" flag


@dataclass
class RiskFlags:
    ticker: str
    annualized_vol: Optional[float] = None
    avg_dollar_volume: Optional[float] = None
    sector: Optional[str] = None
    flags: list[str] = field(default_factory=list)
    suggested_max_weight: float = MAX_SINGLE_NAME_PCT


@dataclass
class PortfolioRiskView:
    current_concentration: dict[str, float] = field(default_factory=dict)  # ticker -> weight
    sector_concentration: dict[str, float] = field(default_factory=dict)
    headroom_pct: float = 1.0   # cash + room remaining within caps
    notes: list[str] = field(default_factory=list)


def _vol_and_volume(ticker: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Annualized vol, avg dollar volume, sector — all from yfinance."""
    try:
        t = yf.Ticker(to_yfinance(ticker))
        hist = t.history(period="3mo", auto_adjust=True)
    except Exception:
        return None, None, None

    vol = None
    if hist is not None and not hist.empty and len(hist) > 5:
        try:
            returns = hist["Close"].pct_change().dropna()
            if len(returns) > 5:
                vol = float(returns.std() * (252 ** 0.5))
        except Exception:
            pass

    avg_dv = None
    if hist is not None and not hist.empty:
        try:
            dv = (hist["Close"] * hist["Volume"]).tail(20).mean()
            avg_dv = float(dv) if dv == dv else None  # NaN guard
        except Exception:
            pass

    sector = None
    try:
        info = t.info or {}
        sector = info.get("sector")
    except Exception:
        pass

    return vol, avg_dv, sector


def assess_candidates(tickers: list[str]) -> dict[str, RiskFlags]:
    """Deterministic per-candidate risk flags."""
    out: dict[str, RiskFlags] = {}
    for ticker in tickers:
        vol, avg_dv, sector = _vol_and_volume(ticker)
        rf = RiskFlags(ticker=ticker, annualized_vol=vol, avg_dollar_volume=avg_dv, sector=sector)

        if vol is not None and vol > HIGH_VOL_ANNUALIZED:
            rf.flags.append("high_vol")
            rf.suggested_max_weight = 0.10
        if avg_dv is not None and avg_dv < MIN_AVG_DOLLAR_VOLUME:
            rf.flags.append("thin_liquidity")
            rf.suggested_max_weight = min(rf.suggested_max_weight, 0.05)
        if vol is None or avg_dv is None:
            rf.flags.append("data_unavailable")

        out[ticker] = rf
    return out


def portfolio_view(
    portfolio: PortfolioState,
    price_map: dict[str, float],
    candidate_risk: dict[str, RiskFlags],
) -> PortfolioRiskView:
    """Aggregate the current book's concentration and surface room for adds."""
    weights = portfolio.position_weights(price_map)
    sector_conc: dict[str, float] = {}
    for ticker, w in weights.items():
        sector = candidate_risk.get(ticker).sector if ticker in candidate_risk else None
        if sector is None:
            sector = "Unknown"
        sector_conc[sector] = sector_conc.get(sector, 0.0) + w

    notes: list[str] = []
    for ticker, w in weights.items():
        if w > MAX_SINGLE_NAME_PCT:
            notes.append(f"{ticker} at {w:.0%} exceeds {MAX_SINGLE_NAME_PCT:.0%} single-name cap")
    for sector, w in sector_conc.items():
        if w > MAX_SECTOR_PCT:
            notes.append(f"{sector} at {w:.0%} exceeds {MAX_SECTOR_PCT:.0%} sector cap")

    cash_pct = portfolio.cash_pct(price_map)
    return PortfolioRiskView(
        current_concentration=weights,
        sector_concentration=sector_conc,
        headroom_pct=cash_pct,
        notes=notes,
    )


def build_risk_brief(
    tickers: list[str],
    portfolio: PortfolioState,
    price_map: dict[str, float],
) -> dict:
    """Combined output suitable for handing to the PM agent."""
    candidate_risk = assess_candidates(tickers)
    pv = portfolio_view(portfolio, price_map, candidate_risk)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "single_name_cap_pct": MAX_SINGLE_NAME_PCT,
        "sector_cap_pct": MAX_SECTOR_PCT,
        "candidates": {
            t: {
                "annualized_vol": rf.annualized_vol,
                "avg_dollar_volume": rf.avg_dollar_volume,
                "sector": rf.sector,
                "flags": rf.flags,
                "suggested_max_weight": rf.suggested_max_weight,
            }
            for t, rf in candidate_risk.items()
        },
        "portfolio": {
            "current_concentration": pv.current_concentration,
            "sector_concentration": pv.sector_concentration,
            "cash_headroom_pct": pv.headroom_pct,
            "violations": pv.notes,
        },
    }
