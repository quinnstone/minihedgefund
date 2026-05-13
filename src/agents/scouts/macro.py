"""Macro scout — regime classification + sector tilts from FRED indicators.

Deterministic rule-based classification of the macro backdrop. The synthesis
agent (Opus) can layer narrative on top, but the regime label itself is
derived here so it's reproducible from the same data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ...collectors.macro import MacroCollector


# Sector tilts by regime — bias only, the PM agent can still override.
REGIME_TILTS = {
    "expansion": {
        "favored": ["Technology", "Consumer Discretionary", "Industrials", "Financials"],
        "disfavored": ["Utilities", "Consumer Staples"],
    },
    "contraction": {
        "favored": ["Consumer Staples", "Utilities", "Health Care"],
        "disfavored": ["Consumer Discretionary", "Industrials", "Financials"],
    },
    "transition": {
        "favored": ["Health Care", "Technology"],
        "disfavored": [],
    },
    "rising_rates": {
        "favored": ["Financials", "Energy"],
        "disfavored": ["Utilities", "Real Estate"],
    },
    "falling_rates": {
        "favored": ["Real Estate", "Utilities", "Technology"],
        "disfavored": ["Financials"],
    },
}


def _classify_regime(indicators: dict) -> dict:
    """Rule-based regime classification.

    Returns: dict with overall regime, rate_trend, inflation_trend, growth_trend.
    """
    rate_trend = "stable"
    inflation_trend = "stable"
    growth_trend = "stable"

    fed = indicators.get("fed_funds_rate")
    if fed and fed.change is not None:
        if fed.change > 0.10:
            rate_trend = "rising"
        elif fed.change < -0.10:
            rate_trend = "falling"

    cpi = indicators.get("inflation_cpi")
    if cpi and cpi.change_percent is not None:
        if cpi.change_percent > 0.5:
            inflation_trend = "rising"
        elif cpi.change_percent < -0.5:
            inflation_trend = "falling"

    gdp = indicators.get("gdp_growth")
    if gdp and gdp.current_value is not None:
        if gdp.current_value < 1.0:
            growth_trend = "weak"
        elif gdp.current_value > 3.0:
            growth_trend = "strong"
        else:
            growth_trend = "moderate"

    # Overall classification
    if growth_trend == "weak" and inflation_trend != "rising":
        overall = "contraction"
    elif growth_trend == "strong" and inflation_trend != "rising":
        overall = "expansion"
    elif inflation_trend == "rising" and rate_trend == "rising":
        overall = "transition"
    elif growth_trend == "moderate":
        overall = "expansion"
    else:
        overall = "transition"

    favored: list[str] = list(REGIME_TILTS.get(overall, {}).get("favored", []))
    disfavored: list[str] = list(REGIME_TILTS.get(overall, {}).get("disfavored", []))

    if rate_trend == "rising":
        favored = list({*favored, *REGIME_TILTS["rising_rates"]["favored"]})
        disfavored = list({*disfavored, *REGIME_TILTS["rising_rates"]["disfavored"]})
    elif rate_trend == "falling":
        favored = list({*favored, *REGIME_TILTS["falling_rates"]["favored"]})
        disfavored = list({*disfavored, *REGIME_TILTS["falling_rates"]["disfavored"]})

    return {
        "overall_regime": overall,
        "rate_trend": rate_trend,
        "inflation_trend": inflation_trend,
        "growth_trend": growth_trend,
        "favored_sectors": favored,
        "disfavored_sectors": disfavored,
    }


def run_macro_scout(macro: MacroCollector) -> dict:
    indicators = macro.get_all_indicators()
    classification = _classify_regime(indicators)

    summary_indicators = {}
    for key in ("fed_funds_rate", "inflation_cpi", "unemployment_rate", "gdp_growth",
                "treasury_10y", "consumer_sentiment"):
        ind = indicators.get(key)
        if not ind:
            continue
        summary_indicators[key] = {
            "name": ind.name,
            "current_value": ind.current_value,
            "previous_value": ind.previous_value,
            "change": ind.change,
            "change_percent": ind.change_percent,
            "unit": ind.unit,
            "last_updated": ind.last_updated.isoformat() if ind.last_updated else None,
        }

    return {
        "scout": "macro",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "indicators": summary_indicators,
        "regime": classification,
        "degraded": not bool(indicators),
    }
