"""Persistence layer — the audit trail.

All read/write of the data/ directory goes through here. Keeps the schema
versioned in one place. The directory layout is git-tracked, so each Monday's
new files become the audit history.

Files:
  data/portfolio_state.json        — single source of truth (cash + positions)
  data/scoreboard.json             — aggregate stats
  data/trades.jsonl                — append-only trade log
  data/decisions/{YYYY-MM-DD}.json — full decision audit (scouts→PM→executed)
  data/marks/{YYYY-MM-DD}.json     — weekly MTM of all positions
  data/reflections/{YYYY-MM-DD}.json — reflection agent output
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ..portfolio.state import ClosedLot, PortfolioState


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.json"
SCOREBOARD_FILE = DATA_DIR / "scoreboard.json"
TRADES_FILE = DATA_DIR / "trades.jsonl"
DECISIONS_DIR = DATA_DIR / "decisions"
MARKS_DIR = DATA_DIR / "marks"
REFLECTIONS_DIR = DATA_DIR / "reflections"


def _ensure_dirs() -> None:
    for d in (DATA_DIR, DECISIONS_DIR, MARKS_DIR, REFLECTIONS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: Any) -> None:
    _ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


# ---- Portfolio state ----

def load_portfolio_state(initial_capital: float = 10_000.00) -> PortfolioState:
    """Load saved state or create a fresh one with the given initial capital."""
    raw = _read_json(PORTFOLIO_STATE_FILE)
    if raw is None:
        return PortfolioState(
            cash=initial_capital,
            initial_capital=initial_capital,
            inception_date=date.today(),
        )
    return PortfolioState.from_dict(raw)


def save_portfolio_state(state: PortfolioState) -> None:
    state.last_updated = datetime.utcnow()
    _write_json(PORTFOLIO_STATE_FILE, state.to_dict())


# ---- Trades (append-only) ----

def append_trade(record: dict) -> None:
    _ensure_dirs()
    with TRADES_FILE.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def load_trades() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    out: list[dict] = []
    with TRADES_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def recent_closed_lots(days: int = 35) -> list[ClosedLot]:
    """Return ClosedLots from the trade log within the lookback window.

    Used by the tax brief for wash-sale detection (30-day window + buffer)."""
    cutoff = date.today().toordinal() - days
    out: list[ClosedLot] = []
    for t in load_trades():
        if t.get("kind") != "sell":
            continue
        sell_date_str = t.get("sell_date") or t.get("date")
        try:
            sd = date.fromisoformat(sell_date_str)
        except (TypeError, ValueError):
            continue
        if sd.toordinal() < cutoff:
            continue
        try:
            acq = date.fromisoformat(t.get("acquisition_date"))
        except (TypeError, ValueError):
            continue
        out.append(ClosedLot(
            lot_id=t.get("lot_id", ""),
            ticker=t.get("ticker", ""),
            shares_closed=float(t.get("shares", 0)),
            cost_basis_per_share=float(t.get("cost_basis_per_share", 0)),
            sell_price_per_share=float(t.get("sell_price_per_share", 0)),
            acquisition_date=acq,
            sell_date=sd,
        ))
    return out


# ---- Decisions / marks / reflections ----

def save_decision(d: date, payload: dict) -> Path:
    path = DECISIONS_DIR / f"{d.isoformat()}.json"
    _write_json(path, payload)
    return path


def load_decision(d: date) -> Optional[dict]:
    return _read_json(DECISIONS_DIR / f"{d.isoformat()}.json")


def load_recent_decisions(n_weeks: int = 4) -> list[dict]:
    """Most recent decision files (newest first), up to n_weeks."""
    if not DECISIONS_DIR.exists():
        return []
    files = sorted(DECISIONS_DIR.glob("*.json"), reverse=True)[:n_weeks]
    out = []
    for f in files:
        d = _read_json(f)
        if d:
            out.append(d)
    return out


def save_marks(d: date, payload: dict) -> Path:
    path = MARKS_DIR / f"{d.isoformat()}.json"
    _write_json(path, payload)
    return path


def load_recent_marks(n_weeks: int = 4) -> list[dict]:
    if not MARKS_DIR.exists():
        return []
    files = sorted(MARKS_DIR.glob("*.json"), reverse=True)[:n_weeks]
    out = []
    for f in files:
        d = _read_json(f)
        if d:
            out.append(d)
    return out


def save_reflection(d: date, payload: dict) -> Path:
    path = REFLECTIONS_DIR / f"{d.isoformat()}.json"
    _write_json(path, payload)
    return path


def load_latest_reflection() -> Optional[dict]:
    if not REFLECTIONS_DIR.exists():
        return None
    files = sorted(REFLECTIONS_DIR.glob("*.json"), reverse=True)
    return _read_json(files[0]) if files else None


# ---- Scoreboard ----

def load_scoreboard() -> dict:
    raw = _read_json(SCOREBOARD_FILE)
    if raw is None:
        return _empty_scoreboard()
    return raw


def save_scoreboard(payload: dict) -> None:
    _write_json(SCOREBOARD_FILE, payload)


def _empty_scoreboard() -> dict:
    return {
        "inception_date": None,
        "initial_capital": 10_000.00,
        "weeks_tracked": 0,
        "weekly_returns": [],
        "cumulative_return_pct": 0.0,
        "cumulative_return_usd": 0.0,
        "current_aum": 10_000.00,
        "weekly_win_rate": 0.0,
        "spy_cumulative_pct": 0.0,
        "cumulative_alpha_pct": 0.0,
        "trades_count": 0,
        "total_realized_gains": 0.0,
        "total_realized_losses": 0.0,
        "estimated_tax_owed": 0.0,
        "after_tax_cumulative_return_pct": 0.0,
    }
