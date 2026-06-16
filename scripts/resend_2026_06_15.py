"""Re-send the 2026-06-15 Discord report — pure replay, no API costs.

Today's cycle did all the real work (agents fired, trades executed, data
committed) but failed at the final Discord POST because the embed batch
exceeded Discord's 6000-char-per-message cap. The chunker fix (commit
7e45d7f) lets the same payload land in 2 messages instead of one
oversized one.

This script:
  1. Loads the saved decision/marks/scoreboard/pick_scoreboard from disk
  2. Re-composes the embeds using the post-fix composer + chunker
  3. POSTs to Discord via the existing webhook
  4. Makes ZERO LLM / agent / scout calls

Cost: Discord webhook POST is free. Single-purpose, run once, idempotent
in the sense that re-running just re-sends.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.config import Config                                             # noqa: E402
from src.discord.composer import compose_digest                           # noqa: E402
from src.discord.sender import DiscordSender                              # noqa: E402
from src.tracking.actual_book import load_actual_book                     # noqa: E402

DATE = date(2026, 6, 15)


def main() -> int:
    decision_file = REPO_ROOT / f"data/decisions/{DATE.isoformat()}.json"
    marks_file = REPO_ROOT / f"data/marks/{DATE.isoformat()}.json"
    scoreboard_file = REPO_ROOT / "data/scoreboard.json"

    if not decision_file.exists():
        print(f"ERROR: {decision_file} not found", file=sys.stderr)
        return 1

    d = json.loads(decision_file.read_text())
    mark = json.loads(marks_file.read_text())
    scoreboard = json.loads(scoreboard_file.read_text())

    pm_output = (d.get("pm") or {}).get("output") or {}
    reflection_output = (d.get("reflection") or {}).get("output") or {}
    insider_brief = (d.get("scout_briefs") or {}).get("insider")
    pick_sb = d.get("pick_scoreboard_snapshot") or {}
    actual_book = load_actual_book()

    title, embeds = compose_digest(
        today=DATE,
        portfolio_state=d.get("portfolio_state_after") or {},
        mark=mark,
        pm_output=pm_output,
        executed_trades=d.get("executed_trades") or [],
        skipped=d.get("skipped") or [],
        scoreboard=scoreboard,
        reflection=reflection_output,
        degraded_signals=d.get("degraded_signals") or [],
        insider_brief=insider_brief,
        pick_scoreboard=pick_sb,
        actual_book=actual_book,
    )

    print(f"Composed {len(embeds)} embeds for {DATE.isoformat()}")
    print(f"Title: {title}")

    config = Config.from_env()
    if not config.discord.webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL not set in .env", file=sys.stderr)
        return 1

    sender = DiscordSender(config.discord)
    ok = sender.send(embeds)
    if not ok:
        print("Discord send returned False — check logs above", file=sys.stderr)
        return 1

    print("✓ Sent to Discord successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
