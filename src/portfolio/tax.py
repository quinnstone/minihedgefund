"""Tax engine: STCG/LTCG, wash-sale guard, LTCG proximity, TLH surfacing.

Tuned for a $140k single filer in NYC (federal 24% bracket, NY 6.85%,
NYC 3.876%). NIIT (3.8%) doesn't apply until MAGI > $200k.

References:
- IRC §1222 (holding period: long-term = >1 year, so days_held >= 366
  qualifies, but conservative practice uses >= 365)
- IRC §1091 (wash sale rule: 30 days before or after sale at a loss)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional

from .state import ClosedLot, Lot, Position


# Holding-period cutoffs (calendar days)
LTCG_HOLDING_DAYS = 365
LTCG_PROXIMITY_DAYS = 330  # flag a lot to hold ~35 more days for LT treatment

# Wash-sale window (calendar days, both sides of the sale date)
WASH_SALE_WINDOW = 30

# Year-end TLH window (Nov 1 through Dec 31)
TLH_WINDOW_START_MONTH = 11


@dataclass(frozen=True)
class TaxBrackets:
    """Marginal rates for the relevant filer. Federal LTCG bracket is
    looked up separately because it's regime-based, not marginal-additive."""

    federal_ordinary: float
    federal_ltcg: float
    state: float
    city: float
    niit_applies: bool = False  # 3.8% net investment income tax

    @property
    def stcg_rate(self) -> float:
        """Short-term gains: ordinary federal + state + city + NIIT if applicable."""
        return self.federal_ordinary + self.state + self.city + (0.038 if self.niit_applies else 0.0)

    @property
    def ltcg_rate(self) -> float:
        """Long-term gains: preferential federal + state + city + NIIT."""
        return self.federal_ltcg + self.state + self.city + (0.038 if self.niit_applies else 0.0)


# Default profile: $140k single filer in NYC (2026 federal brackets)
NYC_140K_SINGLE = TaxBrackets(
    federal_ordinary=0.24,
    federal_ltcg=0.15,
    state=0.0685,
    city=0.03876,
    niit_applies=False,
)


class LotSelection(str, Enum):
    """Strategy for choosing which lots to sell when closing a position."""

    AUTO = "auto"          # tax-optimal: HIFO if at a loss, oldest-LT if at a gain
    HIFO = "hifo"          # highest cost basis first (max loss / min gain)
    FIFO = "fifo"          # oldest first
    LTCG_FIRST = "ltcg_first"  # prefer LT-qualified lots regardless of basis
    LIFO = "lifo"          # newest first


@dataclass
class WashSaleCheck:
    """Result of a wash-sale check for a proposed buy."""

    is_wash_sale_risk: bool
    blocking_close_date: Optional[date]
    blocking_realized_loss: float
    days_until_clear: int


@dataclass
class TLHCandidate:
    """An open position with unrealized losses worth harvesting."""

    ticker: str
    shares: float
    unrealized_loss: float
    estimated_tax_savings: float
    is_long_term: bool


@dataclass
class LTCGProximity:
    """A lot approaching the long-term threshold."""

    lot_id: str
    ticker: str
    days_held: int
    days_to_long_term: int
    shares: float
    unrealized_pnl: float
    extra_tax_if_sold_now: float


class TaxEngine:
    def __init__(self, brackets: TaxBrackets = NYC_140K_SINGLE):
        self.brackets = brackets

    # ---- Classification ----

    def is_long_term(self, acquisition_date: date, sell_date: date) -> bool:
        return (sell_date - acquisition_date).days >= LTCG_HOLDING_DAYS

    def estimated_tax(self, realized_pnl: float, is_long_term: bool) -> float:
        """Estimated federal+state+city tax on a single realized gain.

        Returns 0 for losses (no tax owed; the loss offsets other gains
        elsewhere — that's TLH territory, not per-trade modeling)."""
        if realized_pnl <= 0:
            return 0.0
        rate = self.brackets.ltcg_rate if is_long_term else self.brackets.stcg_rate
        return realized_pnl * rate

    def after_tax_pnl(self, closed: ClosedLot) -> float:
        """Net of estimated tax. Losses are returned as-is (full loss for TLH)."""
        gross = closed.realized_pnl
        tax = self.estimated_tax(gross, closed.is_long_term)
        return gross - tax

    # ---- Lot selection ----

    def select_lots_to_sell(
        self,
        position: Position,
        shares_to_sell: float,
        sell_price: float,
        sell_date: date,
        strategy: LotSelection = LotSelection.AUTO,
    ) -> list[tuple[str, float]]:
        """Return [(lot_id, shares_from_lot)] to satisfy the sell, by strategy.

        AUTO picks per-sell: if the average lot would close at a loss, use HIFO
        (maximize the realized loss for TLH); else prefer the oldest
        LTCG-qualified lot (minimize tax via preferential rate + FIFO drift)."""
        if shares_to_sell <= 0:
            return []
        if shares_to_sell > position.total_shares + 1e-9:
            raise ValueError(
                f"can't sell {shares_to_sell} of {position.ticker}; "
                f"only {position.total_shares} held"
            )

        ordered = self._order_lots(position, sell_price, sell_date, strategy)

        result: list[tuple[str, float]] = []
        remaining = shares_to_sell
        for lot in ordered:
            if remaining <= 1e-9:
                break
            take = min(lot.shares, remaining)
            result.append((lot.lot_id, take))
            remaining -= take

        return result

    def _order_lots(
        self,
        position: Position,
        sell_price: float,
        sell_date: date,
        strategy: LotSelection,
    ) -> list[Lot]:
        lots = list(position.lots)

        if strategy == LotSelection.FIFO:
            return sorted(lots, key=lambda l: l.acquisition_date)
        if strategy == LotSelection.LIFO:
            return sorted(lots, key=lambda l: l.acquisition_date, reverse=True)
        if strategy == LotSelection.HIFO:
            return sorted(lots, key=lambda l: l.cost_basis_per_share, reverse=True)
        if strategy == LotSelection.LTCG_FIRST:
            return sorted(
                lots,
                key=lambda l: (
                    not self.is_long_term(l.acquisition_date, sell_date),
                    l.acquisition_date,
                ),
            )

        # AUTO
        avg_basis = position.avg_cost_per_share
        if sell_price < avg_basis:
            return sorted(lots, key=lambda l: l.cost_basis_per_share, reverse=True)
        return sorted(
            lots,
            key=lambda l: (
                not self.is_long_term(l.acquisition_date, sell_date),
                l.acquisition_date,
            ),
        )

    # ---- Wash sale guard ----

    def check_wash_sale(
        self,
        ticker: str,
        proposed_buy_date: date,
        recent_closed_lots: list[ClosedLot],
    ) -> WashSaleCheck:
        """Block (or flag) a buy if any same-ticker loss closed within ±30 days.

        Conservative: we treat any prior loss in `recent_closed_lots` for the
        ticker within the window as blocking. Real wash-sale enforcement also
        defers the loss into the new lot's basis; this engine just refuses
        the buy and surfaces the reason."""
        threshold = proposed_buy_date - timedelta(days=WASH_SALE_WINDOW)

        worst: Optional[ClosedLot] = None
        for c in recent_closed_lots:
            if c.ticker != ticker:
                continue
            if c.sell_date < threshold or c.sell_date > proposed_buy_date:
                continue
            if c.realized_pnl >= 0:
                continue
            if worst is None or c.realized_pnl < worst.realized_pnl:
                worst = c

        if worst is None:
            return WashSaleCheck(
                is_wash_sale_risk=False,
                blocking_close_date=None,
                blocking_realized_loss=0.0,
                days_until_clear=0,
            )

        clear_date = worst.sell_date + timedelta(days=WASH_SALE_WINDOW + 1)
        days_until_clear = max(0, (clear_date - proposed_buy_date).days)
        return WashSaleCheck(
            is_wash_sale_risk=True,
            blocking_close_date=worst.sell_date,
            blocking_realized_loss=worst.realized_pnl,
            days_until_clear=days_until_clear,
        )

    # ---- LTCG proximity ----

    def ltcg_proximity_flags(
        self,
        position: Position,
        as_of: date,
        current_price: float,
    ) -> list[LTCGProximity]:
        """Lots that would qualify for LTCG treatment within ~35 days.

        Closing now incurs STCG; holding to LT saves (stcg_rate - ltcg_rate)
        on the gain. We surface that delta so the PM can weigh patience vs
        exit conviction."""
        flags: list[LTCGProximity] = []
        for lot in position.lots:
            days = lot.holding_period_days(as_of)
            if days < LTCG_PROXIMITY_DAYS or days >= LTCG_HOLDING_DAYS:
                continue
            unrealized = lot.unrealized_pnl(current_price)
            if unrealized <= 0:
                continue
            extra_tax = unrealized * (self.brackets.stcg_rate - self.brackets.ltcg_rate)
            flags.append(LTCGProximity(
                lot_id=lot.lot_id,
                ticker=position.ticker,
                days_held=days,
                days_to_long_term=LTCG_HOLDING_DAYS - days,
                shares=lot.shares,
                unrealized_pnl=unrealized,
                extra_tax_if_sold_now=extra_tax,
            ))
        return flags

    # ---- Tax-loss harvesting ----

    def tlh_candidates(
        self,
        positions: dict[str, Position],
        price_map: dict[str, float],
        as_of: date,
        min_loss: float = 25.0,
    ) -> list[TLHCandidate]:
        """Open positions with unrealized losses big enough to be worth harvesting.

        Year-round; PM can also gate on the year-end window."""
        out: list[TLHCandidate] = []
        for ticker, pos in positions.items():
            price = price_map.get(ticker)
            if price is None:
                continue
            unreal = pos.unrealized_pnl(price)
            if unreal >= -min_loss:
                continue
            oldest = pos.oldest_lot_date()
            is_lt = oldest is not None and (as_of - oldest).days >= LTCG_HOLDING_DAYS
            rate = self.brackets.ltcg_rate if is_lt else self.brackets.stcg_rate
            savings = abs(unreal) * rate
            out.append(TLHCandidate(
                ticker=ticker,
                shares=pos.total_shares,
                unrealized_loss=unreal,
                estimated_tax_savings=savings,
                is_long_term=is_lt,
            ))
        return sorted(out, key=lambda c: c.estimated_tax_savings, reverse=True)

    @staticmethod
    def in_year_end_tlh_window(as_of: date) -> bool:
        """Mid-November onward: surface TLH opportunities aggressively."""
        return as_of.month >= TLH_WINDOW_START_MONTH
