"""Discord composer — hedge-fund report format.

Sections (each is one embed):
  1. Header / weekly thesis + color-coded mood
  2. Scoreboard (cum return, vs SPY, AUM, after-tax, win rate)
  3. This week's actions (one block per non-NONE/HOLD decision)
  4. Open positions with MTM
  5. Last week's reflection summary
  6. Diagnostics — degraded signals + skipped trades
  7. Disclaimer footer

Discord constraints respected: ≤6000 chars per embed, ≤10 embeds per message.
The sender chunks across messages if needed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional


COLOR_BULLISH = 0x00CC66
COLOR_BEARISH = 0xCC3333
COLOR_NEUTRAL = 0xFFAA00
COLOR_INFO = 0x3498DB
COLOR_SCOREBOARD = 0x9B59B6


def _color_for_return(pct: Optional[float]) -> int:
    if pct is None:
        return COLOR_NEUTRAL
    if pct > 0.005:
        return COLOR_BULLISH
    if pct < -0.005:
        return COLOR_BEARISH
    return COLOR_NEUTRAL


def _pct(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.{digits}f}%"


def _usd(x: Optional[float]) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _trim(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _conviction_badge(c: Optional[str]) -> str:
    return {"high": "🟢 HIGH", "medium": "🟡 MED", "low": "🟠 LOW"}.get((c or "").lower(), "—")


def _action_badge(action: str) -> str:
    return {
        "OPEN":  "🟢 OPEN",
        "ADD":   "🟢 ADD",
        "HOLD":  "⚪ HOLD",
        "TRIM":  "🟠 TRIM",
        "CLOSE": "🔴 CLOSE",
        "NONE":  "·  PASS",
    }.get(action, action)


def compose_digest(
    today: date,
    portfolio_state: dict,
    mark: dict,
    pm_output: dict,
    executed_trades: list[dict],
    skipped: list[dict],
    scoreboard: dict,
    reflection: Optional[dict],
    degraded_signals: list[str],
    insider_brief: Optional[dict] = None,
    pick_scoreboard: Optional[dict] = None,
    actual_book: Optional[dict] = None,
) -> tuple[str, list[dict]]:
    """Return (title, embeds) ready to hand to DiscordSender."""
    embeds: list[dict] = []

    title = _title(today, mark)
    embeds.append(_header_embed(today, mark, pm_output, title))
    embeds.append(_scoreboard_embed(scoreboard))

    # Actual-vs-simulated-vs-SPY — appears only if the user has bootstrapped
    # their TOS book. Sits right after the simulation scoreboard so the two
    # are visually adjacent for comparison.
    if actual_book and actual_book.get("entries_count"):
        embeds.append(_actual_book_embed(actual_book, scoreboard))

    pick_sb_embed = _pick_scoreboard_embed(pick_scoreboard) if pick_scoreboard else None
    if pick_sb_embed:
        embeds.append(pick_sb_embed)

    actions_embed = _actions_embed(pm_output, executed_trades)
    if actions_embed:
        embeds.append(actions_embed)

    positions_embed = _positions_embed(mark, portfolio_state)
    if positions_embed:
        embeds.append(positions_embed)

    insider_embed = _insider_embed(insider_brief) if insider_brief else None
    if insider_embed:
        embeds.append(insider_embed)

    if reflection:
        embeds.append(_reflection_embed(reflection))

    if degraded_signals or skipped:
        embeds.append(_diagnostics_embed(degraded_signals, skipped))

    embeds.append(_disclaimer_embed())

    return title, embeds


def compose_error(today: date, error_message: str, run_url: Optional[str] = None) -> list[dict]:
    """One-embed error payload for failed runs."""
    body = f"```\n{_trim(error_message, 1500)}\n```"
    if run_url:
        body += f"\n[View GitHub Actions run]({run_url})"
    return [{
        "title": f"❌ MiniHedgeFund — Run failed {today.isoformat()}",
        "description": body,
        "color": COLOR_BEARISH,
        "footer": {"text": f"Generated {datetime.utcnow().isoformat()}Z"},
    }]


# ---- internal embed builders ----

def _title(today: date, mark: dict) -> str:
    wr = mark.get("weekly_return_pct")
    suffix = ""
    if wr is not None:
        suffix = f" · {_pct(wr)} wk"
    return f"MiniHedgeFund — Week of {today.strftime('%b %d, %Y')}{suffix}"


def _header_embed(today: date, mark: dict, pm_output: dict, title: str) -> dict:
    thesis = pm_output.get("weekly_thesis") or "(no thesis recorded)"
    narrative = pm_output.get("narrative") or ""
    body = f"**Thesis:** {thesis}\n\n{narrative}"
    return {
        "title": title,
        "description": _trim(body, 4000),
        "color": _color_for_return(mark.get("weekly_return_pct")),
        "footer": {"text": "Not investment advice — experimental AI simulation."},
    }


def _scoreboard_embed(sb: dict) -> dict:
    lines = []
    aum = sb.get("current_aum") or 0
    cum = sb.get("cumulative_return_pct")
    spy = sb.get("spy_cumulative_pct")
    alpha = sb.get("cumulative_alpha_pct")
    after_tax = sb.get("after_tax_cumulative_return_pct")
    weeks = sb.get("weeks_tracked", 0)
    win = sb.get("weekly_win_rate")

    lines.append(f"**AUM:** {_usd(aum)}  ·  Initial: {_usd(sb.get('initial_capital'))}")
    lines.append(f"**Cumulative:** {_pct(cum)}  ·  After-tax: {_pct(after_tax)}")
    lines.append(f"**vs SPY:** {_pct(spy)}  ·  Alpha: {_pct(alpha)}")
    lines.append(f"**Weeks:** {weeks}  ·  Weekly win rate: {_pct(win, digits=1) if win is not None else '—'}")

    realized_g = sb.get("total_realized_gains") or 0
    realized_l = sb.get("total_realized_losses") or 0
    tax_owed = sb.get("estimated_tax_owed") or 0
    if realized_g or realized_l:
        lines.append(f"**Realized:** gains {_usd(realized_g)} · losses {_usd(realized_l)} · est. tax owed {_usd(tax_owed)}")

    best = sb.get("best_week")
    worst = sb.get("worst_week")
    if best and worst and best.get("return_pct") is not None:
        lines.append(f"**Best:** {best.get('week_of')} {_pct(best.get('return_pct'))}  ·  **Worst:** {worst.get('week_of')} {_pct(worst.get('return_pct'))}")

    return {
        "title": "📊 Scoreboard",
        "description": "\n".join(lines),
        "color": COLOR_SCOREBOARD,
    }


def _actions_embed(pm_output: dict, executed_trades: list[dict]) -> Optional[dict]:
    decisions = pm_output.get("decisions") or []
    actionable = [d for d in decisions if d.get("action") not in (None, "NONE", "HOLD")]
    if not actionable:
        return None

    # Map executed trades back to decisions by ticker
    exec_by_ticker: dict[str, list[dict]] = {}
    for t in executed_trades:
        exec_by_ticker.setdefault(t.get("ticker", ""), []).append(t)

    blocks: list[str] = []
    for d in actionable:
        ticker = d.get("ticker", "")
        action = d.get("action", "")
        conv = _conviction_badge(d.get("conviction"))
        thesis = _trim(d.get("thesis", ""), 280)
        size = ""
        if action == "OPEN":
            size = f" · {d.get('target_weight_pct', 0):.1f}% target"
        elif action == "ADD":
            size = f" · +{d.get('additional_weight_pct', 0):.1f}%"
        elif action == "TRIM":
            size = f" · {d.get('trim_pct_of_position', 0):.0f}% of position"

        fills = exec_by_ticker.get(ticker, [])
        fill_str = ""
        if fills:
            kinds = ", ".join(f"{f['kind']} {f['shares']} @ ${f['price']:.2f}" for f in fills[:3])
            fill_str = f"\n   _fill:_ {kinds}"

        blocks.append(
            f"**{_action_badge(action)} `${ticker}`** · {conv}{size}\n"
            f"   {thesis}{fill_str}"
        )

    desc = "\n\n".join(blocks)
    return {
        "title": "🎯 This Week's Actions",
        "description": _trim(desc, 4000),
        "color": COLOR_INFO,
    }


def _positions_embed(mark: dict, portfolio_state: dict) -> Optional[dict]:
    positions = mark.get("positions") or []
    if not positions:
        return {
            "title": "💼 Positions",
            "description": f"All cash: {_usd(mark.get('cash'))}",
            "color": COLOR_NEUTRAL,
        }

    lines = []
    aum = mark.get("aum") or 1.0
    for p in positions:
        ticker = p.get("ticker", "")
        mv = p.get("market_value") or 0
        weight = mv / aum if aum > 0 else 0
        upl = p.get("unrealized_pnl") or 0
        upl_pct = p.get("unrealized_pnl_pct")
        days = p.get("days_held")
        upl_color = "🟢" if upl > 0 else ("🔴" if upl < 0 else "⚪")
        lines.append(
            f"{upl_color} **${ticker}** · {_usd(mv)} ({weight * 100:.1f}%)  ·  "
            f"{_pct(upl_pct)}  ·  {days}d held"
        )

    cash_pct = mark.get("cash", 0) / aum if aum > 0 else 0
    lines.append(f"💵 **Cash** · {_usd(mark.get('cash'))} ({cash_pct * 100:.1f}%)")

    return {
        "title": "💼 Open Positions (MTM)",
        "description": _trim("\n".join(lines), 4000),
        "color": COLOR_INFO,
    }


def _reflection_embed(reflection: dict) -> dict:
    out = reflection.get("output") or reflection  # support both wrapped+unwrapped
    summary = out.get("summary") or ""
    lessons = out.get("lessons_for_pm") or []
    watch = out.get("watch_for") or []

    lines = []
    if summary:
        lines.append(f"_{summary}_")

    if lessons:
        lines.append("\n**Lessons applied this week:**")
        for ll in lessons[:6]:
            lines.append(f"• {ll}")

    if watch:
        lines.append("\n**Watching for:**")
        for w in watch[:3]:
            lines.append(f"• {w}")

    return {
        "title": "🔁 Reflection",
        "description": _trim("\n".join(lines), 4000),
        "color": COLOR_SCOREBOARD,
    }


def _diagnostics_embed(degraded_signals: list[str], skipped: list[dict]) -> dict:
    lines = []
    if degraded_signals:
        lines.append("**Degraded signals:** " + ", ".join(degraded_signals))
    if skipped:
        lines.append("**Skipped trades:**")
        for s in skipped[:8]:
            lines.append(f"• `{s.get('ticker', '?')}` ({s.get('action', '?')}) — {s.get('reason', '?')}")
    return {
        "title": "🔧 Diagnostics",
        "description": _trim("\n".join(lines), 2000),
        "color": COLOR_NEUTRAL,
    }


def _actual_book_embed(actual: dict, sim_scoreboard: dict) -> dict:
    """User's actual TOS book vs the simulated book vs SPY — the north-star
    section. SPY benchmark is anchored to the user's first actual entry date
    (apples-to-apples for the actual book; sim has its own deployment_aum
    baseline)."""
    lines = []

    aum = actual.get("current_aum")
    cum = actual.get("cumulative_return_pct")
    sim_cum = sim_scoreboard.get("cumulative_return_pct")
    spy_cum = actual.get("spy_return_from_inception_pct")
    alpha = actual.get("alpha_pct")
    inception = actual.get("inception_date")
    positions = actual.get("positions") or []

    lines.append(f"**Your TOS book:**  {_usd(aum)}  ·  {_pct(cum)} cum")
    lines.append(f"**Simulated book:** {_pct(sim_cum)} cum")
    if spy_cum is not None:
        lines.append(f"**SPY** since {inception}: {_pct(spy_cum)}")
    if alpha is not None:
        marker = "🟢" if alpha > 0 else "🔴" if alpha < 0 else "⚪"
        lines.append(f"**Your alpha vs SPY:** {marker} {_pct(alpha)}")
    lines.append(
        f"_Open positions: {len(positions)}  ·  Realized P&L: {_usd(actual.get('realized_pnl'))}_"
    )
    if actual.get("leveraged"):
        lines.append(f"⚠ **Cash negative** (${actual.get('cash'):.2f}) — over-deployed vs $10k notional")

    return {
        "title": "🎯 Actual vs Simulated vs SPY",
        "description": _trim("\n".join(lines), 4000),
        "color": COLOR_SCOREBOARD,
    }


def _pick_scoreboard_embed(pick_sb: dict) -> Optional[dict]:
    """The signal-quality view — every pick the system has ever made.

    Distinct from portfolio scoreboard: this answers "if every recommendation
    had been bought at recommendation time, frictionless, how would the
    basket have performed?" Independent of sizing rules and Schwab realism.
    """
    agg = pick_sb.get("aggregate") or {}
    if not agg.get("total_picks"):
        return None

    lines = []
    total = agg.get("total_picks", 0)
    open_n = agg.get("open_picks", 0)
    closed_n = agg.get("closed_picks", 0)
    wins = agg.get("win_count", 0)
    losses = agg.get("loss_count", 0)
    win_rate = agg.get("win_rate", 0)

    lines.append(f"**Total picks:** {total}  ·  {open_n} open · {closed_n} closed")
    lines.append(f"**Win rate:** {win_rate * 100:.1f}%  ({wins} W / {losses} L)")

    eq = agg.get("equal_weight_basket_return_pct")
    wt = agg.get("weighted_basket_return_pct")
    avg_win = agg.get("avg_winner_return_pct")
    avg_loss = agg.get("avg_loser_return_pct")
    lines.append(f"**If executed at rec time (equal-weight):** {_pct(eq)}")
    lines.append(f"**Weight-aware basket:** {_pct(wt)}")
    if wins or losses:
        lines.append(f"  · winners avg {_pct(avg_win)}  ·  losers avg {_pct(avg_loss)}")

    best = agg.get("best_pick") or {}
    worst = agg.get("worst_pick") or {}
    if best.get("ticker"):
        lines.append(
            f"**Best:** ${best['ticker']} {_pct(best.get('return_pct'))} "
            f"({best.get('conviction', '?')}, {best.get('week_of', '?')})"
        )
    if worst.get("ticker") and worst.get("ticker") != best.get("ticker"):
        lines.append(
            f"**Worst:** ${worst['ticker']} {_pct(worst.get('return_pct'))} "
            f"({worst.get('conviction', '?')}, {worst.get('week_of', '?')})"
        )

    by_conv = agg.get("by_conviction") or {}
    if by_conv:
        conv_lines = []
        for conv in ("high", "medium", "low"):
            b = by_conv.get(conv)
            if not b or b.get("count", 0) == 0:
                continue
            conv_lines.append(
                f"  · **{conv.upper()}** ({b['count']}): "
                f"win {b['win_rate'] * 100:.0f}%, avg {_pct(b['avg_return_pct'])}"
            )
        if conv_lines:
            lines.append("**By conviction:**")
            lines.extend(conv_lines)

    return {
        "title": "📈 Pick Scoreboard (signal quality)",
        "description": _trim("\n".join(lines), 4000),
        "color": COLOR_SCOREBOARD,
    }


def _insider_embed(insider_brief: dict) -> Optional[dict]:
    """Surface tickers with notable insider activity. Skips when nothing
    meaningful happened (≥$50k net OR a cluster_buy)."""
    candidates = [
        c for c in (insider_brief.get("candidates") or [])
        if not c.get("is_etf")
        and (c.get("cluster_buy") or abs(c.get("net_value_usd") or 0) >= 50_000)
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda c: abs(c.get("net_value_usd") or 0), reverse=True)

    lines = []
    for c in candidates[:8]:
        net = c.get("net_value_usd") or 0
        buys = c.get("buy_count") or 0
        sells = c.get("sell_count") or 0
        marker = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪")
        cluster = " ⭐ cluster-buy" if c.get("cluster_buy") else ""
        lines.append(
            f"{marker} **${c['ticker']}** · net {_usd(net)}  ·  "
            f"{buys} buy / {sells} sell ({c.get('distinct_buyers', 0)} buyers, {c.get('distinct_sellers', 0)} sellers){cluster}"
        )

    return {
        "title": "🏦 Insider Activity (last 7 days)",
        "description": _trim("\n".join(lines), 4000),
        "color": COLOR_INFO,
    }


def _disclaimer_embed() -> dict:
    return {
        "title": " ",
        "description": (
            "_Not investment advice. This is an experimental AI-driven simulation; "
            "no real capital is deployed. All decisions are made by language models "
            "and may be wrong._"
        ),
        "color": COLOR_NEUTRAL,
    }
