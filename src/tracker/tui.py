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
MIN_BAR_WIDTH = 8

# Longest trailing text a bar line can carry, e.g. "  resets in 27d23h".
_RESET_SUFFIX_BUDGET = 20
# "  <connector> <label> " + " 100%"
_TREE_CHROME_BUDGET = 16


def _term_width() -> int:
    """Current terminal width, with a sane fallback for pipes and CI."""
    try:
        cols = console.size.width
    except Exception:  # pragma: no cover - not a tty
        return 80
    return cols if cols and cols > 0 else 80


def _bar_width() -> int:
    """Bar width that fits the current terminal.

    The tree lines are ``  ├ <label> <bar> <pct>  resets in …``. On an 80-col
    terminal the full 20-char bar fits; on a narrow pane it must shrink or the
    line wraps and the tree connectors break apart.
    """
    cols = _term_width()
    available = cols - _TREE_CHROME_BUDGET - _RESET_SUFFIX_BUDGET
    return max(MIN_BAR_WIDTH, min(BAR_WIDTH, available))


def _age_str(fetched_at: float | None) -> str:
    """How long ago the sample was fetched: '3s', '45m', '3h', '1.2d'."""
    if not fetched_at:
        return ""
    delta = time.time() - fetched_at
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta/60:.0f}m ago"
    if delta < 86400:
        return f"{delta/3600:.0f}h ago"
    return f"{delta/86400:.1f}d ago"


def _reset_str(resets_at: str | None) -> str:
    """Time until the usage window resets: '45m', '20h50m', '5d17h'."""
    if not resets_at:
        return ""
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        delta = dt.timestamp() - time.time()
        if delta <= 0:
            return "resets now"
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        d = h // 24
        if d > 0:
            return f"resets in {d}d{h - d * 24}h"
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


def _bar(pct: float | None, width: int | None = None) -> Text:
    """A progress bar followed by the percentage.

    ``width`` defaults to whatever fits the current terminal. Percentages are
    clamped to 0-100 for the bar itself, but the true value is still printed so
    a provider reporting >100% stays visible rather than being silently hidden.
    """
    if pct is None:
        return Text("—", style="dim")
    w = width if width is not None else _bar_width()
    clamped = max(0.0, min(100.0, float(pct)))
    filled = int(round(clamped / 100 * w))
    filled = max(0, min(w, filled))
    bar = "█" * filled + "░" * (w - filled)
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
    """Build the indented lines for a Grok account's usage.

    (``/v1/billing?format=credits``), which returns ``creditUsagePercent`` for
    the weekly SuperGrok credit window — the same percentage grok.com's UI
    shows. When billing is unreachable we fall back to the binary
    active/blocked flag from ``api.x.ai/v1/models``.
    """
    if au.error and not au.windows:
        return [Text(f"  {au.error}", style="red")]
    if not au.windows:
        return [Text("  no data", style="dim")]

    w = au.windows
    # API-key mode shares the generic health formatter
    if w.get("auth_type") == "api_key":
        return _format_windows_apikey(au)

    lines: list[tuple[str, Text]] = []

    credit_pct = w.get("credit_usage_pct")
    if credit_pct is not None:
        # The weekly SuperGrok credit window — the main bar grok.com shows.
        bar = _bar(credit_pct)
        reset = _reset_str(w.get("billing_period_end"))
        if reset:
            bar.append(f"  {reset}", style="dim")
        lines.append(("wk", bar))

        # Per-product bars only add signal when there's more than one product;
        # with a single product (the common case) it duplicates the weekly bar.
        products = w.get("product_usage") or []
        if len(products) > 1:
            for p in products:
                name = (p.get("product") or "?").lower()
                # "grokbuild" -> "build"; generic fallback: first 6 chars
                label = "build" if "build" in name else name[:6]
                pct = p.get("usage_pct")
                if pct is not None:
                    lines.append((label, _bar(pct)))

    # Monthly billing window — independent of weekly credits field
    # (api sometimes omits creditUsagePercent for unified-billing accounts).
    monthly_pct = w.get("monthly_pct")
    if monthly_pct is not None:
        mbar = _bar(monthly_pct)
        mreset = _reset_str(w.get("monthly_period_end"))
        if mreset:
            mbar.append(f"  {mreset}", style="dim")
        lines.append(("mo", mbar))

    # Actionable quota / auth hint from v1/models (when billing is missing or 100%).
    quota = w.get("quota_status")
    if quota == "blocked":
        msg = w.get("quota_message", w.get("quota_reason", ""))
        parts: list[tuple[str, str]] = [("no quota", "bold red")]
        if msg:
            parts.append((f"  {msg}", "red"))
        lines.append(("qta", Text.assemble(*parts)))
    elif quota == "error":
        reason = w.get("quota_reason", "")
        msg = w.get("quota_message") or reason or "auth error"
        if reason == "token-expired" or au.needs_relogin:
            label = "auth expired"
        else:
            label = "auth error"
        lines.append(("qta", Text.assemble(
            (label, "bold red"),
            (f"  {msg}", "red"),
        )))
    elif quota == "active" and credit_pct is None:
        lines.append(("qta", Text("has quota", style="green")))

    # Last activity (from transcript parsing)
    last = w.get("last_activity")
    if last:
        lines.append(("last", Text(f" {last[:10]}", style="dim")))

    # Top-level auth/refresh error (e.g. dead refresh token) alongside any windows.
    if au.error and quota != "error":
        lines.append(("err", Text(f" {au.error}", style="red")))

    return _format_tree_lines(lines)


def _format_windows_codex(au: AccountUsage) -> list[Text]:
    """Codex / ChatGPT subscription windows (primary + secondary)."""
    if au.needs_relogin:
        return [Text("  re-login needed — refresh token dead", style="bold red")]
    if au.error and not au.windows:
        return [Text(f"  {au.error}", style="red")]
    if not au.windows:
        return [Text("  no data", style="dim")]

    w = au.windows
    lines: list[tuple[str, Text]] = []

    for key, default_label in (("primary", "pri"), ("secondary", "sec")):
        win = w.get(key)
        if not win:
            continue
        pct = win.get("pct")
        mins = win.get("window_minutes")
        if mins and mins <= 360:
            label = "5h"
        elif mins and mins <= 60 * 24 * 8:
            label = "wk"
        elif mins:
            label = "mo"
        else:
            label = win.get("name") or default_label
        bar = _bar(pct)
        suffix = _reset_str(win.get("resets_at"))
        if suffix:
            bar.append(f"  {suffix}", style="dim")
        lines.append((str(label)[:6], bar))

    for s in w.get("scoped") or []:
        pct = s.get("pct")
        bar = _bar(pct)
        suffix = _reset_str(s.get("resets_at"))
        if suffix:
            bar.append(f"  {suffix}", style="dim")
        name = str(s.get("name") or "extra")[:6]
        lines.append((name, bar))

    credits = w.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        if credits.get("unlimited"):
            lines.append(("cr", Text(" unlimited", style="green")))
        elif credits.get("balance") is not None:
            lines.append(("cr", Text(f" {credits['balance']}", style="magenta")))

    if w.get("limit_reached") or w.get("reached_type"):
        msg = w.get("reached_type") or "limit reached"
        lines.append(("lim", Text(f" {msg}", style="bold red")))

    if au.error:
        lines.append(("err", Text(f" {au.error}", style="red")))

    return _format_tree_lines(lines) if lines else [Text("  no windows", style="dim")]


def _format_windows_apikey(au: AccountUsage) -> list[Text]:
    """Generic formatter for API-key accounts (gemini/openai/claude-key/grok-key)."""
    if au.error and not au.windows:
        return [Text(f"  {au.error}", style="red")]
    if not au.windows:
        return [Text("  no data", style="dim")]

    w = au.windows
    lines: list[tuple[str, Text]] = []

    quota = w.get("quota_status")
    if quota == "active":
        lines.append(("key", Text(" valid", style="green")))
    elif quota == "blocked":
        msg = w.get("quota_message") or w.get("quota_reason") or "blocked"
        lines.append(("key", Text.assemble(
            (" blocked", "bold red"),
            (f"  {msg}", "red"),
        )))
    elif quota == "error":
        msg = w.get("quota_message") or w.get("quota_reason") or "auth error"
        lines.append(("key", Text.assemble(
            (" invalid", "bold red"),
            (f"  {msg}", "red"),
        )))
    else:
        lines.append(("key", Text(f" {quota or 'unknown'}", style="dim")))

    if w.get("model_count") is not None:
        lines.append(("models", Text(f" {w['model_count']}", style="dim")))

    samples = w.get("sample_models") or []
    if samples:
        shown = ", ".join(samples[:2])
        lines.append(("e.g.", Text(f" {shown}", style="dim")))

    note = w.get("note")
    if note and not samples:
        # Shorten long notes
        short = note if len(note) <= 60 else note[:57] + "..."
        lines.append(("", Text(f" {short}", style="dim")))

    if au.error and quota != "error":
        lines.append(("err", Text(f" {au.error}", style="red")))

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
    # Only pad non-empty labels; empty-label lines get no trailing space
    nonempty = [len(l) for l, _ in lines if l]
    pad = max(nonempty) if nonempty else 0
    result: list[Text] = []
    for i, (label, content) in enumerate(lines):
        is_last = i == len(lines) - 1
        connector = "└" if is_last else "├"
        if label:
            result.append(Text.assemble(
                (f"  {connector} ", "dim"),
                (f"{label:<{pad}} ", "dim"),
                content,
            ))
        else:
            result.append(Text.assemble(
                (f"  {connector} ", "dim"),
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


_PROVIDER_LABELS = {
    "claude": "Claude",
    "grok": "Grok",
    "codex": "Codex",
    "gemini": "Gemini",
    "openai": "OpenAI",
}


def _format_windows_for(au: AccountUsage) -> list[Text]:
    if au.windows and au.windows.get("auth_type") == "api_key":
        return _format_windows_apikey(au)
    if au.provider == "claude":
        return _format_windows_claude(au)
    if au.provider == "grok":
        return _format_windows_grok(au)
    if au.provider == "codex":
        return _format_windows_codex(au)
    if au.provider in ("gemini", "openai"):
        return _format_windows_apikey(au)
    return _format_windows_apikey(au)


def render_accounts(results: list[AccountUsage]) -> None:
    """Render the all-accounts usage dashboard as a compact tree."""
    if not results:
        console.print(
            "[dim]No accounts added yet. Run:[/dim]  "
            "tracker add claude|grok|codex  [dim]or[/dim]  tracker add <api_key>"
        )
        return

    # Group by provider
    by_provider: dict[str, list[AccountUsage]] = {}
    for au in results:
        by_provider.setdefault(au.provider, []).append(au)

    lines: list[Text] = []
    provider_order = ["claude", "grok", "codex", "gemini", "openai"]
    # Include any unknown providers at the end
    for p in by_provider:
        if p not in provider_order:
            provider_order.append(p)

    rendered_providers = 0
    for provider in provider_order:
        accounts = by_provider.get(provider, [])
        if not accounts:
            continue

        if rendered_providers > 0:
            lines.append(Text(""))
        rendered_providers += 1

        title = _PROVIDER_LABELS.get(provider, provider.capitalize())
        lines.append(Text.assemble(
            (title, "bold"),
            (f"  ({len(accounts)})", "dim"),
        ))

        for ai, au in enumerate(accounts):
            header = Text.assemble(
                (f"  {au.label}", "bold"),
            )
            # Drop the email when it would push the header past the terminal
            # width (it is redundant with the label in most setups anyway).
            tag_len = len(au.source) + len(_age_str(au.fetched_at)) + 6
            budget = _term_width() - len(au.label) - tag_len - 4
            if au.email and au.email != au.label:
                if len(au.email) <= budget:
                    header.append(Text(f"  {au.email}", style="dim"))
                elif budget > 6:
                    header.append(Text(f"  {au.email[:budget - 2]}…", style="dim"))
            if au.tier and au.tier != "api_key":
                header.append(Text(f"  {au.tier}", style="dim"))
            elif au.tier == "api_key":
                header.append(Text("  api-key", style="dim"))
            header.append(Text("  [", style="dim"))
            header.append(_source_tag(au))
            header.append(Text("]", style="dim"))
            lines.append(header)

            for wl in _format_windows_for(au):
                lines.append(wl)

            if ai < len(accounts) - 1:
                lines.append(Text(""))

    console.print(Text("\n").join(lines) if lines else "")


def _headroom_pct(au: AccountUsage) -> float | None:
    """Remaining headroom (0-100) for an account, or None if unknown.

    The dashboard shows *usage*; the question a user actually asks is "which
    account has room left?". Headroom is ``100 - worst window used``: the
    binding constraint, since hitting any one window blocks the account.
    """
    w = au.windows or {}
    if au.needs_relogin:
        return None
    if w.get("quota_status") == "blocked" or w.get("limit_reached"):
        return 0.0

    used: list[float] = []

    def _take(v: object) -> None:
        if isinstance(v, (int, float)):
            used.append(float(v))

    for key in ("five_hour", "seven_day", "primary", "secondary"):
        win = w.get(key)
        if isinstance(win, dict):
            _take(win.get("pct"))
    for key in ("credit_usage_pct", "monthly_pct"):
        _take(w.get(key))
    for s in w.get("scoped") or []:
        if isinstance(s, dict):
            _take(s.get("pct"))

    if not used:
        return None
    return max(0.0, 100.0 - max(used))


def _best_account(results: list[AccountUsage]) -> AccountUsage | None:
    """The account with the most headroom, for the 'use this next' hint."""
    scored = [
        (h, au) for au, h in ((au, _headroom_pct(au)) for au in results)
        if h is not None and h > 0
    ]
    if not scored:
        return None
    return max(scored, key=lambda t: t[0])[1]


def render_status(results: list[AccountUsage]) -> None:
    """One-line aggregate summary."""
    parts: list[str] = []
    for provider in ("claude", "grok", "codex", "gemini", "openai"):
        n = sum(1 for r in results if r.provider == provider)
        if n:
            parts.append(f"{n} {_PROVIDER_LABELS.get(provider, provider)}")

    if not parts:
        parts.append("0 accounts")

    claude_accts = [r for r in results if r.provider == "claude"]
    max_7d: float | None = None
    for r in claude_accts:
        if r.windows and r.windows.get("seven_day"):
            pct = r.windows["seven_day"].get("pct")
            if pct is not None and (max_7d is None or pct > max_7d):
                max_7d = pct
    if max_7d is not None:
        parts.append(f"max 7d: {max_7d:.0f}%")

    relogin_count = sum(1 for r in results if r.needs_relogin)
    if relogin_count:
        parts.append(f"{relogin_count} need re-login")

    grok_blocked = sum(
        1 for r in results
        if r.provider == "grok" and r.windows and r.windows.get("quota_status") == "blocked"
    )
    if grok_blocked:
        parts.append(f"{grok_blocked} Grok blocked")

    console.print("  · ".join(parts))

    # The actionable line: which account still has room.
    best = _best_account(results)
    if best is not None:
        headroom = _headroom_pct(best)
        label = best.label or best.email or best.provider
        console.print(Text.assemble(
            ("  best: ", "dim"),
            (f"{_PROVIDER_LABELS.get(best.provider, best.provider)} {label}", "bold green"),
            (f"  {headroom:.0f}% free", "green"),
        ))


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