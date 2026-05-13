"""Portfolio state: tax lots, positions, and persistent AUM tracking.

Lots are the unit of cost-basis accounting. Each buy creates a new Lot. Sells
specify which lots to close (specific-ID method), preserving holding-period
and cost-basis information needed for accurate tax treatment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


def _new_lot_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Lot:
    """A specific-ID tax lot. One per buy execution."""

    lot_id: str
    ticker: str
    shares: float
    cost_basis_per_share: float
    acquisition_date: date

    @property
    def total_cost(self) -> float:
        return self.shares * self.cost_basis_per_share

    def market_value(self, current_price: float) -> float:
        return self.shares * current_price

    def unrealized_pnl(self, current_price: float) -> float:
        return self.market_value(current_price) - self.total_cost

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.cost_basis_per_share <= 0:
            return 0.0
        return (current_price - self.cost_basis_per_share) / self.cost_basis_per_share

    def holding_period_days(self, as_of: date) -> int:
        return (as_of - self.acquisition_date).days

    def to_dict(self) -> dict:
        return {
            "lot_id": self.lot_id,
            "ticker": self.ticker,
            "shares": self.shares,
            "cost_basis_per_share": self.cost_basis_per_share,
            "acquisition_date": self.acquisition_date.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Lot":
        return cls(
            lot_id=d["lot_id"],
            ticker=d["ticker"],
            shares=d["shares"],
            cost_basis_per_share=d["cost_basis_per_share"],
            acquisition_date=date.fromisoformat(d["acquisition_date"]),
        )


@dataclass
class Position:
    """All open lots for a single ticker."""

    ticker: str
    lots: list[Lot] = field(default_factory=list)

    @property
    def total_shares(self) -> float:
        return sum(lot.shares for lot in self.lots)

    @property
    def total_cost_basis(self) -> float:
        return sum(lot.total_cost for lot in self.lots)

    @property
    def avg_cost_per_share(self) -> float:
        s = self.total_shares
        return self.total_cost_basis / s if s > 0 else 0.0

    def market_value(self, current_price: float) -> float:
        return self.total_shares * current_price

    def unrealized_pnl(self, current_price: float) -> float:
        return self.market_value(current_price) - self.total_cost_basis

    def unrealized_pnl_pct(self, current_price: float) -> float:
        cb = self.total_cost_basis
        return (self.market_value(current_price) - cb) / cb if cb > 0 else 0.0

    def oldest_lot_date(self) -> Optional[date]:
        return min((lot.acquisition_date for lot in self.lots), default=None)

    def days_held(self, as_of: date) -> int:
        oldest = self.oldest_lot_date()
        return (as_of - oldest).days if oldest else 0

    def add_lot(self, lot: Lot) -> None:
        if lot.ticker != self.ticker:
            raise ValueError(f"Lot ticker {lot.ticker} != position ticker {self.ticker}")
        self.lots.append(lot)

    def remove_lot(self, lot_id: str) -> Optional[Lot]:
        for i, lot in enumerate(self.lots):
            if lot.lot_id == lot_id:
                return self.lots.pop(i)
        return None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "lots": [lot.to_dict() for lot in self.lots],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            ticker=d["ticker"],
            lots=[Lot.from_dict(lot_dict) for lot_dict in d.get("lots", [])],
        )


@dataclass
class ClosedLot:
    """The output of a sell — preserves all info needed for tax reporting."""

    lot_id: str
    ticker: str
    shares_closed: float
    cost_basis_per_share: float
    sell_price_per_share: float
    acquisition_date: date
    sell_date: date

    @property
    def proceeds(self) -> float:
        return self.shares_closed * self.sell_price_per_share

    @property
    def cost_basis(self) -> float:
        return self.shares_closed * self.cost_basis_per_share

    @property
    def realized_pnl(self) -> float:
        return self.proceeds - self.cost_basis

    @property
    def holding_period_days(self) -> int:
        return (self.sell_date - self.acquisition_date).days

    @property
    def is_long_term(self) -> bool:
        return self.holding_period_days >= 365

    def to_dict(self) -> dict:
        return {
            "lot_id": self.lot_id,
            "ticker": self.ticker,
            "shares_closed": self.shares_closed,
            "cost_basis_per_share": self.cost_basis_per_share,
            "sell_price_per_share": self.sell_price_per_share,
            "acquisition_date": self.acquisition_date.isoformat(),
            "sell_date": self.sell_date.isoformat(),
            "realized_pnl": self.realized_pnl,
            "holding_period_days": self.holding_period_days,
            "is_long_term": self.is_long_term,
        }


@dataclass
class PortfolioState:
    """The persistent state of the simulated portfolio across weeks."""

    cash: float = 10_000.00
    positions: dict[str, Position] = field(default_factory=dict)
    initial_capital: float = 10_000.00
    inception_date: Optional[date] = None
    last_updated: Optional[datetime] = None

    def get_or_create_position(self, ticker: str) -> Position:
        if ticker not in self.positions:
            self.positions[ticker] = Position(ticker=ticker)
        return self.positions[ticker]

    def open_lot(
        self,
        ticker: str,
        shares: float,
        price_per_share: float,
        acquisition_date: date,
    ) -> Lot:
        if shares <= 0:
            raise ValueError(f"shares must be > 0, got {shares}")
        cost = shares * price_per_share
        if cost > self.cash + 1e-6:
            raise ValueError(f"insufficient cash: need {cost:.2f}, have {self.cash:.2f}")

        lot = Lot(
            lot_id=_new_lot_id(),
            ticker=ticker,
            shares=shares,
            cost_basis_per_share=price_per_share,
            acquisition_date=acquisition_date,
        )
        self.get_or_create_position(ticker).add_lot(lot)
        self.cash -= cost
        return lot

    def close_lots(
        self,
        ticker: str,
        lot_shares: list[tuple[str, float]],
        sell_price_per_share: float,
        sell_date: date,
    ) -> list[ClosedLot]:
        """Sell specific share quantities from specific lots.

        lot_shares: list of (lot_id, shares_to_close) tuples.
        Returns the ClosedLot records and credits proceeds to cash.
        Empty positions are auto-removed.
        """
        position = self.positions.get(ticker)
        if not position:
            raise ValueError(f"no position in {ticker}")

        closed: list[ClosedLot] = []
        for lot_id, shares_to_close in lot_shares:
            lot = next((l for l in position.lots if l.lot_id == lot_id), None)
            if lot is None:
                raise ValueError(f"lot {lot_id} not found in {ticker}")
            if shares_to_close > lot.shares + 1e-9:
                raise ValueError(
                    f"can't close {shares_to_close} shares from lot {lot_id} "
                    f"with only {lot.shares} shares"
                )

            closed.append(ClosedLot(
                lot_id=lot.lot_id,
                ticker=ticker,
                shares_closed=shares_to_close,
                cost_basis_per_share=lot.cost_basis_per_share,
                sell_price_per_share=sell_price_per_share,
                acquisition_date=lot.acquisition_date,
                sell_date=sell_date,
            ))

            lot.shares -= shares_to_close
            if lot.shares <= 1e-9:
                position.remove_lot(lot_id)

        self.cash += sum(c.proceeds for c in closed)

        if not position.lots:
            del self.positions[ticker]

        return closed

    def total_aum(self, price_map: dict[str, float]) -> float:
        positions_value = sum(
            pos.market_value(price_map[ticker])
            for ticker, pos in self.positions.items()
            if ticker in price_map
        )
        return self.cash + positions_value

    def total_return_pct(self, price_map: dict[str, float]) -> float:
        if self.initial_capital <= 0:
            return 0.0
        return (self.total_aum(price_map) - self.initial_capital) / self.initial_capital

    def cash_pct(self, price_map: dict[str, float]) -> float:
        aum = self.total_aum(price_map)
        return self.cash / aum if aum > 0 else 1.0

    def position_weights(self, price_map: dict[str, float]) -> dict[str, float]:
        aum = self.total_aum(price_map)
        if aum <= 0:
            return {}
        return {
            ticker: pos.market_value(price_map[ticker]) / aum
            for ticker, pos in self.positions.items()
            if ticker in price_map
        }

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {t: p.to_dict() for t, p in self.positions.items()},
            "initial_capital": self.initial_capital,
            "inception_date": self.inception_date.isoformat() if self.inception_date else None,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        return cls(
            cash=d.get("cash", 10_000.00),
            positions={t: Position.from_dict(p) for t, p in d.get("positions", {}).items()},
            initial_capital=d.get("initial_capital", 10_000.00),
            inception_date=date.fromisoformat(d["inception_date"]) if d.get("inception_date") else None,
            last_updated=datetime.fromisoformat(d["last_updated"]) if d.get("last_updated") else None,
        )
