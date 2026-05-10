"""Macroeconomic data collector using FRED API."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fredapi import Fred

from ..config import FredConfig

logger = logging.getLogger(__name__)


@dataclass
class MacroIndicator:
    """Macroeconomic indicator data."""
    name: str
    series_id: str
    current_value: float
    previous_value: float
    change: float
    change_percent: float
    last_updated: datetime
    unit: str
    description: str


@dataclass
class MacroSummary:
    """Summary of key macroeconomic indicators."""
    fed_funds_rate: Optional[MacroIndicator]
    inflation_cpi: Optional[MacroIndicator]
    unemployment_rate: Optional[MacroIndicator]
    gdp_growth: Optional[MacroIndicator]
    treasury_10y: Optional[MacroIndicator]
    consumer_sentiment: Optional[MacroIndicator]


class MacroCollector:
    """Collects macroeconomic data from FRED."""

    # Key FRED series
    SERIES = {
        "fed_funds_rate": {
            "id": "FEDFUNDS",
            "name": "Federal Funds Rate",
            "unit": "%",
            "description": "Federal Reserve target interest rate",
        },
        "inflation_cpi": {
            "id": "CPIAUCSL",
            "name": "Consumer Price Index",
            "unit": "Index",
            "description": "Consumer Price Index for All Urban Consumers",
        },
        "unemployment_rate": {
            "id": "UNRATE",
            "name": "Unemployment Rate",
            "unit": "%",
            "description": "Civilian unemployment rate",
        },
        "gdp_growth": {
            "id": "A191RL1Q225SBEA",
            "name": "Real GDP Growth",
            "unit": "%",
            "description": "Real GDP percent change from preceding period",
        },
        "treasury_10y": {
            "id": "DGS10",
            "name": "10-Year Treasury",
            "unit": "%",
            "description": "10-Year Treasury Constant Maturity Rate",
        },
        "consumer_sentiment": {
            "id": "UMCSENT",
            "name": "Consumer Sentiment",
            "unit": "Index",
            "description": "University of Michigan Consumer Sentiment",
        },
        "pce_inflation": {
            "id": "PCEPI",
            "name": "PCE Inflation",
            "unit": "Index",
            "description": "Personal Consumption Expenditures Price Index",
        },
        "housing_starts": {
            "id": "HOUST",
            "name": "Housing Starts",
            "unit": "Thousands",
            "description": "New privately-owned housing units started",
        },
        "retail_sales": {
            "id": "RSXFS",
            "name": "Retail Sales",
            "unit": "Millions $",
            "description": "Advance retail sales",
        },
        "industrial_production": {
            "id": "INDPRO",
            "name": "Industrial Production",
            "unit": "Index",
            "description": "Industrial Production Index",
        },
    }

    def __init__(self, config: FredConfig):
        """Initialize FRED client."""
        self.fred = Fred(api_key=config.api_key)

    def get_indicator(
        self,
        series_key: str,
        lookback_periods: int = 2,
    ) -> Optional[MacroIndicator]:
        """Get a specific macroeconomic indicator."""
        if series_key not in self.SERIES:
            logger.error(f"Unknown series key: {series_key}")
            return None

        series_info = self.SERIES[series_key]
        series_id = series_info["id"]

        try:
            # Get recent data
            data = self.fred.get_series(
                series_id,
                observation_start=(datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
            )

            if data.empty:
                logger.warning(f"No data for series {series_id}")
                return None

            # Get latest values
            data = data.dropna()
            if len(data) < lookback_periods:
                return None

            current_value = float(data.iloc[-1])
            previous_value = float(data.iloc[-lookback_periods])

            change = current_value - previous_value
            change_percent = (change / previous_value * 100) if previous_value != 0 else 0

            return MacroIndicator(
                name=series_info["name"],
                series_id=series_id,
                current_value=current_value,
                previous_value=previous_value,
                change=change,
                change_percent=change_percent,
                last_updated=data.index[-1].to_pydatetime(),
                unit=series_info["unit"],
                description=series_info["description"],
            )

        except Exception as e:
            logger.error(f"Error fetching {series_key}: {e}")
            return None

    def get_all_indicators(self) -> dict[str, MacroIndicator]:
        """Get all tracked macroeconomic indicators."""
        indicators = {}

        for key in self.SERIES:
            indicator = self.get_indicator(key)
            if indicator:
                indicators[key] = indicator

        return indicators

    def get_macro_summary(self) -> MacroSummary:
        """Get summary of key macroeconomic indicators."""
        return MacroSummary(
            fed_funds_rate=self.get_indicator("fed_funds_rate"),
            inflation_cpi=self.get_indicator("inflation_cpi"),
            unemployment_rate=self.get_indicator("unemployment_rate"),
            gdp_growth=self.get_indicator("gdp_growth"),
            treasury_10y=self.get_indicator("treasury_10y"),
            consumer_sentiment=self.get_indicator("consumer_sentiment"),
        )

    def get_rate_expectations(self) -> dict:
        """Analyze Fed rate direction based on recent data."""
        indicators = self.get_all_indicators()

        signals = {
            "hawkish": [],  # Suggests rate hikes
            "dovish": [],   # Suggests rate cuts
            "neutral": [],
        }

        # Analyze inflation
        cpi = indicators.get("inflation_cpi")
        if cpi:
            if cpi.change_percent > 0.3:  # Rising inflation
                signals["hawkish"].append(f"CPI rising ({cpi.change_percent:.1f}% change)")
            elif cpi.change_percent < -0.2:  # Falling inflation
                signals["dovish"].append(f"CPI falling ({cpi.change_percent:.1f}% change)")
            else:
                signals["neutral"].append("CPI stable")

        # Analyze unemployment
        unemp = indicators.get("unemployment_rate")
        if unemp:
            if unemp.current_value > 4.5:  # High unemployment
                signals["dovish"].append(f"Unemployment elevated at {unemp.current_value}%")
            elif unemp.current_value < 3.5:  # Very low unemployment
                signals["hawkish"].append(f"Tight labor market at {unemp.current_value}%")
            else:
                signals["neutral"].append(f"Unemployment moderate at {unemp.current_value}%")

        # Analyze GDP
        gdp = indicators.get("gdp_growth")
        if gdp:
            if gdp.current_value > 3:  # Strong growth
                signals["hawkish"].append(f"Strong GDP growth at {gdp.current_value}%")
            elif gdp.current_value < 1:  # Weak growth
                signals["dovish"].append(f"Weak GDP growth at {gdp.current_value}%")

        # Determine overall bias
        if len(signals["hawkish"]) > len(signals["dovish"]):
            bias = "hawkish"
        elif len(signals["dovish"]) > len(signals["hawkish"]):
            bias = "dovish"
        else:
            bias = "neutral"

        return {
            "bias": bias,
            "signals": signals,
            "fed_funds_current": indicators.get("fed_funds_rate"),
        }
