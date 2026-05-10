"""Market data collector using Yahoo Finance."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class StockData:
    """Stock price and performance data."""
    ticker: str
    name: str
    current_price: float
    previous_close: float
    change_percent: float
    volume: int
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None


@dataclass
class EarningsData:
    """Earnings report data."""
    ticker: str
    company_name: str
    report_date: datetime
    eps_estimate: Optional[float] = None
    eps_actual: Optional[float] = None
    surprise_percent: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_actual: Optional[float] = None


@dataclass
class SectorPerformance:
    """Sector ETF performance data."""
    sector: str
    etf_ticker: str
    change_percent_1d: float
    change_percent_1w: float
    change_percent_1m: float


@dataclass
class IndexData:
    """Major market index data."""
    name: str
    ticker: str
    current_value: float
    change_percent: float
    ytd_change_percent: float


class MarketCollector:
    """Collects market data from Yahoo Finance."""

    # Major indices
    INDICES = {
        "S&P 500": "^GSPC",
        "Dow Jones": "^DJI",
        "NASDAQ": "^IXIC",
        "Russell 2000": "^RUT",
        "VIX": "^VIX",
    }

    # Sector ETFs
    SECTOR_ETFS = {
        "Technology": "XLK",
        "Financial": "XLF",
        "Healthcare": "XLV",
        "Energy": "XLE",
        "Consumer Discretionary": "XLY",
        "Consumer Staples": "XLP",
        "Industrial": "XLI",
        "Materials": "XLB",
        "Utilities": "XLU",
        "Real Estate": "XLRE",
        "Communication": "XLC",
    }

    def get_stock_data(self, ticker: str) -> Optional[StockData]:
        """Get current stock data for a ticker."""
        try:
            stock = yf.Ticker(ticker)
            info = stock.info

            if not info or "regularMarketPrice" not in info:
                return None

            current_price = info.get("regularMarketPrice", 0)
            previous_close = info.get("regularMarketPreviousClose", current_price)

            if previous_close > 0:
                change_percent = ((current_price - previous_close) / previous_close) * 100
            else:
                change_percent = 0

            return StockData(
                ticker=ticker,
                name=info.get("shortName", ticker),
                current_price=current_price,
                previous_close=previous_close,
                change_percent=change_percent,
                volume=info.get("regularMarketVolume", 0),
                market_cap=info.get("marketCap"),
                pe_ratio=info.get("trailingPE"),
                fifty_two_week_high=info.get("fiftyTwoWeekHigh"),
                fifty_two_week_low=info.get("fiftyTwoWeekLow"),
            )

        except Exception as e:
            logger.error(f"Error getting stock data for {ticker}: {e}")
            return None

    def get_multiple_stocks(self, tickers: list[str]) -> list[StockData]:
        """Get data for multiple stocks."""
        stocks = []
        for ticker in tickers:
            data = self.get_stock_data(ticker)
            if data:
                stocks.append(data)
        return stocks

    def get_index_data(self) -> list[IndexData]:
        """Get major index performance data."""
        indices = []

        for name, ticker in self.INDICES.items():
            try:
                index = yf.Ticker(ticker)
                hist = index.history(period="1y")

                if hist.empty:
                    continue

                current = hist["Close"].iloc[-1]
                prev_close = hist["Close"].iloc[-2] if len(hist) > 1 else current
                ytd_start = hist["Close"].iloc[0]

                change_percent = ((current - prev_close) / prev_close) * 100 if prev_close else 0
                ytd_change = ((current - ytd_start) / ytd_start) * 100 if ytd_start else 0

                indices.append(IndexData(
                    name=name,
                    ticker=ticker,
                    current_value=current,
                    change_percent=change_percent,
                    ytd_change_percent=ytd_change,
                ))

            except Exception as e:
                logger.error(f"Error getting index data for {name}: {e}")
                continue

        return indices

    def get_sector_performance(self) -> list[SectorPerformance]:
        """Get sector ETF performance data."""
        sectors = []

        for sector, etf in self.SECTOR_ETFS.items():
            try:
                ticker = yf.Ticker(etf)
                hist = ticker.history(period="1mo")

                if hist.empty or len(hist) < 2:
                    continue

                current = hist["Close"].iloc[-1]
                prev_day = hist["Close"].iloc[-2]
                week_ago = hist["Close"].iloc[-5] if len(hist) >= 5 else hist["Close"].iloc[0]
                month_ago = hist["Close"].iloc[0]

                sectors.append(SectorPerformance(
                    sector=sector,
                    etf_ticker=etf,
                    change_percent_1d=((current - prev_day) / prev_day) * 100,
                    change_percent_1w=((current - week_ago) / week_ago) * 100,
                    change_percent_1m=((current - month_ago) / month_ago) * 100,
                ))

            except Exception as e:
                logger.error(f"Error getting sector data for {sector}: {e}")
                continue

        return sectors

    def get_earnings_calendar(
        self,
        days_back: int = 7,
        days_forward: int = 7,
    ) -> list[EarningsData]:
        """Get recent and upcoming earnings."""
        # yfinance doesn't have great earnings calendar support
        # This would need a supplementary data source in production
        # For now, we'll get earnings from specific tickers

        major_tickers = [
            "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
            "JPM", "BAC", "WFC", "GS", "MS",
            "JNJ", "UNH", "PFE", "MRK",
            "XOM", "CVX", "COP",
        ]

        earnings = []
        for ticker in major_tickers:
            try:
                stock = yf.Ticker(ticker)
                info = stock.info

                # Check if there's upcoming earnings
                earnings_date = info.get("earningsDate")
                if earnings_date:
                    # earnings_date is sometimes a list
                    if isinstance(earnings_date, list) and earnings_date:
                        report_date = datetime.fromtimestamp(earnings_date[0])
                    elif isinstance(earnings_date, (int, float)):
                        report_date = datetime.fromtimestamp(earnings_date)
                    else:
                        continue

                    earnings.append(EarningsData(
                        ticker=ticker,
                        company_name=info.get("shortName", ticker),
                        report_date=report_date,
                        eps_estimate=info.get("epsForward"),
                    ))

            except Exception as e:
                logger.debug(f"No earnings data for {ticker}: {e}")
                continue

        # Sort by date
        earnings.sort(key=lambda e: e.report_date)
        return earnings

    def get_market_movers(self, tickers: list[str], top_n: int = 10) -> dict:
        """Get top gainers and losers from a list of tickers."""
        stocks = self.get_multiple_stocks(tickers)

        # Sort by change percent
        sorted_stocks = sorted(stocks, key=lambda s: s.change_percent, reverse=True)

        return {
            "gainers": sorted_stocks[:top_n],
            "losers": sorted_stocks[-top_n:][::-1],
        }
