"""Grok usage: identity from auth.json + ccusage-style transcript parsing.

Grok has no live quota API. Usage is derived from local session transcripts:
  ~/.grok/sessions/<url-encoded-cwd>/<session-uuid>/updates.jsonl
Each ``turn_completed`` event in updates.jsonl carries a ``usage`` object:
  {inputTokens, outputTokens, totalTokens, cachedReadTokens, reasoningTokens,
   modelCalls, costUsdTicks, modelUsage: {<model>: {...}}}

Session attribution (phase 1): all sessions under ~/.grok/sessions/ are
attributed to the Grok account being synced. Single-account use is correct;
multi-account historical session attribution is a phase-2 concern.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import paths

logger = logging.getLogger("tracker")

# costUsdTicks → USD. xAI uses 1 tick = $1e-9 (nano-dollar), so divide by 1e9.
TICKS_PER_USD = 1_000_000_000


def _decode_jwt_tier(access_token: str) -> str | None:
    """Extract the ``tier`` claim from a Grok OIDC access token (JWT)."""
    try:
        import base64
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        # JWT middle segment: base64url, may need padding
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        tier = payload.get("tier")
        return str(tier) if tier is not None else None
    except Exception:
        return None


def extract_identity(blob: dict) -> dict:
    """Extract account identity from a grok auth.json entry.

    The ``tier`` claim lives in the JWT payload, not the auth.json top level.
    """
    access_token = blob.get("key") or blob.get("access_token")
    return {
        "email": blob.get("email"),
        "user_id": blob.get("user_id"),
        "principal_id": blob.get("principal_id"),
        "team_id": blob.get("team_id"),
        "tier": _decode_jwt_tier(access_token) if access_token else None,
    }


def _iter_session_updates() -> Iterator[tuple[str, str, Path]]:
    """Yield (session_id, project_cwd, updates_path) for every grok session."""
    sessions = paths.GROK_SESSIONS_DIR
    if not sessions.is_dir():
        return
    for proj_dir in sessions.iterdir():
        if not proj_dir.is_dir():
            continue
        project_cwd = _url_decode(proj_dir.name)
        for sess_dir in proj_dir.iterdir():
            if not sess_dir.is_dir() or not _is_uuid(sess_dir.name):
                continue
            updates = sess_dir / "updates.jsonl"
            if updates.is_file():
                yield sess_dir.name, project_cwd, updates


def _url_decode(name: str) -> str:
    from urllib.parse import unquote
    return unquote(name)


def _is_uuid(s: str) -> bool:
    if len(s) != 36:
        return False
    return s.count("-") == 4


def _parse_updates_file(
    path: Path, seen_event_ids: set[str]
) -> list[dict[str, Any]]:
    """Parse a single updates.jsonl, returning token-usage row dicts.

    Skips events we've already seen (by _meta.eventId) to dedupe across syncs.
    """
    rows: list[dict[str, Any]] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                params = event.get("params")
                if not isinstance(params, dict):
                    continue
                update = params.get("update")
                if not isinstance(update, dict):
                    continue
                if update.get("sessionUpdate") != "turn_completed":
                    continue

                meta = event.get("_meta") or {}
                event_id = meta.get("eventId") or ""
                if event_id and event_id in seen_event_ids:
                    continue

                usage = update.get("usage")
                if not isinstance(usage, dict):
                    continue

                ts = event.get("timestamp")
                if not isinstance(ts, (int, float)):
                    continue

                # modelUsage may have per-model breakdown; use the aggregate
                # usage object as the primary row (it's the sum)
                cost_ticks = usage.get("costUsdTicks") or 0
                row = {
                    "session_id": params.get("sessionId", path.parent.name),
                    "ts": float(ts),
                    "model": _primary_model(usage),
                    "input_tokens": usage.get("inputTokens"),
                    "output_tokens": usage.get("outputTokens"),
                    "cache_tokens": usage.get("cachedReadTokens"),
                    "reasoning_tokens": usage.get("reasoningTokens"),
                    "cost_usd_ticks": cost_ticks,
                    "cost_estimate": cost_ticks / TICKS_PER_USD if cost_ticks else None,
                }
                rows.append(row)
                if event_id:
                    seen_event_ids.add(event_id)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to parse grok updates %s: %r", path, e)
    return rows


def _primary_model(usage: dict) -> str | None:
    """Return the model name from modelUsage if there's exactly one entry."""
    model_usage = usage.get("modelUsage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        return next(iter(model_usage.keys()))
    if isinstance(model_usage, dict) and len(model_usage) > 1:
        return "mixed"
    return None


def get_seen_event_ids(conn, account_id: str) -> set[str]:
    """Return event IDs already stored for this account (for dedup).

    We don't store event IDs directly; instead we dedupe by (session_id, ts)
    via INSERT OR IGNORE on the token_usage table. This function returns an
    empty set — dedup is handled by the unique-ish (session_id, ts) pair.
    """
    # Fetch existing (session_id, ts) pairs for this account to dedupe in-memory
    rows = conn.execute(
        "SELECT session_id, ts FROM token_usage WHERE account_id=?",
        (account_id,),
    ).fetchall()
    return set()  # DB-level dedup via INSERT OR IGNORE handles this


def parse_all_sessions(account_id: str) -> list[dict[str, Any]]:
    """Parse every grok session transcript, returning token-usage rows.

    Each row is ready for store.insert_token_usage (needs account_id added).
    De-duplication is deferred to the DB layer (INSERT OR IGNORE).

    Phase-1 limitation: all sessions attributed to account_id regardless of
    which grok login was active at the time.
    """
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session_id, _project, updates_path in _iter_session_updates():
        for row in _parse_updates_file(updates_path, seen):
            row["account_id"] = account_id
            all_rows.append(row)
    return all_rows


def derive_usage_summary(conn, account_id: str) -> dict[str, Any]:
    """Build a derived usage_sample from aggregated token_usage.

    Returns a windows dict like:
      {total_tokens, total_cost, session_count, last_rate_limit, last_activity}
    """
    agg = conn.execute(
        """SELECT
             COUNT(DISTINCT session_id) AS sessions,
             SUM(input_tokens) AS input,
             SUM(output_tokens) AS output,
             SUM(cache_tokens) AS cache,
             SUM(cost_estimate) AS cost,
             MAX(ts) AS last_ts
           FROM token_usage WHERE account_id=?""",
        (account_id,),
    ).fetchone()

    windows: dict[str, Any] = {
        "total_input": agg["input"] or 0,
        "total_output": agg["output"] or 0,
        "total_cache": agg["cache"] or 0,
        "total_cost": round(agg["cost"] or 0, 4),
        "session_count": agg["sessions"] or 0,
        "last_activity": datetime.fromtimestamp(
            agg["last_ts"], tz=timezone.utc
        ).isoformat() if agg["last_ts"] else None,
    }

    # Check for rate-limit events
    rl = conn.execute(
        "SELECT * FROM rate_limit_events WHERE account_id=? ORDER BY ts DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if rl:
        windows["last_rate_limit"] = {
            "kind": rl["kind"],
            "at": datetime.fromtimestamp(rl["ts"], tz=timezone.utc).isoformat(),
        }

    return windows


def check_live_quota(access_token: str, timeout: float = 5.0) -> dict[str, Any]:
    """Check Grok account quota status via api.x.ai/v1/models.

    Returns:
      {"status": "active"}  — account is healthy, has remaining quota
      {"status": "blocked", "reason": "spending-limit", "message": "..."}  — hit limit
      {"status": "error", "reason": ...}  — couldn't determine

    This is the closest thing Grok has to a live usage endpoint: when the
    account hits its spending limit / weekly limit / runs out of credits,
    api.x.ai returns 403 with a structured error code. When healthy, it
    returns 200 with the model catalog.
    """
    import urllib.request
    import urllib.error

    url = "https://api.x.ai/v1/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": "active", "http_code": resp.status}
    except urllib.error.HTTPError as e:
        if e.code == 403:
            try:
                body = json.loads(e.read().decode())
                code = body.get("code", "")
                msg = body.get("error", "")
                # Parse the structured error code:
                # "personal-team-blocked:spending-limit" → blocked, reason=spending-limit
                if "spending-limit" in code:
                    return {
                        "status": "blocked",
                        "reason": "spending-limit",
                        "message": "out of credits — add credits at grok.com/?_s=usage",
                    }
                if "weekly-limit" in code or "weekly" in code.lower():
                    return {
                        "status": "blocked",
                        "reason": "weekly-limit",
                        "message": "weekly limit reached — resets next cycle",
                    }
                if "free-usage" in code or "free" in code.lower():
                    return {
                        "status": "blocked",
                        "reason": "free-usage-limit",
                        "message": "free usage limit hit — upgrade at grok.com/supergrok",
                    }
                # Generic blocked
                return {
                    "status": "blocked",
                    "reason": code or "unknown",
                    "message": msg or "account blocked",
                }
            except (json.JSONDecodeError, Exception):
                return {"status": "error", "reason": f"http-{e.code}"}
        if e.code == 401:
            return {"status": "error", "reason": "token-expired",
                    "message": "access token expired — run grok login --oauth"}
        return {"status": "error", "reason": f"http-{e.code}"}
    except Exception as e:
        return {"status": "error", "reason": "network"}

def fetch_credit_usage(access_token: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """Fetch live credit usage from the cli-chat-proxy billing endpoint.

    This is the same percentage Grok shows in its own UI: the weekly
    SuperGrok credit window. The endpoint is
    ``https://cli-chat-proxy.grok.com/v1/billing?format=credits`` and returns:

      {"config": {
        "currentPeriod": {"start", "end", "type": "...WEEKLY"},
        "creditUsagePercent": float,     # 0-100, percent USED
        "productUsage": [{"product": "GrokBuild", "usagePercent": float}],
        "isUnifiedBillingUser": bool,
        "prepaidBalance": {"val": int},
      }}

    Returns None on any failure so the caller falls back to transcript data.
    """
    import urllib.request
    import urllib.error

    url = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "grok-cli/0.2.112",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        logger.debug("grok billing http-%s: %s", e.code, e.read()[:200])
        return None
    except Exception as e:
        logger.debug("grok billing network: %s", e)
        return None

    cfg = data.get("config") or {}
    period = cfg.get("currentPeriod") or {}
    products = [
        {"product": p.get("product"), "usage_pct": p.get("usagePercent")}
        for p in (cfg.get("productUsage") or [])
    ]
    return {
        "credit_usage_pct": cfg.get("creditUsagePercent"),
        "period_start": period.get("start"),
        "period_end": period.get("end"),
        "product_usage": products,
    }