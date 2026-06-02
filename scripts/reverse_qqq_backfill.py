"""One-off reversal of the QQQ backfill from scripts/backfill_qqq_2026_05_25.py.

Background
----------
On 2026-05-26, the missed 2026-05-25 QQQ pick was backfilled into the
simulation by scripts/backfill_qqq_2026_05_25.py (commit 19f88eb). That
backfill assumed the user might mirror the simulation in their real Schwab
account. They did not — QQQ was never actually purchased IRL. To keep the
simulation honest as an experiment alongside real-money behavior, we
reverse the backfill across both cycles that have touched the QQQ lot:

  2026-05-25 — the original backfill (1 share added)
  2026-06-01 — the PM held QQQ; mark + decision included it in the book

This script is the symmetric inverse of backfill_qqq_2026_05_25.py.
Both scripts remain in the repo as a paired historical record.

What this updates
-----------------
  data/portfolio_state.json    — remove QQQ lot, restore $730.65 to cash
  data/trades.jsonl            — remove the backfill buy line (match lot_id)
  data/marks/2026-05-25.json   — re-mark without QQQ
  data/marks/2026-06-01.json   — re-mark without QQQ
  data/pick_scoreboard.json    — drop the QQQ Pick, recompute aggregate
  data/scoreboard.json         — adjust last two weekly_returns AUM
  data/decisions/2026-05-25.json — restore QQQ skipped[] entry, add reversal_audit
  data/decisions/2026-06-01.json — add reversal_audit (preserve PM record)

Idempotent: if QQQ already absent from portfolio_state, exits cleanly.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.tracking.persistence import (                                    # noqa: E402
    DECISIONS_DIR, MARKS_DIR, TRADES_FILE,
    load_portfolio_state, load_scoreboard,
    save_portfolio_state, save_scoreboard,
)
from src.tracking.pick_tracker import (                                   # noqa: E402
    compute_aggregate, load_pick_scoreboard, save_pick_scoreboard,
    update_weekly_recaps,
)

TICKER = "QQQ"
BACKFILL_LOT_ID = "deff22b3d1f6"      # from the backfill audit
BACKFILL_DATE = date(2026, 5, 25)
HOLD_DATE = date(2026, 6, 1)
ORIGINAL_SKIPPED_ENTRY = {"ticker": "QQQ", "action": "buy", "reason": "fill shares = 0"}
REVERSAL_TS = datetime.now(timezone.utc).isoformat()
REVERSAL_REASON = (
    "The 2026-05-25 backfill assumed user mirroring; user did NOT actually buy "
    "QQQ IRL, so the simulation is reverted to match. Removes lot "
    f"{BACKFILL_LOT_ID} and restores cash. The HOLD decision the PM made on "
    "2026-06-01 is preserved in the record as historical PM output, but the "
    "position itself is now absent from the book."
)


def main() -> int:
    print("=== Reversing QQQ backfill ===\n")

    state = load_portfolio_state()
    if TICKER not in state.positions:
        print(f"{TICKER} already absent from portfolio_state — reversal already applied. Exiting.")
        return 0

    # 1. Find the backfill lot
    pos = state.positions[TICKER]
    target = next((l for l in pos.lots if l.lot_id == BACKFILL_LOT_ID), None)
    if target is None:
        print(f"WARN: lot {BACKFILL_LOT_ID} not found among QQQ lots. Refusing to guess.")
        return 1

    # 2. Restore the cost basis to cash. We're undoing the buy as if it never
    # happened, not selling at market — so the original $730.65 (cost basis)
    # comes back, not today's price.
    restored_cash = target.shares * target.cost_basis_per_share
    print(f"Removing lot {BACKFILL_LOT_ID}: {target.shares} sh QQQ @ "
          f"${target.cost_basis_per_share:.4f} = ${restored_cash:.2f}")

    pos.remove_lot(BACKFILL_LOT_ID)
    state.cash += restored_cash
    if not pos.lots:
        del state.positions[TICKER]

    print(f"Cash: now ${state.cash:.2f}")
    print(f"Positions: {sorted(state.positions.keys())}")
    save_portfolio_state(state)

    # 3. Strip the backfill trade from the append-only log
    trades_raw = TRADES_FILE.read_text().splitlines() if TRADES_FILE.exists() else []
    kept = []
    removed = 0
    for line in trades_raw:
        if not line.strip():
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if t.get("ticker") == TICKER and t.get("lot_id") == BACKFILL_LOT_ID:
            removed += 1
            continue
        kept.append(line)
    TRADES_FILE.write_text("\n".join(kept) + ("\n" if kept else ""))
    print(f"Removed {removed} trade line(s) from trades.jsonl")

    # 4. Re-mark both affected weeks without QQQ. Preserve the original price_map
    # (Monday's actual prices stay accurate); just drop QQQ from positions[] and
    # recompute the AUM, deducting QQQ's contribution.
    for d in (BACKFILL_DATE, HOLD_DATE):
        marks_file = MARKS_DIR / f"{d.isoformat()}.json"
        if not marks_file.exists():
            continue
        m = json.loads(marks_file.read_text())
        qqq_in_marks = [p for p in (m.get("positions") or []) if p.get("ticker") == TICKER]
        if not qqq_in_marks:
            print(f"  {marks_file.name}: QQQ already absent, skipping")
            continue
        qqq_mv = sum(float(p.get("market_value") or 0) for p in qqq_in_marks)
        m["positions"] = [p for p in m["positions"] if p.get("ticker") != TICKER]
        # Unconditionally add restored_cash to the mark's cash. In the
        # "QQQ never bought" counterfactual, every mark from BACKFILL_DATE
        # forward has $730.65 more cash than the on-disk value (the money
        # we never spent on QQQ). Earlier versions of this script had a
        # cleverness check that skipped the adjustment when the diff felt
        # large — buggy: by construction the diff WILL be large after the
        # portfolio state has been updated. Always add.
        m["cash"] = round(float(m.get("cash") or 0) + restored_cash, 4)
        m["aum"] = round(m["cash"] + sum(float(p.get("market_value") or 0) for p in m["positions"]), 4)
        marks_file.write_text(json.dumps(m, indent=2, default=str))
        print(f"  {marks_file.name}: removed QQQ ({qqq_mv:.2f} MV), new AUM=${m['aum']:.2f}")

    # 5. Drop the QQQ Pick + recompute aggregates
    sb_picks = load_pick_scoreboard()
    before = len(sb_picks.get("picks") or [])
    sb_picks["picks"] = [p for p in sb_picks.get("picks") or []
                         if not (p.get("ticker") == TICKER and p.get("week_of") == BACKFILL_DATE.isoformat())]
    after = len(sb_picks["picks"])
    print(f"pick_scoreboard: {before - after} QQQ Pick(s) removed")
    sb_picks["aggregate"] = compute_aggregate(sb_picks)
    # Rebuild weekly recaps for both weeks
    update_weekly_recaps(sb_picks, BACKFILL_DATE)
    update_weekly_recaps(sb_picks, HOLD_DATE)
    save_pick_scoreboard(sb_picks)

    # 6. Scoreboard: rewrite the AUM of the two affected weekly entries +
    # current_aum, then recompute cumulative against deployment_aum
    sb = load_scoreboard()
    # Pull post-fix AUM from the marks we just wrote
    aum_by_week = {}
    for d in (BACKFILL_DATE, HOLD_DATE):
        mf = MARKS_DIR / f"{d.isoformat()}.json"
        if mf.exists():
            aum_by_week[d.isoformat()] = float(json.loads(mf.read_text()).get("aum") or 0)
    for w in sb.get("weekly_returns") or []:
        if w.get("week_of") in aum_by_week:
            w["aum"] = aum_by_week[w["week_of"]]
    if sb.get("weekly_returns"):
        sb["current_aum"] = round(sb["weekly_returns"][-1]["aum"], 4)
    deployment_aum = sb.get("deployment_aum") or sb["current_aum"]
    sb["cumulative_return_pct"] = round(
        (sb["current_aum"] - deployment_aum) / deployment_aum if deployment_aum > 0 else 0, 6,
    )
    sb["cumulative_return_usd"] = round(sb["current_aum"] - deployment_aum, 4)
    save_scoreboard(sb)
    print(f"scoreboard: current_aum=${sb['current_aum']:.2f}, "
          f"cumulative={sb['cumulative_return_pct']*100:+.3f}%")

    # 7. Annotate 2026-05-25 decision (restore QQQ skipped[], add reversal_audit)
    d1_file = DECISIONS_DIR / f"{BACKFILL_DATE.isoformat()}.json"
    d1 = json.loads(d1_file.read_text())
    skipped = d1.get("skipped") or []
    if not any(s.get("ticker") == TICKER and s.get("action") == "buy" for s in skipped):
        skipped.append(ORIGINAL_SKIPPED_ENTRY)
        d1["skipped"] = skipped
    d1["backfill_reversed_audit"] = {
        "reversed_at_utc": REVERSAL_TS,
        "reason": REVERSAL_REASON,
        "lot_id_removed": BACKFILL_LOT_ID,
        "cash_restored_usd": restored_cash,
        "script": "scripts/reverse_qqq_backfill.py",
    }
    d1_file.write_text(json.dumps(d1, indent=2, default=str))
    print(f"Annotated {d1_file.name}")

    # 8. Annotate 2026-06-01 decision (PM's HOLD QQQ stands as historical record)
    d2_file = DECISIONS_DIR / f"{HOLD_DATE.isoformat()}.json"
    if d2_file.exists():
        d2 = json.loads(d2_file.read_text())
        d2["qqq_retroactively_removed_audit"] = {
            "reversed_at_utc": REVERSAL_TS,
            "note": (
                "The PM HOLD decision on QQQ this week was made when QQQ was in "
                "the book (carried over from 2026-05-25 backfill). The backfill "
                "has since been reversed because the position was never bought "
                "IRL. PM record preserved as-is; QQQ is no longer in the actual "
                "portfolio_state from this date onward."
            ),
            "script": "scripts/reverse_qqq_backfill.py",
        }
        d2_file.write_text(json.dumps(d2, indent=2, default=str))
        print(f"Annotated {d2_file.name}")

    print("\n=== Reversal complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
