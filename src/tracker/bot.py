"""Discord dashboard bot — read-only usage via one slash command + refresh button.

Surface:
  /usage        → instant embed built from the latest stored SQLite sample
  [⟳ Refresh]  → forces a real sync (Claude API + Grok billing) and edits the embed

Hosting: same machine as tracker (reads the same store + credential files).
Access:  single owner (owner_id in ~/.config/tracker/discord.json).

This module imports ``discord`` lazily so the CLI stays light; the optional
``[bot]`` extra declares ``discord.py>=2.4``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import paths, store, usage
from .tui import BAR_WIDTH, _age_str, _reset_str

logger = logging.getLogger("tracker")

# ANSI 24-bit is flaky across Discord clients; we render plain unicode bars in a
# normal code block. Severity is conveyed by the embed's side color instead.
_EMBED_COLOR = {  # worst-account severity → embed color
    "green":  0x2ECC71,
    "yellow": 0xF1C40F,
    "red":    0xE74C3C,
    "dim":    0x95A5A6,
}

REFRESH_BUTTON_ID = "tracker:refresh"


def _config_path() -> Path:
    return paths.config_dir() / "discord.json"


def _ensure_discord() -> None:
    """Import discord on demand; raise with an install hint if missing."""
    try:
        import discord  # noqa: F401
        from discord import app_commands  # noqa: F401
    except ImportError as e:
        raise SystemExit(
            "discord.py is not installed. Install the bot extra:\n"
            "    uv tool install --force -e \".[bot]\""
        ) from e


def _severity(pct: float | None, blocked: bool) -> str:
    if blocked:
        return "red"
    if pct is None:
        return "dim"
    if pct >= 90:
        return "red"
    if pct >= 75:
        return "yellow"
    return "green"


def _plain_bar(pct: float | None) -> str:
    if pct is None:
        return "—"
    filled = int(round(pct / 100 * BAR_WIDTH))
    filled = max(0, min(BAR_WIDTH, filled))
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    return f"{bar} {pct:>3.0f}%"


def _render_account_block(au: usage.AccountUsage) -> tuple[str, str]:
    """Return (header_line, body_text) for one account."""
    header = au.label
    if au.email and au.email != au.label:
        header += f"  {au.email}"
    if au.tier:
        header += f"  t{au.tier}"

    lines: list[str] = []
    if au.error:
        lines.append(f"  error: {au.error}")
    elif not au.windows:
        lines.append("  no data")
    else:
        w = au.windows
        if au.provider == "claude":
            if au.needs_relogin:
                lines.append("  re-login needed — refresh token dead")
            else:
                for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
                    win = w.get(key)
                    if not win:
                        continue
                    pct = win.get("pct")
                    suffix = _reset_str(win.get("resets_at"))
                    line = f"  {label:<5} {_plain_bar(pct)}"
                    if suffix:
                        line += f"  {suffix}"
                    lines.append(line)
                for s in w.get("scoped") or []:
                    pct = s.get("pct")
                    suffix = _reset_str(s.get("resets_at"))
                    name = s["name"]
                    if len(name) > 6:
                        name = name[:4] + ".."
                    line = f"  {name:<5} {_plain_bar(pct)}"
                    if suffix:
                        line += f"  {suffix}"
                    lines.append(line)
                spend = w.get("spend")
                if spend:
                    lines.append(
                        f"  $$    ${spend['used']:.2f} / ${spend['limit']:.2f} "
                        f"({spend['pct']:.0f}%)"
                    )
        else:  # grok
            credit_pct = w.get("credit_usage_pct")
            if credit_pct is not None:
                bar = _plain_bar(credit_pct)
                reset = _reset_str(w.get("billing_period_end"))
                line = f"  wk    {bar}"
                if reset:
                    line += f"  {reset}"
                lines.append(line)
                products = w.get("product_usage") or []
                if len(products) > 1:
                    for p in products:
                        name = (p.get("product") or "?").lower()
                        label = "build" if "build" in name else name[:6]
                        pct = p.get("usage_pct")
                        if pct is not None:
                            lines.append(f"  {label:<5} {_plain_bar(pct)}")
            quota = w.get("quota_status")
            if quota == "blocked":
                msg = w.get("quota_message", w.get("quota_reason", ""))
                line = "  qta   no quota"
                if msg:
                    line += f"  {msg}"
                lines.append(line)
            elif quota == "active" and credit_pct is None:
                lines.append("  qta   has quota")
            last = w.get("last_activity")
            if last:
                lines.append(f"  last  {last[:10]}")

    return header, "\n".join(lines)


def _build_embed(results: list[usage.AccountUsage]) -> tuple[str, int]:
    """Return (code_block_text, embed_color) for the full dashboard."""
    # Worst severity across all accounts drives the embed color.
    worst = "dim"
    pct_for_severity: float | None = None
    for au in results:
        if au.provider == "claude":
            if au.needs_relogin:
                worst = "red"
                continue
            w = au.windows or {}
            for key in ("five_hour", "seven_day"):
                win = w.get(key) or {}
                pct = win.get("pct")
                if pct is not None:
                    s = _severity(pct, blocked=False)
                    if _sev_rank(s) > _sev_rank(worst):
                        worst = s
            for s in w.get("scoped") or []:
                pct = s.get("pct")
                if pct is not None:
                    sv = _severity(pct, blocked=False)
                    if _sev_rank(sv) > _sev_rank(worst):
                        worst = sv
        else:
            w = au.windows or {}
            pct = w.get("credit_usage_pct")
            blocked = w.get("quota_status") == "blocked"
            sv = _severity(pct, blocked)
            if _sev_rank(sv) > _sev_rank(worst):
                worst = sv

    # Group + render
    by_provider: dict[str, list[usage.AccountUsage]] = {}
    for au in results:
        by_provider.setdefault(au.provider, []).append(au)

    out: list[str] = []
    for provider in ("claude", "grok"):
        accts = by_provider.get(provider)
        if not accts:
            continue
        out.append(f"{provider.capitalize()}  ({len(accts)})")
        for au in accts:
            header, body = _render_account_block(au)
            out.append(header)
            if body:
                out.append(body)
        out.append("")

    text = "\n".join(out).rstrip()
    return text, _EMBED_COLOR.get(worst, _EMBED_COLOR["dim"])


def _sev_rank(s: str) -> int:
    return {"dim": 0, "green": 1, "yellow": 2, "red": 3}.get(s, 0)


def _newest_fetched_at(results: list[usage.AccountUsage]) -> float | None:
    ts = [au.fetched_at for au in results if au.fetched_at]
    return max(ts) if ts else None


def run_bot() -> int:
    """Start the Discord gateway client. Blocks until interrupted."""
    _ensure_discord()
    import discord
    from discord import app_commands

    cfg_path = _config_path()
    if not cfg_path.exists():
        print(f"Missing {cfg_path}. Create it with:\n"
              f'  {{"token": "...", "owner_id": <your_discord_id>, '
              f'"guild_id": <your_guild_id>}}')
        return 1
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {cfg_path}: {e}")
        return 1

    token = cfg.get("token")
    owner_id = int(cfg.get("owner_id", 0))
    guild_id = int(cfg.get("guild_id", 0))
    if not token or not owner_id or not guild_id:
        print(f"{cfg_path} must set token, owner_id, guild_id.")
        return 1

    intents = discord.Intents.default()
    # Slash commands and button interactions need no privileged intents.
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # In-flight sync guard: message_id -> asyncio.Event set when that message's
    # sync completes. Prevents stampede if the refresh button is mashed.
    _inflight: dict[int, asyncio.Event] = {}

    def _gather_cached() -> list[usage.AccountUsage]:
        conn = store.connect()
        return usage.read_cached_all(conn)

    def _gather_fresh() -> list[usage.AccountUsage]:
        conn = store.connect()
        return usage.collect_all(conn, force=True)

    def _make_embed(results: list[usage.AccountUsage]) -> discord.Embed:
        text, color = _build_embed(results)
        newest = _newest_fetched_at(results)
        age = _age_str(newest) or "never"
        embed = discord.Embed(
            description=f"```\n{text}\n```",
            color=color,
        )
        embed.set_footer(text=f"updated {age} · click ⟳ to refresh live")
        return embed

    def _refresh_button(disabled: bool = False) -> discord.ui.Button:
        return discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="⟳ Refresh",
            custom_id=REFRESH_BUTTON_ID,
            disabled=disabled,
        )

    def _make_view(disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(_refresh_button(disabled=disabled))
        return view

    @tree.command(
        name="usage",
        description="Show live Claude + Grok usage for all tracked accounts",
        guild=discord.Object(id=guild_id),
    )
    async def usage_cmd(interaction: discord.Interaction) -> None:
        if interaction.user.id != owner_id:
            await interaction.response.send_message(
                "not authorized", ephemeral=True)
            return
        results = _gather_cached()
        await interaction.response.send_message(
            embed=_make_embed(results), view=_make_view()
        )

    @client.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        if interaction.user.id != owner_id:
            if interaction.type == discord.InteractionType.component:
                await interaction.response.send_message(
                    "not authorized", ephemeral=True)
            return
        if interaction.type != discord.InteractionType.component:
            return
        if interaction.data.get("custom_id") != REFRESH_BUTTON_ID:
            return

        msg_id = interaction.message.id
        if msg_id in _inflight:
            # A sync is already running for this message; ignore the repeat.
            await interaction.response.defer()
            return

        evt = asyncio.Event()
        _inflight[msg_id] = evt

        # Acknowledge immediately; the sync takes ~6s.
        await interaction.response.edit_message(
            view=_make_view(disabled=True)
        )

        try:
            results = await asyncio.to_thread(_gather_fresh)
            await interaction.edit_original_response(
                embed=_make_embed(results), view=_make_view()
            )
        except Exception as e:
            logger.exception("refresh sync failed")
            try:
                await interaction.edit_original_response(
                    content=f"refresh failed: {e}", view=_make_view()
                )
            except Exception:
                pass
        finally:
            _inflight.pop(msg_id, None)
            evt.set()

    @client.event
    async def on_ready() -> None:
        guild = discord.Object(id=guild_id)
        try:
            synced = await tree.sync(guild=guild)
            logger.info("synced %d command(s) to guild %s", len(synced), guild_id)
        except Exception:
            logger.exception("command sync failed")
        logger.info("tracker bot ready as %s (guild=%d)", client.user, guild_id)

    client.run(token, log_level=logging.INFO)
    return 0