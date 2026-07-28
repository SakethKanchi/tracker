"""Rich table rendering for `tracker list` — THE all-accounts dashboard."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .usage import AccountUsage

console = Console()


def _age_str(fetched_at: float | None) -> str:
    if not fetched_at:
        return "—"
    delta = time.time() - fetched_at
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta/60:.0f}m ago"
    return f"{delta/3600:.1f}h ago"


def _reset_str(resets_at: str | None) -> str:
    """Format an ISO reset timestamp as a relative countdown."""
    if not resets_at:
        return ""
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        delta = dt.timestamp() - time.time()
        if delta <= 0:
            return "reset now"
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        if h > 0:
            return f"resets in {h}h{m}m"
        return f"resets in {m}m"
    except (ValueError, TypeError):
        return ""


def _pct_text(pct: float | None) -> Text:
    """Color-code a utilization percentage."""
    if pct is None:
        return Text("—", style="dim")
    styled = Text(f"{pct:.0f}%")
    if pct >= 90:
        styled.stylize("bold red")
    elif pct >= 75:
        styled.stylize("yellow")
    elif pct >= 50:
        styled.stylize("green")
    else:
        styled.stylize("cyan")
    return styled


def _claude_usage_cell(au: AccountUsage) -> Text:
    """Build the usage text for a Claude account."""
    if au.needs_relogin:
        return Text("re-login needed", style="bold red")

    if not au.windows:
        if au.error:
            return Text(au.error, style="red")
        return Text("no data", style="dim")

    w = au.windows
    parts: list[Text] = []

    h5 = w.get("five_hour")
    if h5:
        parts.append(Text("5h: "))
        parts.append(_pct_text(h5.get("pct")))
        reset = _reset_str(h5.get("resets_at"))
        if reset:
            parts.append(Text(f" ({reset})", style="dim"))
        parts.append(Text("  "))

    d7 = w.get("seven_day")
    if d7:
        parts.append(Text("7d: "))
        parts.append(_pct_text(d7.get("pct")))
        reset = _reset_str(d7.get("resets_at"))
        if reset:
            parts.append(Text(f" ({reset})", style="dim"))

    # Scoped windows (per-model limits like Fable)
    scoped = w.get("scoped")
    if scoped and isinstance(scoped, list):
        for s in scoped:
            parts.append(Text(f"\n  {s['name']}: "))
            parts.append(_pct_text(s.get("pct")))

    # Spend (pay-as-you-go)
    spend = w.get("spend")
    if spend:
        parts.append(Text(
            f"\n  spend: ${spend['used']:.2f}/${spend['limit']:.2f} ({spend['pct']:.0f}%)",
            style="magenta",
        ))

    return Text.assemble(*parts) if parts else Text("no windows", style="dim")


def _grok_usage_cell(au: AccountUsage) -> Text:
    """Build the usage text for a Grok account (derived, no live API)."""
    if au.error:
        return Text(au.error, style="red")

    if not au.windows:
        return Text("no data", style="dim")

    w = au.windows
    parts: list[Text] = []

    total_in = w.get("total_input", 0)
    total_out = w.get("total_output", 0)
    total_cost = w.get("total_cost", 0)
    sessions = w.get("session_count", 0)

    parts.append(Text("tokens: "))
    parts.append(Text(f"{total_in:,}", style="cyan"))
    parts.append(Text(" in / "))
    parts.append(Text(f"{total_out:,}", style="green"))
    parts.append(Text(" out"))

    parts.append(Text(f"\n  cost: ${total_cost:.4f}", style="magenta"))
    parts.append(Text(f"\n  sessions: {sessions}", style="dim"))

    rl = w.get("last_rate_limit")
    if rl:
        parts.append(Text(f"\n  last throttle: {rl['kind']}", style="yellow"))

    last = w.get("last_activity")
    if last:
        parts.append(Text(f"\n  last active: {last[:19]}", style="dim"))

    return Text.assemble(*parts)


def render_accounts(results: list[AccountUsage]) -> None:
    """Render the all-accounts usage dashboard to the console."""
    if not results:
        console.print("[dim]No accounts added yet. Run:[/dim] tracker add <provider>")
        return

    table = Table(
        title="Account Usage",
        title_style="bold",
        show_lines=True,
        pad_edge=False,
    )
    table.add_column("Provider", style="dim", width=7)
    table.add_column("Label", style="bold", min_width=10)
    table.add_column("Email", style="dim", max_width=35)
    table.add_column("Usage", ratio=1, overflow="fold")
    table.add_column("Source", justify="right", width=11)
    table.add_column("Age", justify="right", width=10)

    for au in results:
        if au.provider == "claude":
            usage_cell = _claude_usage_cell(au)
        else:
            usage_cell = _grok_usage_cell(au)

        # Source styling
        src_style = {
            "api": "bold green",
            "cached": "dim",
            "backing-off": "yellow",
            "derived": "blue",
            "manual": "magenta",
            "error": "red",
            "no-data": "dim red",
        }.get(au.source, "")
        source_cell = Text(au.source, style=src_style)

        age_cell = _age_str(au.fetched_at)

        table.add_row(
            au.provider.capitalize(),
            au.label,
            au.email or "—",
            usage_cell,
            source_cell,
            age_cell,
        )

    console.print(table)


def render_status(results: list[AccountUsage]) -> None:
    """One-line aggregate summary."""
    claude_accts = [r for r in results if r.provider == "claude"]
    grok_accts = [r for r in results if r.provider == "grok"]

    parts: list[str] = []
    parts.append(f"{len(claude_accts)} Claude")
    parts.append(f"{len(grok_accts)} Grok")

    # Quick aggregate: highest Claude 7d usage
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
    """Render historical token-usage report from store.token_usage_since."""
    if not rows:
        console.print("[dim]No token usage recorded yet.[/dim]")
        return

    table = Table(title="Token Usage Report", show_lines=True, pad_edge=False)
    table.add_column("Provider", style="dim", width=7)
    table.add_column("Session", style="dim", max_width=36)
    table.add_column("Date", width=20)
    table.add_column("Model", max_width=20)
    table.add_column("In", justify="right")
    table.add_column("Out", justify="right")
    table.add_column("Cache", justify="right")
    table.add_column("Cost", justify="right", style="magenta")

    # Flatten rows — they come from token_usage table joined or not
    # rows is a list of sqlite3.Row
    for row in rows:
        ts = row["ts"]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        table.add_row(
            row["model"] or "—",
            "",  # session shortened
            dt,
            row["model"] or "—",
            f"{row['input_tokens'] or 0:,}",
            f"{row['output_tokens'] or 0:,}",
            f"{row['cache_tokens'] or 0:,}",
            f"${row['cost_estimate'] or 0:.4f}",
        )

    console.print(table)