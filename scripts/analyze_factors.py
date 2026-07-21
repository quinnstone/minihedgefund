"""Factor→return correlation analysis over the pick history.

Reads data/pick_scoreboard.json and computes, for each of the 7 synthesis
factors (sentiment/earnings/technical/macro_fit/influencer/news/insider):

  - Pearson correlation between factor score and pick return
  - Avg return for picks with above-median factor score
  - Avg return for picks with below-median factor score
  - Spread (positive = signal, negative = anti-signal)

Also breaks down by pick status (open vs closed) — because a factor that
predicts closed-pick outcomes may or may not still hold on live positions.

Run monthly (or after every ~10 new picks) to detect signal drift. If a
factor's correlation flips sign consistently, update HEURISTIC_WEIGHTS in
src/agents/synthesis.py accordingly.

Usage:
    python -m scripts.analyze_factors
    python -m scripts.analyze_factors --json  # emit JSON for programmatic use
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

FACTORS = ("sentiment", "earnings", "technical", "macro_fit", "influencer", "news", "insider")


def _pearson(pairs):
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    num = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    denom = (
        sum((p[0] - mx) ** 2 for p in pairs)
        * sum((p[1] - my) ** 2 for p in pairs)
    ) ** 0.5
    return num / denom if denom else 0.0


def analyze(picks: list[dict]) -> dict:
    """Compute per-factor stats over the given picks list."""
    stats = {}
    for f in FACTORS:
        pairs = []
        for p in picks:
            fb = p.get("factor_breakdown") or {}
            score = fb.get(f)
            ret = (
                p.get("final_return_pct")
                if p.get("status") == "closed"
                else p.get("lifetime_return_pct")
            )
            if score is not None and ret is not None:
                pairs.append((score, ret))
        n = len(pairs)
        if n < 3:
            stats[f] = {"n": n, "insufficient_data": True}
            continue
        corr = _pearson(pairs)
        med = sorted(p[0] for p in pairs)[n // 2]
        hi = [p[1] for p in pairs if p[0] > med]
        lo = [p[1] for p in pairs if p[0] <= med]
        avg_hi = sum(hi) / len(hi) if hi else None
        avg_lo = sum(lo) / len(lo) if lo else None
        spread = (avg_hi - avg_lo) if (avg_hi is not None and avg_lo is not None) else None
        stats[f] = {
            "n": n,
            "correlation": round(corr, 4) if corr is not None else None,
            "avg_return_above_median_score": round(avg_hi, 6) if avg_hi is not None else None,
            "avg_return_below_median_score": round(avg_lo, 6) if avg_lo is not None else None,
            "spread": round(spread, 6) if spread is not None else None,
        }
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON (default is human-readable)")
    parser.add_argument(
        "--only-closed", action="store_true",
        help="restrict to closed picks (removes look-forward bias from open picks)",
    )
    args = parser.parse_args()

    pick_file = REPO_ROOT / "data/pick_scoreboard.json"
    if not pick_file.exists():
        print("no data/pick_scoreboard.json yet", file=sys.stderr)
        return 1

    picks = json.loads(pick_file.read_text()).get("picks") or []
    if args.only_closed:
        picks = [p for p in picks if p.get("status") == "closed"]

    stats = analyze(picks)

    if args.json:
        print(json.dumps({"n_picks": len(picks), "factors": stats}, indent=2))
        return 0

    print(f"factor→return correlation across {len(picks)} pick(s)"
          + (" — closed only" if args.only_closed else ""))
    print()
    print(f"  {'factor':<11} {'n':>3}   {'corr':>7}   {'>med score avg':>15}   {'<=med avg':>10}   {'spread':>8}")
    for f, s in stats.items():
        if s.get("insufficient_data"):
            print(f"  {f:<11} n={s['n']:>2}  (insufficient)")
            continue
        avg_hi = s["avg_return_above_median_score"]
        avg_lo = s["avg_return_below_median_score"]
        spr = s["spread"]
        def fmt(x, wide=False): return f"{x * 100:+.1f}%" if x is not None else "n/a"
        print(
            f"  {f:<11} {s['n']:>3}   {s['correlation']:+.2f}   "
            f"{fmt(avg_hi):>15}   {fmt(avg_lo):>10}   {fmt(spr):>8}"
        )
    print()
    print("Interpretation:")
    print("  spread > +3%  = factor is a positive signal (keep or weight up)")
    print("  spread ~ 0%   = factor is noise (weight down or drop)")
    print("  spread < -3%  = factor is anti-signal (drop or invert)")
    print()
    print("If any factor's spread flipped from what's in HEURISTIC_WEIGHTS, update")
    print("src/agents/synthesis.py and re-measure after ~4 more weeks of picks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
