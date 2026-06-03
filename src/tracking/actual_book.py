"""Actual paper-money book — tracks real ThinkOrSwim entries alongside the simulation.

The simulated book (data/pick_scoreboard.json) tracks what the AI's picks
would produce at the Monday-10am-ET fill prices with 5bp slippage. The
actual book tracks what the user actually entered at — which may differ
materially (entry timing, price drift, decision overrides).

Comparing both vs SPY answers two distinct questions:
  Simulated vs SPY: does the strategy work in theory?
  Actual    vs SPY: is it working for the user in practice?

The actual book is purely additive — nothing here touches the simulation.
If data/actual_entries.txt doesn't exist, all functions return safe empties
and the Discord report behaves exactly as before.

File format
-----------
Plain text, one entry per line:

    YYYY-MM-DD  TICKER  buy|sell  SHARES  PRICE  [notes...]

Whitespace-tolerant (spaces or tabs). Comments start with `#`. Blank lines
ignored. Order doesn't matter — entries are sorted by date during build.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .persistence import DATA_DIR

logger = logging.getLogger(__name__)


ACTUAL_ENTRIES_FILE = DATA_DIR / "actual_entries.txt"
ACTUAL_BOOK_FILE = DATA_DIR / "actual_book.json"

INITIAL_CAPITAL = 10_000.0   # matches the simulation; allows direct compare


# ─── Data shapes ────────────────────────────────────────────────────────

@dataclass
class ActualEntry:
    """One line from data/actual_entries.txt — raw user input."""
    date: date
    ticker: str
    action: str       # "buy" or "sell"
    shares: float
    price: float
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "ticker": self.ticker,
            "action": self.action,
            "shares": self.shares,
            "price": self.price,
            "notes": self.notes,
        }


@dataclass
class ActualLot:
    """One open lot in the actual book. FIFO closing semantics."""
    ticker: str
    shares: float
    cost_basis_per_share: float
    acquisition_date: date


@dataclass
class ActualPosition:
    ticker: str
    lots: list[ActualLot] = field(default_factory=list)

    @property
    def total_shares(self) -> float:
        return sum(l.shares for l in self.lots)

    @property
    def total_cost_basis(self) -> float:
        return sum(l.shares * l.cost_basis_per_share for l in self.lots)

    @property
    def avg_cost_per_share(self) -> float:
        s = self.total_shares
        return self.total_cost_basis / s if s > 0 else 0.0


@dataclass
class ClosedTrade:
    ticker: str
    shares: float
    cost_basis_per_share: float
    sell_price: float
    acquisition_date: date
    sell_date: date

    @property
    def realized_pnl(self) -> float:
        return (self.sell_price - self.cost_basis_per_share) * self.shares


# ─── Parser ─────────────────────────────────────────────────────────────

class ParseError(ValueError):
    """Bad line in actual_entries.txt — caller decides to skip or stop."""


def parse_entries_file(path: Path = ACTUAL_ENTRIES_FILE) -> tuple[list[ActualEntry], list[str]]:
    """Parse the text file. Returns (entries, warnings).

    Lines that fail to parse are skipped with a warning rather than crashing
    the whole rebuild — one typo shouldn't lose the entire book.
    """
    entries: list[ActualEntry] = []
    warnings: list[str] = []

    if not path.exists():
        return entries, warnings

    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            entries.append(_parse_line(stripped))
        except ParseError as exc:
            warnings.append(f"line {lineno}: {exc} — {raw!r}")

    entries.sort(key=lambda e: (e.date, e.ticker, 0 if e.action == "buy" else 1))
    return entries, warnings


def _parse_line(line: str) -> ActualEntry:
    parts = line.split(maxsplit=5)
    if len(parts) < 5:
        raise ParseError(
            "expected at least 5 whitespace-separated fields: "
            "DATE TICKER buy|sell SHARES PRICE [notes...]"
        )
    raw_date, raw_ticker, raw_action, raw_shares, raw_price = parts[:5]
    notes = parts[5].strip() if len(parts) == 6 else ""

    try:
        entry_date = date.fromisoformat(raw_date)
    except ValueError:
        raise ParseError(f"bad date {raw_date!r}; expected YYYY-MM-DD")

    action = raw_action.lower()
    if action not in {"buy", "sell"}:
        raise ParseError(f"action {raw_action!r} must be 'buy' or 'sell'")

    try:
        shares = float(raw_shares)
        if shares <= 0:
            raise ValueError
    except ValueError:
        raise ParseError(f"shares {raw_shares!r} must be a positive number")

    try:
        price = float(raw_price)
        if price <= 0:
            raise ValueError
    except ValueError:
        raise ParseError(f"price {raw_price!r} must be a positive number")

    return ActualEntry(
        date=entry_date,
        ticker=raw_ticker.upper(),
        action=action,
        shares=shares,
        price=price,
        notes=notes,
    )


# ─── Builder ────────────────────────────────────────────────────────────

def build_actual_book(
    entries: list[ActualEntry],
    current_price_map: dict[str, float],
    spy_inception_price: Optional[float] = None,
    spy_current_price: Optional[float] = None,
    initial_capital: float = INITIAL_CAPITAL,
) -> dict:
    """Replay entries chronologically to produce the current book state.

    Buy: cash drops, new lot opened.
    Sell: lots closed FIFO (simpler than specific-ID for the user's book —
          the simulation uses specific-ID; the actual book just needs
          honest accounting).

    Allows cash to go negative if user over-deploys vs the $10k notional —
    surfaced in the report rather than hidden so it's visible.
    """
    # Defensive: sort internally so the build is correct regardless of input
    # order (parser already sorts, but build_actual_book is also called
    # directly from tests and could be from a future caller). Buys before
    # sells on the same day so sells against just-opened positions work.
    sorted_entries = sorted(
        entries,
        key=lambda e: (e.date, e.ticker, 0 if e.action == "buy" else 1),
    )

    cash = initial_capital
    positions: dict[str, ActualPosition] = {}
    closed: list[ClosedTrade] = []
    inception_date: Optional[date] = sorted_entries[0].date if sorted_entries else None

    for entry in sorted_entries:
        if entry.action == "buy":
            cash -= entry.shares * entry.price
            pos = positions.setdefault(entry.ticker, ActualPosition(ticker=entry.ticker))
            pos.lots.append(ActualLot(
                ticker=entry.ticker,
                shares=entry.shares,
                cost_basis_per_share=entry.price,
                acquisition_date=entry.date,
            ))
        else:   # sell
            pos = positions.get(entry.ticker)
            if pos is None or pos.total_shares <= 0:
                # Sell with no position — record as warning trade but skip
                continue
            cash += entry.shares * entry.price
            _close_fifo(pos, entry.shares, entry.price, entry.date, closed)
            if pos.total_shares <= 1e-9:
                del positions[entry.ticker]

    # Mark-to-market
    positions_value = 0.0
    position_views = []
    for ticker, pos in sorted(positions.items()):
        cur = current_price_map.get(ticker)
        mv = pos.total_shares * cur if cur else 0.0
        positions_value += mv
        position_views.append({
            "ticker": ticker,
            "shares": round(pos.total_shares, 6),
            "avg_cost_per_share": round(pos.avg_cost_per_share, 4),
            "current_price": round(cur, 4) if cur else None,
            "market_value": round(mv, 4),
            "unrealized_pnl": round(mv - pos.total_cost_basis, 4) if cur else None,
            "unrealized_pnl_pct": round((cur - pos.avg_cost_per_share) / pos.avg_cost_per_share, 6)
                if cur and pos.avg_cost_per_share > 0 else None,
            "n_lots": len(pos.lots),
            "oldest_acquisition": min(l.acquisition_date for l in pos.lots).isoformat(),
        })

    aum = cash + positions_value
    realized_pnl = sum(t.realized_pnl for t in closed)
    cum_return_pct = (aum - initial_capital) / initial_capital if initial_capital > 0 else 0.0

    spy_return_pct: Optional[float] = None
    alpha_pct: Optional[float] = None
    if spy_inception_price and spy_current_price and spy_inception_price > 0:
        spy_return_pct = (spy_current_price - spy_inception_price) / spy_inception_price
        alpha_pct = cum_return_pct - spy_return_pct

    return {
        "inception_date": inception_date.isoformat() if inception_date else None,
        "initial_capital": initial_capital,
        "cash": round(cash, 4),
        "positions_value": round(positions_value, 4),
        "current_aum": round(aum, 4),
        "cumulative_return_pct": round(cum_return_pct, 6),
        "cumulative_return_usd": round(aum - initial_capital, 4),
        "realized_pnl": round(realized_pnl, 4),
        "spy_inception_price": spy_inception_price,
        "spy_current_price": spy_current_price,
        "spy_return_from_inception_pct": round(spy_return_pct, 6) if spy_return_pct is not None else None,
        "alpha_pct": round(alpha_pct, 6) if alpha_pct is not None else None,
        "positions": position_views,
        "closed_trades_count": len(closed),
        "entries_count": len(entries),
        "leveraged": cash < -0.01,    # surfaced as flag if user over-deployed
    }


def _close_fifo(
    pos: ActualPosition,
    shares_to_close: float,
    sell_price: float,
    sell_date: date,
    closed_out: list[ClosedTrade],
) -> None:
    remaining = shares_to_close
    while remaining > 1e-9 and pos.lots:
        lot = pos.lots[0]
        take = min(lot.shares, remaining)
        closed_out.append(ClosedTrade(
            ticker=pos.ticker,
            shares=take,
            cost_basis_per_share=lot.cost_basis_per_share,
            sell_price=sell_price,
            acquisition_date=lot.acquisition_date,
            sell_date=sell_date,
        ))
        lot.shares -= take
        remaining -= take
        if lot.shares <= 1e-9:
            pos.lots.pop(0)


# ─── Persistence ────────────────────────────────────────────────────────

def save_actual_book(book: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTUAL_BOOK_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(book, indent=2, default=str))
    os.replace(tmp, ACTUAL_BOOK_FILE)


def load_actual_book() -> Optional[dict]:
    """Return the parsed actual book, or None if it doesn't exist yet.

    Returning None — rather than an empty dict — is intentional: the composer
    uses None to mean "user hasn't set up the actual book; skip the embed."
    """
    if not ACTUAL_BOOK_FILE.exists():
        return None
    try:
        return json.loads(ACTUAL_BOOK_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("actual_book.json corrupted; returning None")
        return None


# ─── Held-tickers helper (for fetching current prices) ──────────────────

def held_tickers_from_entries(entries: list[ActualEntry]) -> list[str]:
    """The set of tickers currently held (replay only — no price fetch).

    Used by the rebuild script to decide which prices to pull from yfinance.
    """
    shares_by_ticker: dict[str, float] = {}
    for e in sorted(entries, key=lambda x: x.date):
        delta = e.shares if e.action == "buy" else -e.shares
        shares_by_ticker[e.ticker] = shares_by_ticker.get(e.ticker, 0.0) + delta
    return sorted(t for t, s in shares_by_ticker.items() if s > 1e-9)
