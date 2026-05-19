"""MiniHedgeFund — Monday 10am ET pipeline orchestration.

Run modes:
  python -m src.main                     # production run (ET-gated)
  python -m src.main --dry-run           # full pipeline; prints embed payload, no Discord
  python -m src.main --force             # skip ET gate (e.g. workflow_dispatch)

The pipeline:
  1. Load persistent portfolio state
  2. Mark to market (compute weekly return + SPY alpha)
  3. Reflection agent reads recent weeks → lessons for PM context
  4. 5 scouts gather signals (deterministic, parallel-safe)
  5. Synthesis agent merges briefs → ranked scorecard
  6. Risk + tax briefs (deterministic)
  7. PM agent makes trade decisions
  8. Executor applies trades (Schwab realism, wash-sale blocks, tax-aware lots)
  9. Persist everything; update scoreboard
 10. Compose + send Discord report

Failures inside individual scouts degrade gracefully; agent failures fail the
run and post an error embed to Discord via the CI workflow's error handler.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Optional

from . import __version__

from .agents.pm import PMAgent
from .agents.reflection import ReflectionAgent
from .agents.risk import (
    MAX_SECTOR_PCT,
    MAX_SINGLE_NAME_PCT,
    build_risk_brief,
)
from .agents.scouts import (
    run_earnings_scout,
    run_influencer_scout,
    run_insider_scout,
    run_macro_scout,
    run_news_scout,
    run_sentiment_scout,
    run_technical_scout,
)
from .agents.synthesis import HEURISTIC_WEIGHTS, SynthesisAgent, heuristic_synthesis
from .agents.tax_constraints import build_tax_brief
from .collectors.edgar import EdgarCollector
from .collectors.macro import MacroCollector
from .collectors.market import MarketCollector
from .collectors.nitter import NitterCollector
from .collectors.reddit import RedditCollector
from .collectors.stocktwits import StockTwitsCollector
from .config import Config
from .discord.composer import compose_digest, compose_error
from .discord.sender import DiscordSender
from .portfolio.schwab import DEFAULT_SLIPPAGE_BPS, SchwabRealism
from .portfolio.tax import LTCG_HOLDING_DAYS, WASH_SALE_WINDOW, TaxEngine
from .tracking.executor import execute_decisions
from .tracking.marking import fetch_price_map, mark_portfolio
from .tracking.pick_tracker import (
    close_picks,
    compute_aggregate,
    load_pick_scoreboard,
    record_picks,
    refresh_open_picks,
    save_pick_scoreboard,
    update_weekly_recaps,
)
from .tracking.persistence import (
    append_trade,
    load_latest_reflection,
    load_portfolio_state,
    load_recent_decisions,
    load_recent_marks,
    load_scoreboard,
    load_trades,
    recent_closed_lots,
    save_decision,
    save_marks,
    save_portfolio_state,
    save_reflection,
    save_scoreboard,
)
from .tracking.scoreboard import compute_realized_tax_totals, update_scoreboard
from .tracking.universe import build_universe

logger = logging.getLogger("minihedgefund")

INITIAL_CAPITAL = 10_000.00


def _today_et() -> date:
    """Date in America/New_York. We use ET for the canonical decision date."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()


def _is_monday_10am_et(tolerance_minutes: int = 90) -> bool:
    """True if we're within a 1.5h window centered on Monday 10am ET.

    The GitHub Actions workflow fires two crons (14:00 and 15:00 UTC) to cover
    EDT/EST. Exactly one of them is 10am ET, the other no-ops here.
    """
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() != 0:
        return False
    minutes_from_10 = abs((now.hour - 10) * 60 + now.minute)
    return minutes_from_10 <= tolerance_minutes


def _git_sha() -> Optional[str]:
    """Best-effort code version capture for the audit trail."""
    sha = os.getenv("GITHUB_SHA")
    if sha:
        return sha[:12]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


def _build_methodology_snapshot(
    tax_engine: TaxEngine,
    synthesis_fallback_used: bool,
    stocktwits_enabled: bool,
    edgar_lookback_days: int,
) -> dict:
    """Capture everything needed to faithfully replay this decision later.

    Anything that could change between runs and would affect a backtest goes
    here. If you change HEURISTIC_WEIGHTS, a sector cap, slippage assumption,
    subreddit list, or tax-bracket profile, those edits are versioned in this
    snapshot per-decision so future-you can rerun a specific Monday with the
    same rules that were in effect at the time.
    """
    from .collectors.reddit import RedditCollector
    return {
        "code_version": __version__,
        "git_sha": _git_sha(),
        "synthesis": {
            "weights": dict(HEURISTIC_WEIGHTS),
            "fallback_used": synthesis_fallback_used,
        },
        "portfolio_rules": {
            "max_single_name_pct": MAX_SINGLE_NAME_PCT,
            "max_sector_pct": MAX_SECTOR_PCT,
            "slippage_bps": DEFAULT_SLIPPAGE_BPS,
        },
        "tax_profile": {
            "stcg_rate": round(tax_engine.brackets.stcg_rate, 6),
            "ltcg_rate": round(tax_engine.brackets.ltcg_rate, 6),
            "ltcg_holding_days": LTCG_HOLDING_DAYS,
            "wash_sale_window_days": WASH_SALE_WINDOW,
            "federal_ordinary": tax_engine.brackets.federal_ordinary,
            "federal_ltcg": tax_engine.brackets.federal_ltcg,
            "state": tax_engine.brackets.state,
            "city": tax_engine.brackets.city,
            "niit_applies": tax_engine.brackets.niit_applies,
        },
        "scout_config": {
            "reddit_subreddits": list(RedditCollector.SUBREDDITS),
            "stocktwits_enabled": stocktwits_enabled,
            "edgar_lookback_days": edgar_lookback_days,
        },
    }


def _build_sector_map(risk_brief: dict, universe: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ticker, info in (risk_brief.get("candidates") or {}).items():
        sector = info.get("sector")
        if sector:
            out[ticker] = sector
    # ETFs default to "ETF" sector so they don't double-up under e.g. Technology
    for t in universe:
        if t not in out and (t.startswith("X") or t in {"SPY", "QQQ", "IWM", "DIA", "TLT", "SHY", "GLD", "IBIT", "ETHA"}):
            out[t] = "ETF"
    return out


def run_weekly(
    config: Config,
    dry_run: bool = False,
    force: bool = False,
    cron: bool = False,
    today: Optional[date] = None,
) -> int:
    today = today or _today_et()

    # Time-of-day gate. Skipped when:
    #   --force      → manual override (user knows what they want)
    #   --cron       → scheduled run; the cron itself decides timing.
    #                  GH Actions cron can lag hours, so trust the schedule.
    #   --dry-run    → testing
    if not force and not cron and not dry_run:
        if not _is_monday_10am_et():
            logger.info("not within Monday 10am ET window — skipping run")
            return 0

    # Idempotency. Skipped when:
    #   --force      → user explicitly asked to re-run
    #   --dry-run    → no persistence anyway, so re-running is harmless
    # Otherwise: if today's decision file already exists, the pipeline
    # already completed successfully today (e.g. earlier cron firing,
    # earlier manual run), and we shouldn't run again.
    if not force and not dry_run:
        from .tracking.persistence import DECISIONS_DIR
        decisions_today = DECISIONS_DIR / f"{today.isoformat()}.json"
        if decisions_today.exists():
            logger.info(
                "decision file %s already exists — pipeline already ran today, skipping",
                decisions_today.name,
            )
            return 0

    logger.info("=== MiniHedgeFund weekly cycle %s ===", today.isoformat())

    state = load_portfolio_state(initial_capital=INITIAL_CAPITAL)
    state_before_dict = deepcopy(state.to_dict())
    if state.inception_date is None:
        state.inception_date = today

    # 1. Mark to market
    prior_marks = load_recent_marks(n_weeks=1)
    prior = prior_marks[0] if prior_marks else None
    snap = mark_portfolio(state, prior, today=today)
    save_marks(today, snap.to_dict())
    logger.info("MTM: AUM=$%.2f, weekly_return=%s, alpha=%s",
                snap.aum,
                f"{snap.weekly_return_pct:.4%}" if snap.weekly_return_pct is not None else "n/a",
                f"{snap.alpha_pct:.4%}" if snap.alpha_pct is not None else "n/a")

    # 2. Reflection agent
    recent_decisions = load_recent_decisions(n_weeks=8)
    recent_marks = load_recent_marks(n_weeks=8)
    scoreboard = load_scoreboard()
    reflection_agent = ReflectionAgent(api_key=config.anthropic.api_key)
    pick_scoreboard_prior = load_pick_scoreboard()
    reflection_input = {
        "today": today.isoformat(),
        "weeks_of_history": min(len(recent_decisions), len(recent_marks)),
        "recent_decisions_summary": [
            {
                "date": d.get("date"),
                "weekly_thesis": ((d.get("pm") or {}).get("output") or {}).get("weekly_thesis"),
                "decisions": ((d.get("pm") or {}).get("output") or {}).get("decisions"),
                "executed_trades_count": len(d.get("executed_trades") or []),
            }
            for d in recent_decisions
        ],
        "recent_marks": [
            {
                "as_of": m.get("as_of"),
                "aum": m.get("aum"),
                "weekly_return_pct": m.get("weekly_return_pct"),
                "spy_weekly_return_pct": m.get("spy_weekly_return_pct"),
                "alpha_pct": m.get("alpha_pct"),
            }
            for m in recent_marks
        ],
        "scoreboard": scoreboard,
        "pick_scoreboard": {
            "aggregate": pick_scoreboard_prior.get("aggregate"),
            "weekly_recaps": pick_scoreboard_prior.get("weekly_recaps"),
            "recent_closed_picks": [
                p for p in (pick_scoreboard_prior.get("picks") or [])
                if p.get("status") == "closed"
            ][-15:],
        },
    }
    reflection_result = reflection_agent.run(reflection_input)
    save_reflection(today, {
        "agent_result": reflection_result.to_dict(),
        "output": reflection_result.output,
    })
    logger.info("reflection: success=%s, cost=$%.4f",
                reflection_result.success, reflection_result.estimated_cost_usd)

    # 3. Build universe
    stocktwits = StockTwitsCollector(sleep_s=1.0)
    reddit = RedditCollector(config.reddit)
    market = MarketCollector()
    macro = MacroCollector(config.fred)
    nitter = NitterCollector()

    trending = []
    try:
        trending = stocktwits.get_trending(limit=15, exclude_crypto=True)
    except Exception as exc:
        logger.warning("trending discovery failed: %s", exc)

    universe = build_universe(
        current_positions=list(state.positions.keys()),
        discovered=trending,
    )
    logger.info("universe: %d tickers", len(universe))

    # 4. Scouts (7 total — sentiment, earnings, technical, macro, influencer, news, insider)
    degraded: list[str] = []
    sentiment_brief = run_sentiment_scout(universe, reddit, stocktwits, lookback_days=7)
    earnings_brief = run_earnings_scout(universe, market)
    technical_brief = run_technical_scout(universe)
    macro_brief = run_macro_scout(macro)
    influencer_brief = run_influencer_scout(universe[:10], nitter)  # cap nitter calls
    news_brief = run_news_scout(universe)
    edgar = EdgarCollector(lookback_days=7)
    insider_brief = run_insider_scout(universe, edgar)

    for label, brief in [
        ("sentiment", sentiment_brief), ("earnings", earnings_brief),
        ("technical", technical_brief), ("macro", macro_brief),
        ("influencer", influencer_brief),
        ("news", news_brief), ("insider", insider_brief),
    ]:
        if brief.get("degraded"):
            degraded.append(label)

    # 5. Synthesis
    synth_agent = SynthesisAgent(api_key=config.anthropic.api_key)
    synth_result = synth_agent.run({
        "universe": universe,
        "today": today.isoformat(),
        "sentiment": sentiment_brief,
        "earnings": earnings_brief,
        "technical": technical_brief,
        "macro": macro_brief,
        "influencer": influencer_brief,
        "news": news_brief,
        "insider": insider_brief,
    })
    if not synth_result.success:
        logger.error("synthesis failed: %s", synth_result.error)
        return _abort(today, f"Synthesis agent failed: {synth_result.error}", config, dry_run)

    # Defensive fallback: tool-use schema's minItems is advisory, not enforced.
    # If the model returned an empty ranking, substitute a deterministic
    # weighted-average heuristic so the PM has something to read.
    if not (synth_result.output.get("ranked_candidates") or []):
        logger.warning(
            "synthesis returned empty ranked_candidates — substituting heuristic fallback"
        )
        fallback = heuristic_synthesis(
            scout_briefs={
                "sentiment": sentiment_brief,
                "earnings": earnings_brief,
                "technical": technical_brief,
                "macro": macro_brief,
                "influencer": influencer_brief,
                "news": news_brief,
                "insider": insider_brief,
            },
            universe=universe,
        )
        # Preserve any LLM narrative + themes if they were produced
        synth_result.output["ranked_candidates"] = fallback["ranked_candidates"]
        synth_result.output["_fallback_used"] = True
        if not synth_result.output.get("market_context"):
            synth_result.output["market_context"] = fallback["market_context"]
        if not synth_result.output.get("themes"):
            synth_result.output["themes"] = fallback["themes"]
        degraded.append("synthesis_fallback")

    logger.info("synthesis: %d candidates ranked, cost=$%.4f, fallback=%s",
                len(synth_result.output.get("ranked_candidates") or []),
                synth_result.estimated_cost_usd,
                synth_result.output.get("_fallback_used", False))

    top_candidates = [c["ticker"] for c in (synth_result.output.get("ranked_candidates") or [])][:15]

    # 6. Risk + tax briefs
    risk_brief = build_risk_brief(top_candidates, state, snap.price_map)
    tax_brief = build_tax_brief(
        top_candidates, state, snap.price_map, recent_closed_lots(days=35), as_of=today,
    )

    # 7. PM agent
    pm_agent = PMAgent(api_key=config.anthropic.api_key)
    pm_result = pm_agent.run({
        "today": today.isoformat(),
        "portfolio_state": state.to_dict(),
        "synthesis": synth_result.output,
        "risk_brief": risk_brief,
        "tax_brief": tax_brief,
        "reflection_lessons": (reflection_result.output or {}).get("lessons_for_pm", []),
        "reflection_watch_for": (reflection_result.output or {}).get("watch_for", []),
    })
    if not pm_result.success:
        logger.error("PM agent failed: %s", pm_result.error)
        return _abort(today, f"PM agent failed: {pm_result.error}", config, dry_run)
    logger.info("PM: %d decisions, target_cash=%s%%, cost=$%.4f",
                len(pm_result.output.get("decisions") or []),
                pm_result.output.get("target_cash_pct"),
                pm_result.estimated_cost_usd)

    # 8. Execute
    schwab = SchwabRealism()
    tax_engine = TaxEngine()
    sector_map = _build_sector_map(risk_brief, universe)
    wash_blocks = set(tax_brief.get("wash_sale_blocks", {}).keys())

    # Enrich price_map with prices for any ticker the PM wants to open or add to.
    # The original snap.price_map only covers current positions + SPY, so without
    # this step every OPEN gets skipped with "no price."
    decisions = pm_result.output.get("decisions") or []
    needed_prices: set[str] = set()
    for d in decisions:
        if d.get("action") in ("OPEN", "ADD"):
            t = (d.get("ticker") or "").upper()
            if t and t not in snap.price_map:
                needed_prices.add(t)
    if needed_prices:
        logger.info("fetching prices for %d new candidates: %s",
                    len(needed_prices), sorted(needed_prices))
        snap.price_map.update(fetch_price_map(sorted(needed_prices)))

    exec_result = execute_decisions(
        decisions=decisions,
        portfolio=state,
        price_map=snap.price_map,
        wash_sale_blocks=wash_blocks,
        schwab=schwab,
        tax_engine=tax_engine,
        sector_map=sector_map,
        as_of=today,
    )
    logger.info("executor: %d trades, %d skipped, realized_pnl=$%.2f",
                len(exec_result.trades), len(exec_result.skipped), exec_result.realized_pnl)

    for tr in exec_result.trades:
        append_trade(tr.to_dict())
    save_portfolio_state(state)

    # Re-mark after trades (so the Discord report shows post-execution book)
    post_snap = mark_portfolio(state, prior, today=today)
    save_marks(today, post_snap.to_dict())

    # 8b. Pick scoreboard — signal-quality ledger separate from portfolio MTM
    pick_sb = pick_scoreboard_prior   # reuse the loaded copy
    # First close any picks the PM CLOSEd this week, then record new buys,
    # then mark all open picks (including the new ones) to current prices.
    close_picks(
        pick_sb, today,
        pm_decisions=pm_result.output.get("decisions") or [],
        executed_trades=[t.to_dict() for t in exec_result.trades],
        market_price_map=post_snap.price_map,
    )
    record_picks(
        pick_sb, today,
        pm_decisions=pm_result.output.get("decisions") or [],
        executed_trades=[t.to_dict() for t in exec_result.trades],
        market_price_map=snap.price_map,   # un-slipped market prices at PM-call time
        ranked_candidates=synth_result.output.get("ranked_candidates") or [],
    )
    refresh_open_picks(pick_sb, today, post_snap.price_map)
    pick_sb["aggregate"] = compute_aggregate(pick_sb)
    update_weekly_recaps(pick_sb, today)
    save_pick_scoreboard(pick_sb)
    logger.info(
        "pick scoreboard: total=%d, open=%d, win_rate=%.1f%%, basket_return=%.2f%%",
        pick_sb["aggregate"]["total_picks"],
        pick_sb["aggregate"]["open_picks"],
        (pick_sb["aggregate"]["win_rate"] or 0) * 100,
        (pick_sb["aggregate"]["equal_weight_basket_return_pct"] or 0) * 100,
    )

    # 9. Scoreboard
    all_trades = load_trades()
    realized_g, realized_l, tax_owed = compute_realized_tax_totals(
        all_trades, tax_engine.brackets.stcg_rate, tax_engine.brackets.ltcg_rate,
    )
    new_week = {
        "week_of": today.isoformat(),
        "return_pct": post_snap.weekly_return_pct,
        "return_usd": post_snap.weekly_return_usd,
        "spy_pct": post_snap.spy_weekly_return_pct,
        "alpha": post_snap.alpha_pct,
        "aum": post_snap.aum,
    }
    inception_iso = state.inception_date.isoformat() if state.inception_date else today.isoformat()
    new_scoreboard = update_scoreboard(
        prior=scoreboard,
        new_week=new_week,
        initial_capital=state.initial_capital,
        inception_date_iso=inception_iso,
        trades_count=len(all_trades),
        realized_gains=realized_g,
        realized_losses=realized_l,
        estimated_tax_owed=tax_owed,
    )
    save_scoreboard(new_scoreboard)

    # 10. Save full decision audit
    methodology = _build_methodology_snapshot(
        tax_engine=tax_engine,
        synthesis_fallback_used=bool(synth_result.output.get("_fallback_used")),
        stocktwits_enabled=stocktwits.enabled,
        edgar_lookback_days=edgar.lookback_days,
    )
    decision_payload = {
        "date": today.isoformat(),
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "methodology": methodology,
        "portfolio_state_before": state_before_dict,
        "portfolio_state_after": state.to_dict(),
        "universe": universe,
        "scout_briefs": {
            "sentiment": sentiment_brief,
            "earnings": earnings_brief,
            "technical": technical_brief,
            "macro": macro_brief,
            "influencer": influencer_brief,
            "news": news_brief,
            "insider": insider_brief,
        },
        "synthesis": {"agent_result": synth_result.to_dict(), "output": synth_result.output},
        "risk_brief": risk_brief,
        "tax_brief": tax_brief,
        "reflection": {"agent_result": reflection_result.to_dict(), "output": reflection_result.output},
        "pm": {"agent_result": pm_result.to_dict(), "output": pm_result.output},
        "executed_trades": [t.to_dict() for t in exec_result.trades],
        "skipped": exec_result.skipped,
        "degraded_signals": degraded,
        "pick_scoreboard_snapshot": {
            "aggregate": pick_sb.get("aggregate"),
            "weekly_recaps": pick_sb.get("weekly_recaps"),
            "open_picks": [p for p in pick_sb.get("picks") or [] if p.get("status") == "open"],
        },
        "agent_costs_usd": {
            "reflection": round(reflection_result.estimated_cost_usd, 6),
            "synthesis": round(synth_result.estimated_cost_usd, 6),
            "pm": round(pm_result.estimated_cost_usd, 6),
            "total": round(
                reflection_result.estimated_cost_usd
                + synth_result.estimated_cost_usd
                + pm_result.estimated_cost_usd, 6,
            ),
        },
    }
    save_decision(today, decision_payload)

    # 11. Discord
    title, embeds = compose_digest(
        today=today,
        portfolio_state=state.to_dict(),
        mark=post_snap.to_dict(),
        pm_output=pm_result.output,
        executed_trades=[t.to_dict() for t in exec_result.trades],
        skipped=exec_result.skipped,
        scoreboard=new_scoreboard,
        reflection=reflection_result.output,
        degraded_signals=degraded,
        insider_brief=insider_brief,
        pick_scoreboard=pick_sb,
    )

    if dry_run:
        print("=== DRY RUN: would post these embeds ===")
        print(json.dumps({"title": title, "embeds": embeds}, indent=2, default=str))
    else:
        sender = DiscordSender(config.discord)
        ok = sender.send(embeds)
        if not ok:
            logger.error("discord send failed")
            return 1

    logger.info("=== cycle complete ===")
    return 0


def _abort(today: date, message: str, config: Config, dry_run: bool) -> int:
    """Post an error embed (or print it) and return non-zero."""
    embeds = compose_error(today, message)
    if dry_run:
        print(json.dumps(embeds, indent=2))
    else:
        try:
            DiscordSender(config.discord).send(embeds)
        except Exception as exc:
            logger.exception("error-embed send also failed: %s", exc)
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MiniHedgeFund weekly cycle")
    parser.add_argument("--dry-run", action="store_true",
                        help="run full pipeline; print Discord payload instead of posting")
    parser.add_argument("--force", action="store_true",
                        help="bypass both the time-of-day gate and the idempotency check")
    parser.add_argument("--cron", action="store_true",
                        help="bypass time-of-day gate but respect idempotency "
                             "(set by the CI workflow on scheduled triggers)")
    parser.add_argument("--env-file", default=None, help="path to .env file")
    parser.add_argument("--today", default=None,
                        help="override today (YYYY-MM-DD); useful for backtest replays")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config.from_env(args.env_file)
    missing = config.validate()
    if missing:
        logger.error("missing required config: %s", missing)
        return 2

    today = date.fromisoformat(args.today) if args.today else None

    try:
        return run_weekly(
            config, dry_run=args.dry_run, force=args.force, cron=args.cron, today=today,
        )
    except Exception as exc:
        logger.exception("fatal pipeline error")
        return _abort(today or _today_et(), f"Fatal: {type(exc).__name__}: {exc}", config, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
