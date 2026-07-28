"""Discord webhook dashboard — posts live usage to a channel on a schedule.

Posts the full Claude + Grok usage dashboard to a Discord channel via webhook,
then edits that same message on each sync cycle. No bot token, no gateway
WebSocket, no discord.py dependency — just urllib POST/PATCH against the
Discord webhook REST API.

  tracker webhook          # run the poller (blocking)
  tracker webhook --once   # post/update once and exit

Config: ~/.config/tracker/webhook.json (0600):
  {"url": "https://discord.com/api/webhooks/...", "interval_sec": 300}

Deploy: systemd user service at ~/.config/systemd/user/tracker-bot.service.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import paths, store, usage
from .tui import BAR_WIDTH, _age_str, _reset_str

logger = logging.getLogger("tracker")

DEFAULT_INTERVAL_SEC = 300  # 5 minutes

_EMBED_COLOR = {
    "green":  0x2ECC71,
    "yellow": 0xF1C40F,
    "red":    0xE74C3C,
    "dim":    0x95A5A6,
}


# ── rendering ──────────────────────────────────────────────────────────────
# Same plain-bar rendering as the interactive bot; shared so the webhook
# dashboard looks identical to what the slash command would have shown.

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


def _sev_rank(s: str) -> int:
    return {"dim": 0, "green": 1, "yellow": 2, "red": 3}.get(s, 0)


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


def _build_text(results: list[usage.AccountUsage]) -> tuple[str, int]:
    """Return (code_block_text, embed_color) for the full dashboard."""
    worst = "dim"
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


def _newest_fetched_at(results: list[usage.AccountUsage]) -> float | None:
    ts = [au.fetched_at for au in results if au.fetched_at]
    return max(ts) if ts else None


# ── Discord webhook REST ────────────────────────────────────────────────────

def _config_path() -> Path:
    return paths.config_dir() / "webhook.json"


def _load_config() -> dict | None:
    cfg_path = _config_path()
    if not cfg_path.exists():
        print(f"Missing {cfg_path}. Create it with:\n"
              f'  {{"url": "https://discord.com/api/webhooks/...", '
              f'"interval_sec": 300}}')
        return None
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {cfg_path}: {e}")
        return None
    if not cfg.get("url"):
        print(f"{cfg_path} must set 'url'.")
        return None
    return cfg


def _build_payload(results: list[usage.AccountUsage]) -> dict:
    """Build the Discord webhook message payload."""
    text, color = _build_text(results)
    newest = _newest_fetched_at(results)
    age = _age_str(newest) or "never"
    return {
        "embeds": [{
            "description": f"```\n{text}\n```",
            "color": color,
            "footer": {"text": f"updated {age} · auto-refresh every {DEFAULT_INTERVAL_SEC // 60}m"},
        }],
    }


def _post_message(url: str, payload: dict, timeout: float = 10.0) -> str | None:
    """POST a new message; return the message ID, or None on failure."""
    import urllib.request
    import urllib.error

    api_url = url + "?wait=true"  # wait=true returns the message object
    data = json.dumps(payload).encode()
    req = urllib.request.Request(api_url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "tracker/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            return body.get("id")
    except urllib.error.HTTPError as e:
        logger.error("webhook POST http-%s: %s", e.code, e.read()[:300])
        return None
    except Exception as e:
        logger.error("webhook POST: %s", e)
        return None


def _patch_message(url: str, message_id: str, payload: dict,
                   timeout: float = 10.0) -> bool:
    """Edit an existing webhook message by ID."""
    import urllib.request
    import urllib.error

    # Extract webhook_id and webhook_token from the URL
    # https://discord.com/api/webhooks/{webhook_id}/{webhook_token}
    parts = url.rstrip("/").split("/")
    if len(parts) < 2:
        logger.error("invalid webhook URL: %s", url)
        return False
    webhook_id = parts[-2]
    webhook_token = parts[-1]
    api_url = f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(api_url, data=data, method="PATCH", headers={
        "Content-Type": "application/json",
        "User-Agent": "tracker/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        logger.error("webhook PATCH http-%s: %s", e.code, e.read()[:300])
        return False
    except Exception as e:
        logger.error("webhook PATCH: %s", e)
        return False


# ── poller ──────────────────────────────────────────────────────────────────

def _gather_fresh() -> list[usage.AccountUsage]:
    """Force-sync all accounts and return the results."""
    conn = store.connect()
    return usage.collect_all(conn, force=True)


def _sync_and_post(url: str, message_id: str | None) -> str | None:
    """Sync all accounts, post or edit the dashboard message, return its ID."""
    results = _gather_fresh()
    payload = _build_payload(results)

    if message_id:
        if _patch_message(url, message_id, payload):
            return message_id
        # Edit failed (message deleted?) — fall through to post a new one
        logger.warning("webhook edit failed, posting new message")

    return _post_message(url, payload)


def run_webhook(once: bool = False) -> int:
    """Run the webhook poller loop. Blocks until interrupted."""
    cfg = _load_config()
    if not cfg:
        return 1
    url = cfg["url"]
    interval = cfg.get("interval_sec", DEFAULT_INTERVAL_SEC)

    # Persist the last message ID so we edit across restarts
    state_path = paths.data_dir() / "webhook_message_id"

    message_id: str | None = None
    if state_path.exists():
        message_id = state_path.read_text().strip() or None

    # Graceful shutdown on SIGINT/SIGTERM (systemd sends SIGTERM)
    _stop = False

    def _handle_signal(signum, frame):
        nonlocal _stop
        _stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("tracker webhook started (interval=%ds, url=…%s)",
                interval, url[-12:])

    while not _stop:
        new_id = _sync_and_post(url, message_id)
        if new_id and new_id != message_id:
            message_id = new_id
            state_path.write_text(message_id)
            logger.info("webhook message posted (id=%s)", message_id)
        elif new_id:
            logger.info("webhook message updated (id=%s)", message_id)
        else:
            logger.warning("webhook post/edit failed; will retry next cycle")

        if once:
            break
        # Sleep in 1s increments so signals are responsive
        for _ in range(interval):
            if _stop:
                break
            time.sleep(1)

    logger.info("tracker webhook stopped")
    return 0