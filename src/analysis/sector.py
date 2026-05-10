"""Sector positioning analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..collectors.market import MarketCollector, SectorPerformance

logger = logging.getLogger(__name__)


@dataclass
class SectorSignal:
    """Sector rotation signal."""
    sector: str
    etf_ticker: str
    signal: str  # "strong_buy", "buy", "neutral", "sell", "strong_sell"
    momentum_1w: float
    momentum_1m: float
    relative_strength: float  # vs S&P 500
    summary: str


class SectorAnalyzer:
    """Analyzes sector rotation and relative performance."""

    def __init__(self, market_collector: MarketCollector):
        """Initialize with market data collector."""
        self.market = market_collector

    def analyze_sectors(self) -> list[SectorSignal]:
        """Analyze all sectors and generate signals."""
        sector_data = self.market.get_sector_performance()
        index_data = self.market.get_index_data()

        # Get S&P 500 weekly performance for relative comparison
        sp500_1w = 0.0
        sp500_1m = 0.0
        for idx in index_data:
            if idx.name == "S&P 500":
                sp500_1w = idx.change_percent
                break

        signals = []
        for sector in sector_data:
            relative_strength = sector.change_percent_1w - sp500_1w
            signal = self._classify_signal(
                momentum_1w=sector.change_percent_1w,
                momentum_1m=sector.change_percent_1m,
                relative_strength=relative_strength,
            )

            summary = self._generate_summary(
                sector=sector.sector,
                signal=signal,
                change_1w=sector.change_percent_1w,
                change_1m=sector.change_percent_1m,
                relative_strength=relative_strength,
            )

            signals.append(SectorSignal(
                sector=sector.sector,
                etf_ticker=sector.etf_ticker,
                signal=signal,
                momentum_1w=sector.change_percent_1w,
                momentum_1m=sector.change_percent_1m,
                relative_strength=relative_strength,
                summary=summary,
            ))

        # Sort by relative strength
        signals.sort(key=lambda s: s.relative_strength, reverse=True)
        return signals

    def _classify_signal(
        self,
        momentum_1w: float,
        momentum_1m: float,
        relative_strength: float,
    ) -> str:
        """Classify sector signal based on momentum and relative strength."""
        score = 0

        # Weekly momentum
        if momentum_1w > 2:
            score += 2
        elif momentum_1w > 0.5:
            score += 1
        elif momentum_1w < -2:
            score -= 2
        elif momentum_1w < -0.5:
            score -= 1

        # Monthly momentum
        if momentum_1m > 5:
            score += 2
        elif momentum_1m > 1:
            score += 1
        elif momentum_1m < -5:
            score -= 2
        elif momentum_1m < -1:
            score -= 1

        # Relative strength vs S&P 500
        if relative_strength > 1.5:
            score += 2
        elif relative_strength > 0.5:
            score += 1
        elif relative_strength < -1.5:
            score -= 2
        elif relative_strength < -0.5:
            score -= 1

        if score >= 4:
            return "strong_buy"
        elif score >= 2:
            return "buy"
        elif score <= -4:
            return "strong_sell"
        elif score <= -2:
            return "sell"
        return "neutral"

    def _generate_summary(
        self,
        sector: str,
        signal: str,
        change_1w: float,
        change_1m: float,
        relative_strength: float,
    ) -> str:
        """Generate sector summary."""
        direction = "outperforming" if relative_strength > 0 else "underperforming"
        return (
            f"{sector}: {change_1w:+.1f}% this week, {change_1m:+.1f}% this month. "
            f"{direction.capitalize()} the S&P 500 by {abs(relative_strength):.1f}pp."
        )

    def get_sector_heatmap_data(self) -> list[dict]:
        """Get data formatted for sector heatmap display."""
        sector_data = self.market.get_sector_performance()

        heatmap = []
        for sector in sector_data:
            heatmap.append({
                "sector": sector.sector,
                "etf": sector.etf_ticker,
                "change_1d": sector.change_percent_1d,
                "change_1w": sector.change_percent_1w,
                "change_1m": sector.change_percent_1m,
                "color": self._get_heatmap_color(sector.change_percent_1w),
            })

        return heatmap

    def _get_heatmap_color(self, change_pct: float) -> str:
        """Get heatmap color based on performance."""
        if change_pct > 3:
            return "#1a9641"    # dark green
        elif change_pct > 1:
            return "#a6d96a"    # light green
        elif change_pct > -1:
            return "#ffffbf"    # yellow/neutral
        elif change_pct > -3:
            return "#fdae61"    # orange
        return "#d7191c"        # red
