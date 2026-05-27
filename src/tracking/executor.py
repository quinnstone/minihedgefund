"""Decision executor — translate PM decisions into actual trades.

Applies in safe order (CLOSE/TRIM first to free cash, then OPEN/ADD), enforces:
  - Schwab fractional rules (S&P 500 only; everything else whole-share)
  - Slippage (5 bps adverse per fill)
  - Tax-aware lot selection on sells (TaxEngine.AUTO: HIFO at loss, oldest-LT at gain)
  - Wash-sale hard block on OPEN/ADD
  - Single-name + sector caps (skip with a warning, don't crash)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..portfolio.schwab import SchwabRealism
from ..portfolio.state import ClosedLot, PortfolioState
from ..portfolio.tax import LotSelection, TaxEngine

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    kind: str           # "buy" | "sell"
    ticker: str
    shares: float
    price: float
    cost_basis_per_share: Optional[float] = None
    sell_price_per_share: Optional[float] = None
    acquisition_date: Optional[date] = None
    sell_date: Optional[date] = None
    realized_pnl: Optional[float] = None
    is_long_term: Optional[bool] = None
    lot_id: Optional[str] = None
    fractional_eligible: bool = False
    cash_residual: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ticker": self.ticker,
            "shares": round(self.shares, 6),
            "price": round(self.price, 4),
            "cost_basis_per_share": round(self.cost_basis_per_share, 4) if self.cost_basis_per_share is not None else None,
            "sell_price_per_share": round(self.sell_price_per_share, 4) if self.sell_price_per_share is not None else None,
            "acquisition_date": self.acquisition_date.isoformat() if self.acquisition_date else None,
            "sell_date": self.sell_date.isoformat() if self.sell_date else None,
            "realized_pnl": round(self.realized_pnl, 4) if self.realized_pnl is not None else None,
            "is_long_term": self.is_long_term,
            "lot_id": self.lot_id,
            "fractional_eligible": self.fractional_eligible,
            "cash_residual": round(self.cash_residual, 4),
            "notes": self.notes,
        }


@dataclass
class ExecutionResult:
    trades: list[TradeRecord] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # decisions skipped + reason
    realized_pnl: float = 0.0
    realized_gains: float = 0.0
    realized_losses: float = 0.0


def _sort_decisions(decisions: list[dict]) -> list[dict]:
    """CLOSE → TRIM → OPEN → ADD → HOLD → NONE."""
    order = {"CLOSE": 0, "TRIM": 1, "OPEN": 2, "ADD": 3, "HOLD": 4, "NONE": 5}
    return sorted(decisions, key=lambda d: order.get(d.get("action", "NONE"), 9))


def execute_decisions(
    decisions: list[dict],
    portfolio: PortfolioState,
    price_map: dict[str, float],
    wash_sale_blocks: set[str],
    schwab: SchwabRealism,
    tax_engine: TaxEngine,
    sector_map: dict[str, str],
    as_of: date,
    single_name_cap_pct: float = 0.20,
    sector_cap_pct: float = 0.40,
) -> ExecutionResult:
    """Apply each decision to the portfolio in safe order.

    The PM may produce conflicting or out-of-cap decisions; the executor's job
    is to honor what it can and surface what it skipped (with reasons) for
    the audit trail and Discord report.
    """
    result = ExecutionResult()
    decisions = _sort_decisions(decisions)

    for d in decisions:
        action = d.get("action")
        ticker = (d.get("ticker") or "").upper()
        thesis = d.get("thesis", "")

        if action in (None, "NONE", "HOLD"):
            continue

        price = price_map.get(ticker)
        if price is None or price <= 0:
            result.skipped.append({"ticker": ticker, "action": action, "reason": "no price"})
            continue

        if action == "CLOSE":
            _execute_sell(
                portfolio=portfolio, tax_engine=tax_engine, schwab=schwab,
                ticker=ticker, sell_fraction=1.0, price=price, as_of=as_of,
                thesis=thesis, result=result,
            )

        elif action == "TRIM":
            frac = d.get("trim_pct_of_position")
            if frac is None or frac <= 0:
                result.skipped.append({"ticker": ticker, "action": action, "reason": "no trim_pct_of_position"})
                continue
            _execute_sell(
                portfolio=portfolio, tax_engine=tax_engine, schwab=schwab,
                ticker=ticker, sell_fraction=min(1.0, frac / 100.0), price=price, as_of=as_of,
                thesis=thesis, result=result,
            )

        elif action == "OPEN":
            if ticker in wash_sale_blocks:
                result.skipped.append({"ticker": ticker, "action": action, "reason": "wash-sale block"})
                continue
            tgt = d.get("target_weight_pct")
            if tgt is None or tgt <= 0:
                result.skipped.append({"ticker": ticker, "action": action, "reason": "no target_weight_pct"})
                continue
            _execute_buy(
                portfolio=portfolio, schwab=schwab, price_map=price_map,
                ticker=ticker, target_weight_pct=tgt / 100.0, price=price, as_of=as_of,
                sector_map=sector_map,
                single_name_cap=single_name_cap_pct, sector_cap=sector_cap_pct,
                thesis=thesis, result=result,
            )

        elif action == "ADD":
            if ticker in wash_sale_blocks:
                result.skipped.append({"ticker": ticker, "action": action, "reason": "wash-sale block"})
                continue
            add = d.get("additional_weight_pct")
            if add is None or add <= 0:
                result.skipped.append({"ticker": ticker, "action": action, "reason": "no additional_weight_pct"})
                continue
            _execute_buy(
                portfolio=portfolio, schwab=schwab, price_map=price_map,
                ticker=ticker, target_weight_pct=add / 100.0, price=price, as_of=as_of,
                sector_map=sector_map,
                single_name_cap=single_name_cap_pct, sector_cap=sector_cap_pct,
                thesis=thesis, result=result,
                is_add=True,
            )

    return result


def _execute_sell(
    portfolio: PortfolioState,
    tax_engine: TaxEngine,
    schwab: SchwabRealism,
    ticker: str,
    sell_fraction: float,
    price: float,
    as_of: date,
    thesis: str,
    result: ExecutionResult,
) -> None:
    position = portfolio.positions.get(ticker)
    if not position or position.total_shares <= 0:
        result.skipped.append({"ticker": ticker, "action": "sell", "reason": "no position"})
        return

    target_shares = position.total_shares * sell_fraction

    fill = schwab.sell(ticker, target_shares, price)
    if fill.fill_shares <= 0:
        result.skipped.append({"ticker": ticker, "action": "sell", "reason": "rounded to zero shares"})
        return

    lot_plan = tax_engine.select_lots_to_sell(
        position, fill.fill_shares, fill.fill_price, as_of, LotSelection.AUTO,
    )
    closed_lots: list[ClosedLot] = portfolio.close_lots(
        ticker, lot_plan, fill.fill_price, as_of,
    )

    for cl in closed_lots:
        pnl = cl.realized_pnl
        result.realized_pnl += pnl
        if pnl > 0:
            result.realized_gains += pnl
        elif pnl < 0:
            result.realized_losses += pnl
        result.trades.append(TradeRecord(
            kind="sell",
            ticker=ticker,
            shares=cl.shares_closed,
            price=fill.fill_price,
            cost_basis_per_share=cl.cost_basis_per_share,
            sell_price_per_share=cl.sell_price_per_share,
            acquisition_date=cl.acquisition_date,
            sell_date=cl.sell_date,
            realized_pnl=pnl,
            is_long_term=cl.is_long_term,
            lot_id=cl.lot_id,
            fractional_eligible=fill.fractional_eligible,
            notes=thesis[:200],
        ))


def _sector_concentration_after(
    portfolio: PortfolioState,
    price_map: dict[str, float],
    sector_map: dict[str, str],
    sector_of_new: Optional[str],
    new_dollars: float,
) -> float:
    aum_after = portfolio.total_aum(price_map) + 0  # the buy reduces cash but adds equal MV
    if aum_after <= 0:
        return 0.0
    current = 0.0
    for ticker, pos in portfolio.positions.items():
        if sector_map.get(ticker) != sector_of_new:
            continue
        price = price_map.get(ticker)
        if price is None:
            continue
        current += pos.market_value(price)
    return (current + new_dollars) / aum_after


def _execute_buy(
    portfolio: PortfolioState,
    schwab: SchwabRealism,
    price_map: dict[str, float],
    ticker: str,
    target_weight_pct: float,
    price: float,
    as_of: date,
    sector_map: dict[str, str],
    single_name_cap: float,
    sector_cap: float,
    thesis: str,
    result: ExecutionResult,
    is_add: bool = False,
) -> None:
    aum = portfolio.total_aum(price_map)
    if aum <= 0:
        result.skipped.append({"ticker": ticker, "action": "buy", "reason": "AUM zero"})
        return

    target_dollars = target_weight_pct * aum

    # Single-name cap: cap target by the room remaining under the cap
    if not is_add:
        if target_weight_pct > single_name_cap:
            target_dollars = single_name_cap * aum
    else:
        existing = portfolio.positions.get(ticker)
        existing_value = existing.market_value(price) if existing else 0.0
        room = max(0.0, single_name_cap * aum - existing_value)
        target_dollars = min(target_dollars, room)
        if target_dollars <= 0:
            result.skipped.append({"ticker": ticker, "action": "ADD", "reason": "already at single-name cap"})
            return

    # Sector cap check
    sector = sector_map.get(ticker)
    if sector:
        projected_sector_pct = _sector_concentration_after(
            portfolio, price_map, sector_map, sector, target_dollars,
        )
        if projected_sector_pct > sector_cap:
            allowed_dollars = max(0.0, target_dollars - (projected_sector_pct - sector_cap) * aum)
            if allowed_dollars <= 0:
                result.skipped.append({"ticker": ticker, "action": "buy", "reason": f"sector cap ({sector})"})
                return
            target_dollars = allowed_dollars

    if target_dollars < 1.0:
        result.skipped.append({"ticker": ticker, "action": "buy", "reason": "target below $1"})
        return

    if target_dollars > portfolio.cash:
        target_dollars = portfolio.cash

    if target_dollars <= 0:
        result.skipped.append({"ticker": ticker, "action": "buy", "reason": "no cash"})
        return

    fill = schwab.buy(ticker, target_dollars, price)

    # Round-up retry for non-fractional names whose 1 share costs more than
    # our dollar target. Without this, a $600 QQQ target at ~$625/share is
    # silently dropped (floor = 0 shares) and the PM's intent is lost. A real
    # execution desk would just buy 1 share — accept a small overshoot vs.
    # zero deployment.
    if fill.fill_shares <= 0 and not fill.fractional_eligible:
        one_share_cost = fill.fill_price
        can_round_up = (
            target_dollars >= 0.5 * one_share_cost   # PM intent was meaningful
            and one_share_cost <= portfolio.cash     # we can afford it
            and one_share_cost <= single_name_cap * aum  # doesn't blow single-name cap
        )
        if can_round_up and sector:
            # Re-check sector cap with the (larger) single-share size
            projected = _sector_concentration_after(
                portfolio, price_map, sector_map, sector, one_share_cost,
            )
            if projected > sector_cap:
                can_round_up = False

        if can_round_up:
            fill = schwab.buy(ticker, one_share_cost, price)

    if fill.fill_shares <= 0:
        # Still zero after round-up consideration. Surface the share price
        # in the reason so the diagnostic is self-explanatory.
        reason = (
            f"fill shares = 0 (target ${target_dollars:.0f} < 1 share at ${fill.fill_price:.0f})"
            if not fill.fractional_eligible
            else "fill shares = 0"
        )
        result.skipped.append({"ticker": ticker, "action": "buy", "reason": reason})
        return

    lot = portfolio.open_lot(ticker, fill.fill_shares, fill.fill_price, as_of)
    result.trades.append(TradeRecord(
        kind="buy",
        ticker=ticker,
        shares=fill.fill_shares,
        price=fill.fill_price,
        cost_basis_per_share=fill.fill_price,
        acquisition_date=as_of,
        lot_id=lot.lot_id,
        fractional_eligible=fill.fractional_eligible,
        cash_residual=fill.cash_residual,
        notes=thesis[:200],
    ))
