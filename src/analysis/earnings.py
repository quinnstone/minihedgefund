"""Earnings impact analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..collectors.market import EarningsData, StockData, MarketCollector

logger = logging.getLogger(__name__)


@dataclass
class EarningsImpact:
    """Earnings report impact assessment."""
    ticker: str
    company_name: str
    result: str  # "beat", "miss", "inline"
    eps_surprise_pct: Optional[float]
    price_reaction_pct: float
    impact_score: float  # 0-10 composite score
    summary: str


class EarningsAnalyzer:
    """Analyzes earnings reports and their market impact."""

    # Thresholds for earnings classification
    BEAT_THRESHOLD = 2.0   # >2% above estimate = beat
    MISS_THRESHOLD = -2.0  # >2% below estimate = miss

    # Weights for impact scoring
    WEIGHT_SURPRISE = 0.3
    WEIGHT_PRICE_REACTION = 0.4
    WEIGHT_MARKET_CAP = 0.3

    def __init__(self, market_collector: MarketCollector):
        """Initialize with market data collector."""
        self.market = market_collector

    def analyze_earnings(
        self,
        earnings_data: list[EarningsData],
    ) -> list[EarningsImpact]:
        """Analyze earnings reports and calculate impact scores."""
        impacts = []

        for earnings in earnings_data:
            # Get current stock data for price reaction
            stock_data = self.market.get_stock_data(earnings.ticker)
            if not stock_data:
                continue

            # Calculate EPS surprise
            eps_surprise_pct = None
            result = "inline"

            if earnings.eps_actual is not None and earnings.eps_estimate is not None:
                if earnings.eps_estimate != 0:
                    eps_surprise_pct = (
                        (earnings.eps_actual - earnings.eps_estimate) /
                        abs(earnings.eps_estimate) * 100
                    )

                    if eps_surprise_pct > self.BEAT_THRESHOLD:
                        result = "beat"
                    elif eps_surprise_pct < self.MISS_THRESHOLD:
                        result = "miss"

            # Price reaction
            price_reaction = stock_data.change_percent

            # Calculate impact score (0-10)
            impact_score = self._calculate_impact_score(
                eps_surprise_pct=eps_surprise_pct,
                price_reaction=price_reaction,
                market_cap=stock_data.market_cap,
            )

            # Generate summary
            summary = self._generate_summary(
                company=earnings.company_name,
                result=result,
                eps_surprise_pct=eps_surprise_pct,
                price_reaction=price_reaction,
            )

            impacts.append(EarningsImpact(
                ticker=earnings.ticker,
                company_name=earnings.company_name,
                result=result,
                eps_surprise_pct=eps_surprise_pct,
                price_reaction_pct=price_reaction,
                impact_score=impact_score,
                summary=summary,
            ))

        # Sort by impact score
        impacts.sort(key=lambda e: e.impact_score, reverse=True)
        return impacts

    def _calculate_impact_score(
        self,
        eps_surprise_pct: Optional[float],
        price_reaction: float,
        market_cap: Optional[float],
    ) -> float:
        """Calculate composite impact score (0-10)."""
        score = 0.0

        # Surprise magnitude (0-10)
        if eps_surprise_pct is not None:
            surprise_score = min(10, abs(eps_surprise_pct) / 5 * 10)
            score += surprise_score * self.WEIGHT_SURPRISE

        # Price reaction magnitude (0-10)
        price_score = min(10, abs(price_reaction) / 5 * 10)
        score += price_score * self.WEIGHT_PRICE_REACTION

        # Market cap significance (0-10)
        if market_cap:
            if market_cap > 1e12:      # >$1T (mega cap)
                cap_score = 10
            elif market_cap > 200e9:   # >$200B (large cap)
                cap_score = 8
            elif market_cap > 50e9:    # >$50B (mid-large cap)
                cap_score = 6
            elif market_cap > 10e9:    # >$10B (mid cap)
                cap_score = 4
            else:
                cap_score = 2
            score += cap_score * self.WEIGHT_MARKET_CAP

        return round(min(10, score), 1)

    def _generate_summary(
        self,
        company: str,
        result: str,
        eps_surprise_pct: Optional[float],
        price_reaction: float,
    ) -> str:
        """Generate a human-readable earnings summary."""
        if result == "beat":
            action = "beat estimates"
            if eps_surprise_pct:
                action += f" by {abs(eps_surprise_pct):.1f}%"
        elif result == "miss":
            action = "missed estimates"
            if eps_surprise_pct:
                action += f" by {abs(eps_surprise_pct):.1f}%"
        else:
            action = "reported inline with estimates"

        direction = "up" if price_reaction > 0 else "down"
        return (
            f"{company} {action}. "
            f"Stock moved {direction} {abs(price_reaction):.1f}% in reaction."
        )

    def get_notable_earnings(
        self,
        min_impact_score: float = 5.0,
    ) -> list[EarningsImpact]:
        """Get earnings reports with significant market impact."""
        earnings_data = self.market.get_earnings_calendar()
        all_impacts = self.analyze_earnings(earnings_data)
        return [e for e in all_impacts if e.impact_score >= min_impact_score]
