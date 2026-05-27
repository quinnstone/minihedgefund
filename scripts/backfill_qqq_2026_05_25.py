"""One-off backfill for the QQQ position the executor skipped on 2026-05-25.

Background
----------
On Monday 2026-05-25, the PM agent recommended OPEN $QQQ at 6% target weight
(MED conviction). The executor floored to 0 shares because QQQ is an ETF
(not S&P 500 → not Schwab Stock Slices fractional eligible) and $600 target
< 1 share at ~$625. The skip was visible in diagnostics but the position
never entered the book.

The executor was fixed in commit bd9156f (now rounds up to 1 share when
intent is meaningful + caps allow). This script applies the same logic
retroactively to 2026-05-25's records so the audit trail reflects what
would have happened with the fixed code.

What this updates
-----------------
  data/portfolio_state.json  — adds QQQ lot
  data/trades.jsonl          — appends buy trade with backfill note
  data/marks/2026-05-25.json — re-marks Monday's snapshot with QQQ included
  data/pick_scoreboard.json  — adds Pick entry with rec_market_price = Mon close
  data/scoreboard.json       — updates current_aum + cumulative metrics
  data/decisions/2026-05-25.json — appends `backfill_audit` field

Run once. Idempotent (skips if QQQ already in portfolio_state).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import yfinance as yf

# Make src/ importable when run from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.portfolio.schwab import SchwabRealism                            # noqa: E402
from src.portfolio.state import PortfolioState                            # noqa: E402
from src.tracking.executor import TradeRecord                             # noqa: E402
from src.tracking.marking import mark_portfolio                           # noqa: E402
from src.tracking.persistence import (                                    # noqa: E402
    DECISIONS_DIR, MARKS_DIR, PORTFOLIO_STATE_FILE,
    SCOREBOARD_FILE, TRADES_FILE,
    append_trade, load_portfolio_state, load_scoreboard,
    save_marks, save_portfolio_state, save_scoreboard,
)
from src.tracking.pick_tracker import (                                   # noqa: E402
    Pick, _new_pick_id, PICK_SCOREBOARD_FILE,
    compute_aggregate, load_pick_scoreboard, save_pick_scoreboard,
    update_weekly_recaps,
)
from src.tracking.scoreboard import update_scoreboard, compute_realized_tax_totals  # noqa: E402
from src.portfolio.tax import TaxEngine                                   # noqa: E402

BACKFILL_DATE = date(2026, 5, 25)
TICKER = "QQQ"
TARGET_WEIGHT_PCT = 6.0
CONVICTION = "medium"
THESIS = (
    "Per reflection lesson — capture AI/tech beta with a small index sleeve "
    "since single-name conviction is concentrated; news 96 confirms. "
    "[Backfilled after executor round-up fix; original Monday cycle skipped this "
    "OPEN due to a now-fixed bug where non-fractional buys with target < 1 share "
    "floored to 0 shares.]"
)
BACKFILL_NOTE = "BACKFILL — executor round-up fix (commit bd9156f); restored 2026-05-26"


def _monday_close_price() -> float:
    """Get the actual 2026-05-25 close from yfinance — truer to the original
    PM call price than today's intraday."""
    hist = yf.Ticker(TICKER).history(
        start=BACKFILL_DATE.isoformat(),
        end=(BACKFILL_DATE.replace(day=BACKFILL_DATE.day + 2)).isoformat(),
        auto_adjust=True,
    )
    if hist is None or hist.empty:
        raise RuntimeError(f"could not fetch {TICKER} history around {BACKFILL_DATE}")
    return float(hist["Close"].iloc[0])


def main() -> int:
    print(f"=== Backfilling {TICKER} for {BACKFILL_DATE.isoformat()} ===\n")

    # 1. Load current state and guard against double-running
    state = load_portfolio_state()
    if TICKER in state.positions:
        print(f"{TICKER} already in portfolio — backfill already applied. Exiting.")
        return 0

    # 2. Get Monday's actual close price
    monday_close = _monday_close_price()
    print(f"{TICKER} {BACKFILL_DATE} close: ${monday_close:.2f}")

    # 3. Apply the fixed executor's round-up logic. Schwab realism = slippage on
    # the fill price; since QQQ is not S&P 500, whole-share rounding applies.
    schwab = SchwabRealism()
    fill = schwab.buy(TICKER, TARGET_WEIGHT_PCT / 100.0 * state.total_aum({}), monday_close)
    # Manual round-up since we're outside _execute_buy's flow
    if fill.fill_shares <= 0:
        # Use 1 share's cost as the actual fill amount
        retry_target = fill.fill_price
        fill = schwab.buy(TICKER, retry_target, monday_close)
    assert fill.fill_shares == 1, f"expected exactly 1 share, got {fill.fill_shares}"
    print(f"Fill: {fill.fill_shares} share @ ${fill.fill_price:.4f} = ${fill.fill_dollars:.2f}")
    print(f"Cash before: ${state.cash:.2f}")

    # 4. Apply the buy to portfolio state
    lot = state.open_lot(TICKER, fill.fill_shares, fill.fill_price, BACKFILL_DATE)
    print(f"Cash after:  ${state.cash:.2f}")
    print(f"New lot: {lot.lot_id} ({lot.shares} sh @ ${lot.cost_basis_per_share:.4f})\n")
    save_portfolio_state(state)

    # 5. Append the trade
    trade = TradeRecord(
        kind="buy", ticker=TICKER, shares=fill.fill_shares, price=fill.fill_price,
        cost_basis_per_share=fill.fill_price, acquisition_date=BACKFILL_DATE,
        lot_id=lot.lot_id, fractional_eligible=fill.fractional_eligible,
        cash_residual=fill.cash_residual, notes=BACKFILL_NOTE,
    )
    append_trade(trade.to_dict())
    print("Appended trade to data/trades.jsonl")

    # 6. Re-mark Monday's snapshot with QQQ now included.
    # Use no prior_marks so we don't pollute weekly_return_pct (it was already
    # computed correctly in the original mark — we're just adding QQQ to the
    # positions list and bumping AUM).
    marks_file = MARKS_DIR / f"{BACKFILL_DATE.isoformat()}.json"
    with open(marks_file) as f:
        orig_marks = json.load(f)
    new_snap = mark_portfolio(state, prior_marks=None, today=BACKFILL_DATE)
    # Preserve original return/alpha calculations from the original mark
    new_snap_dict = new_snap.to_dict()
    for k in ("weekly_return_pct", "weekly_return_usd", "spy_weekly_return_pct",
              "alpha_pct", "prior_aum"):
        if orig_marks.get(k) is not None:
            new_snap_dict[k] = orig_marks[k]
    save_marks(BACKFILL_DATE, new_snap_dict)
    print(f"Re-marked {marks_file.name}: AUM now ${new_snap.aum:.2f}")

    # 7. Add Pick entry to pick_scoreboard
    pick_sb = load_pick_scoreboard()
    pick = Pick(
        pick_id=_new_pick_id(), ticker=TICKER, week_of=BACKFILL_DATE.isoformat(),
        action="OPEN", rec_market_price=monday_close,
        executed_fill_price=fill.fill_price, target_weight_pct=TARGET_WEIGHT_PCT,
        conviction=CONVICTION, thesis=THESIS, factor_breakdown={},
        current_price=monday_close, lifetime_return_pct=0.0, days_held=0, status="open",
    )
    pick_sb["picks"].append(asdict(pick))
    pick_sb["aggregate"] = compute_aggregate(pick_sb)
    update_weekly_recaps(pick_sb, BACKFILL_DATE)
    save_pick_scoreboard(pick_sb)
    print(f"Added Pick {pick.pick_id} to pick_scoreboard.json")

    # 8. Update main scoreboard's current_aum with the new total
    sb = load_scoreboard()
    if sb.get("weekly_returns"):
        # Rewrite the last weekly return's aum to reflect QQQ inclusion
        sb["weekly_returns"][-1]["aum"] = new_snap.aum
    sb["current_aum"] = round(new_snap.aum, 4)
    # Recompute cumulative based on deployment_aum baseline
    deployment_aum = sb.get("deployment_aum") or new_snap.aum
    sb["cumulative_return_pct"] = round((new_snap.aum - deployment_aum) / deployment_aum if deployment_aum > 0 else 0, 6)
    sb["cumulative_return_usd"] = round(new_snap.aum - deployment_aum, 4)
    save_scoreboard(sb)
    print(f"Updated scoreboard.json: current_aum=${sb['current_aum']:.2f}")

    # 9. Annotate the decision file
    decision_file = DECISIONS_DIR / f"{BACKFILL_DATE.isoformat()}.json"
    with open(decision_file) as f:
        decision = json.load(f)
    decision["backfill_audit"] = {
        "ticker": TICKER,
        "backfilled_at_utc": datetime.now(timezone.utc).isoformat(),
        "backfill_reason": (
            "Original 2026-05-25 cycle skipped this OPEN with 'fill shares = 0' "
            "because the non-fractional buy floored to 0 (target ~$600 < 1 share "
            "at ~$625). Executor was fixed in commit bd9156f to round up to 1 "
            "share when intent is meaningful + caps allow. This backfill applies "
            "the fixed logic to the original PM decision so the audit trail "
            "reflects what should have happened."
        ),
        "rec_market_price": monday_close,
        "executed_fill_price": fill.fill_price,
        "shares": fill.fill_shares,
        "lot_id": lot.lot_id,
        "pick_id": pick.pick_id,
        "script": "scripts/backfill_qqq_2026_05_25.py",
    }
    # Also remove the QQQ skip entry from the original skipped list since it's
    # now resolved (keep a note for traceability)
    decision["skipped"] = [s for s in (decision.get("skipped") or [])
                           if not (s.get("ticker") == TICKER and s.get("action") == "buy")]
    decision_file.write_text(json.dumps(decision, indent=2, default=str))
    print(f"Annotated {decision_file.name} with backfill_audit")

    print("\n=== Backfill complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
