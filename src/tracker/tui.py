"""Compact tree-style rendering with inline progress bars — cswap-inspired.

One account header, then indented lines with ├/└ connectors. Each utilization
window gets a 20-char bar (█/░) colored by severity. Far more compact than a
rich Table: a 3-window Claude account takes 4 lines, not a 6-row table cell.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from rich.console import Console
from rich.text import Text

from .usage import AccountUsage

console = Console()

BAR_WIDTH = 20


def _age_str(fetched_at: float | None) -> str:
    if not fetched_at:
        return ""
    delta = time.time() - fetched_at
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta/60:.0f}m ago"
    return f"{delta/3600:.1f}h ago"


def _reset_str(resets_at: str | None) -> str:
    if not resets_at:
        return ""
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        delta = dt.timestamp() - time.time()
        if delta <= 0:
            return "resets now"
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        if h > 0:
            return f"resets in {h}h{m}m"
        return f"resets in {m}m"
    except (ValueError, TypeError):
        return ""


def _pct_color(pct: float) -> str:
    if pct >= 90:
        return "bold red"
    if pct >= 75:
        return "yellow"
    if pct >= 50:
        return "green"
    return "cyan"


def _bar(pct: float | None) -> Text:
    """A 20-char progress bar followed by the percentage."""
    if pct is None:
        return Text("—", style="dim")
    filled = int(round(pct / 100 * BAR_WIDTH))
    filled = max(0, min(BAR_WIDTH, filled))
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    return Text.assemble(
        (bar, _pct_color(pct)),
        (f" {pct:>3.0f}%", _pct_color(pct)),
    )


def _format_windows_claude(au: AccountUsage) -> list[Text]:
    """Build the indented ├/└ lines for a Claude account's windows."""
    if au.needs_relogin:
        return [Text("  re-login needed — refresh token dead", style="bold red")]

    if not au.windows:
        if au.error:
            return [Text(f"  {au.error}", style="red")]
        return [Text("  no data", style="dim")]

    w = au.windows
    lines: list[tuple[str, Text]] = []  # (label, bar_text)

    for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
        win = w.get(key)
        if not win:
            continue
        pct = win.get("pct")
        suffix = _reset_str(win.get("resets_at"))
        bar = _bar(pct)
        if suffix:
            bar.append(f"  {suffix}", style="dim")
        lines.append((label, bar))

    for s in w.get("scoped") or []:
        pct = s.get("pct")
        suffix = _reset_str(s.get("resets_at"))
        bar = _bar(pct)
        if suffix:
            bar.append(f"  {suffix}", style="dim")
        name = s["name"]
        if len(name) > 6:
            name = name[:4] + ".."
        lines.append((name, bar))

    # Spend line
    spend = w.get("spend")
    if spend:
        lines.append(("$$", Text(
            f" ${spend['used']:.2f} / ${spend['limit']:.2f} ({spend['pct']:.0f}%)",
            style="magenta",
        )))

    return _format_tree_lines(lines)


def _format_windows_grok(au: AccountUsage) -> list[Text]:
    """Build the indented lines for a Grok account's derived usage."""
    if au.error:
        return [Text(f"  {au.error}", style="red")]
    if not au.windows:
        return [Text("  no data", style="dim")]

    w = au.windows
    total_in = w.get("total_input", 0)
    total_out = w.get("total_output", 0)
    total_cost = w.get("total_cost", 0)
    sessions = w.get("session_count", 0)

    lines: list[tuple[str, Text]] = [
        ("tok", Text.assemble(
            (f"{_fmt_tok(total_in)} in", "cyan"),
            (" / ", "dim"),
            (f"{_fmt_tok(total_out)} out", "green"),
        )),
        ("$$", Text(f" ${total_cost:.2f}  ({sessions} sessions)", style="magenta")),
    ]

    # Live quota status from api.x.ai/v1/models
    quota = w.get("quota_status")
    if quota == "blocked":
        reason = w.get("quota_reason", "")
        msg = w.get("quota_message", "")
        lines.append(("qta", Text.assemble(
            ("BLOCKED", "bold red"),
            (f"  {msg}" if msg else f"  {reason}", "red"),
        )))
    elif quota == "active":
        lines.append(("qta", Text("active", style="green")))

    rl = w.get("last_rate_limit")
    if rl:
        lines.append(("rl", Text(f" {rl['kind']}", style="yellow")))

    last = w.get("last_activity")
    if last:
        lines.append(("last", Text(f" {last[:10]}", style="dim")))

    return _format_tree_lines(lines)


def _fmt_tok(n: int) -> str:
    """Format token counts compactly: 229M, 2.1M, 850K."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _format_tree_lines(lines: list[tuple[str, Text]]) -> list[Text]:
    """Format (label, content) pairs as ├/└ tree lines with padded labels."""
    if not lines:
        return []
    pad = max(len(label) for label, _ in lines)
    result: list[Text] = []
    for i, (label, content) in enumerate(lines):
        is_last = i == len(lines) - 1
        connector = "└" if is_last else "├"
        result.append(Text.assemble(
            (f"  {connector} ", "dim"),
            (f"{label:<{pad}} ", "dim"),
            content,
        ))
    return result


def _source_tag(au: AccountUsage) -> Text:
    """Small colored source/age indicator after the account label."""
    style = {
        "api": "green",
        "cached": "dim",
        "backing-off": "yellow",
        "derived": "blue",
        "manual": "magenta",
        "error": "red",
        "no-data": "dim red",
    }.get(au.source, "")
    parts = [Text(au.source, style=style)]
    age = _age_str(au.fetched_at)
    if age:
        parts.append(Text(f" · {age}", style="dim"))
    return Text.assemble(*parts)


def render_accounts(results: list[AccountUsage]) -> None:
    """Render the all-accounts usage dashboard as a compact tree."""
    if not results:
        console.print("[dim]No accounts added yet. Run:[/dim]  tracker add <provider>")
        return

    # Group by provider
    by_provider: dict[str, list[AccountUsage]] = {}
    for au in results:
        by_provider.setdefault(au.provider, []).append(au)

    lines: list[Text] = []
    provider_order = ["claude", "grok"]
    for pi, provider in enumerate(provider_order):
        accounts = by_provider.get(provider, [])
        if not accounts:
            continue
        # Provider header
        lines.append(Text.assemble(
            (provider.capitalize(), "bold"),
            (f"  ({len(accounts)})", "dim"),
        ))
        for ai, au in enumerate(accounts):
            is_last_acct = (ai == len(accounts) - 1)
            # Account header line
            connector = "└" if is_last_acct else "├"
            lines.append(Text.assemble(
                (f"  {connector} ", "dim"),
                (au.label, "bold"),
                (f"  {au.email or ''}", "dim"),
                ("  [", "dim"),
                _source_tag(au),
                ("]", "dim"),
            ))
            # Window lines (indented under the account)
            if au.provider == "claude":
                win_lines = _format_windows_claude(au)
            else:
                win_lines = _format_windows_grok(au)
            for wl in win_lines:
                lines.append(wl)
        if pi < len(provider_order) - 1:
            lines.append(Text(""))

    console.print(Text.assemble(*[Text("\n")] ) if not lines else Text("\n").join(lines))


def render_status(results: list[AccountUsage]) -> None:
    """One-line aggregate summary."""
    claude_accts = [r for r in results if r.provider == "claude"]
    grok_accts = [r for r in results if r.provider == "grok"]

    parts: list[str] = []
    parts.append(f"{len(claude_accts)} Claude")
    parts.append(f"{len(grok_accts)} Grok")

    max_7d: float | None = None
    relogin_count = sum(1 for r in claude_accts if r.needs_relogin)
    for r in claude_accts:
        if r.windows and r.windows.get("seven_day"):
            pct = r.windows["seven_day"].get("pct")
            if pct is not None and (max_7d is None or pct > max_7d):
                max_7d = pct
    if max_7d is not None:
        parts.append(f"max 7d: {max_7d:.0f}%")
    if relogin_count:
        parts.append(f"{relogin_count} need re-login")

    console.print("  · ".join(parts))


def render_tokens(rows: list, since: str | None = None) -> None:
    """Render historical token-usage report."""
    if not rows:
        console.print("[dim]No token usage recorded yet.[/dim]")
        return

    # Aggregate by session for compactness
    from collections import defaultdict
    by_session: dict[str, dict] = defaultdict(lambda: {
        "input": 0, "output": 0, "cache": 0, "cost": 0.0, "model": "", "ts": 0
    })
    for row in rows:
        sid = row["session_id"]
        s = by_session[sid]
        s["input"] += row["input_tokens"] or 0
        s["output"] += row["output_tokens"] or 0
        s["cache"] += row["cache_tokens"] or 0
        s["cost"] += row["cost_estimate"] or 0
        if row["model"]:
            s["model"] = row["model"]
        if row["ts"] > s["ts"]:
            s["ts"] = row["ts"]

    console.print(f"[bold]Token Usage[/bold]  [dim]({len(by_session)} sessions"
                  + (f" since {since}" if since else "") + ")[/dim]\n")

    for sid, s in sorted(by_session.items(), key=lambda x: -x[1]["ts"]):
        dt = datetime.fromtimestamp(s["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        console.print(Text.assemble(
            (f"  ├ {dt}  ", "dim"),
            (f"{s['model']:<18}", "cyan"),
            ("  ", ""),
            (f"{_fmt_tok(s['input'])} in", "cyan"),
            (" / ", "dim"),
            (f"{_fmt_tok(s['output'])} out", "green"),
            ("  ", ""),
            (f"${s['cost']:.2f}", "magenta"),
        ))