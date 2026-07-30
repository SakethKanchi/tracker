"""Grok usage: OIDC refresh + identity + ccusage-style transcript parsing.

Live signals:
  - Weekly/monthly credits via cli-chat-proxy.grok.com billing endpoints
  - Blocked reason via api.x.ai/v1/models (403 with structured code)

Auth:
  - Tokens from ``grok login --oauth`` live in ~/.grok/auth.json
  - Refresh: POST {oidc_issuer}/oauth2/token  (grant_type=refresh_token)
  - Access token field is ``key``; expiry is ISO ``expires_at``

Session attribution (phase 1): all sessions under ~/.grok/sessions/ are
attributed to the Grok account being synced. Single-account use is correct;
multi-account historical session attribution is a phase-2 concern.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import paths

logger = logging.getLogger("tracker")

# costUsdTicks → USD. xAI uses 1 tick = $1e-9 (nano-dollar), so divide by 1e9.
TICKS_PER_USD = 1_000_000_000

DEFAULT_OIDC_ISSUER = "https://auth.x.ai"
DEFAULT_OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
EXPIRY_BUFFER_S = 5 * 60  # refresh if < 5 min left
USER_AGENT = "grok-cli/0.2.112"


@dataclass
class RefreshResult:
    credentials: dict | None  # updated auth blob, or None on failure
    error: str | None         # "invalid_grant" | "no_refresh_token" | "transient" | None


def access_token_of(blob: dict) -> str | None:
    """Return the bearer token from a grok auth blob."""
    tok = blob.get("key") or blob.get("access_token")
    return tok if isinstance(tok, str) and tok else None


def _decode_jwt_payload(access_token: str) -> dict[str, Any] | None:
    try:
        import base64
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _decode_jwt_tier(access_token: str) -> str | None:
    """Extract the ``tier`` claim from a Grok OIDC access token (JWT)."""
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return None
    tier = payload.get("tier")
    return str(tier) if tier is not None else None


def _parse_expires_at(value: Any) -> float | None:
    """Parse auth.json ``expires_at`` (ISO-8601, often with >6 fractional digits)."""
    if isinstance(value, (int, float)):
        # Heuristic: ms vs seconds
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Truncate fractional seconds to 6 digits (fromisoformat limit)
    if "." in s:
        head, rest = s.split(".", 1)
        digits = []
        tz_idx = 0
        for i, c in enumerate(rest):
            if c.isdigit():
                digits.append(c)
                tz_idx = i + 1
            else:
                tz_idx = i
                break
        frac = "".join(digits[:6]).ljust(6, "0")
        s = f"{head}.{frac}{rest[tz_idx:]}"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def expiry_ts(blob: dict) -> float:
    """Best-effort absolute expiry time (unix seconds). 0 if unknown."""
    ts = _parse_expires_at(blob.get("expires_at") or blob.get("expiresAt"))
    if ts is not None:
        return ts
    tok = access_token_of(blob)
    if tok:
        payload = _decode_jwt_payload(tok)
        if payload and isinstance(payload.get("exp"), (int, float)):
            return float(payload["exp"])
    return 0.0


def is_token_expired(blob: dict) -> bool:
    """True if the access token is missing or within the refresh buffer of expiry."""
    tok = access_token_of(blob)
    if not tok:
        return True
    exp = expiry_ts(blob)
    if exp <= 0:
        return True
    return time_now() + EXPIRY_BUFFER_S >= exp


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def refresh_token(blob: dict, timeout: float = 15.0) -> RefreshResult:
    """Refresh a Grok OIDC access token via auth.x.ai.

    Updates ``key``, ``expires_at``, and ``refresh_token`` (rotation) in-place
    on success. Returns a copy-friendly updated blob.
    """
    refresh_tok = blob.get("refresh_token") or blob.get("refreshToken")
    if not refresh_tok:
        return RefreshResult(None, "no_refresh_token")

    client_id = blob.get("oidc_client_id") or DEFAULT_OIDC_CLIENT_ID
    issuer = (blob.get("oidc_issuer") or DEFAULT_OIDC_ISSUER).rstrip("/")
    token_url = f"{issuer}/oauth2/token"

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": client_id,
    }).encode()

    req = urllib.request.Request(
        token_url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        lower = body_text.lower()
        if e.code in (400, 401, 403) and (
            "invalid_grant" in lower
            or "invalid_token" in lower
            or "expired" in lower
            or "revoked" in lower
        ):
            return RefreshResult(None, "invalid_grant")
        logger.debug("Grok refresh failed: %r, body: %s", e, body_text[:500])
        return RefreshResult(None, "transient")
    except Exception as e:
        logger.debug("Grok refresh failed: %r", e)
        return RefreshResult(None, "transient")

    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return RefreshResult(None, "transient")

    updated = dict(blob)
    updated["key"] = access
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        exp_dt = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
        updated["expires_at"] = exp_dt.isoformat().replace("+00:00", "Z")
    if isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
        updated["refresh_token"] = data["refresh_token"]
    return RefreshResult(updated, None)


def extract_identity(blob: dict) -> dict:
    """Extract account identity from a grok auth.json entry.

    The ``tier`` claim lives in the JWT payload, not the auth.json top level.
    """
    access_token = access_token_of(blob)
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


def _is_auth_failure(http_code: int, code: str, msg: str) -> bool:
    """True when the API rejected the bearer token (not a spending block).

    Real spending blocks are 403 with a structured team-blocked code. Auth
    failures show up as 401, as 403 ``unauthenticated:*``, or as 400
    ``Incorrect API key`` for non-JWT garbage.
    """
    if http_code == 401:
        return True
    c = (code or "").lower()
    m = (msg or "").lower()
    if "unauthenticated" in c or "bad-credentials" in c:
        return True
    if "could not be validated" in m or "invalid or expired credentials" in m:
        return True
    if "oauth2 access token" in m and ("validat" in m or "expir" in m):
        return True
    if "incorrect api key" in m or "invalid api key" in m:
        return True
    if http_code == 400 and ("api key" in m or "credentials" in m):
        return True
    return False


def check_live_quota(access_token: str, timeout: float = 5.0) -> dict[str, Any]:
    """Check Grok account quota status via api.x.ai/v1/models.

    Returns:
      {"status": "active"}  — account is healthy, has remaining quota
      {"status": "blocked", "reason": "spending-limit", "message": "..."}  — hit limit
      {"status": "error", "reason": "token-expired"|"network"|...}  — couldn't determine

    When the account hits its spending/weekly limit, api.x.ai returns 403 with a
    structured code like ``personal-team-blocked:spending-limit``. Auth failures
    also often arrive as 403 (``unauthenticated:bad-credentials``) and must not
    be mislabeled as "no quota".
    """
    url = "https://api.x.ai/v1/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": "active", "http_code": resp.status}
    except urllib.error.HTTPError as e:
        body: dict[str, Any] = {}
        try:
            raw = e.read().decode(errors="replace")
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                body = parsed
        except (json.JSONDecodeError, Exception):
            body = {}
        code = str(body.get("code") or "")
        msg = str(body.get("error") or body.get("message") or "")

        if _is_auth_failure(e.code, code, msg):
            return {
                "status": "error",
                "reason": "token-expired",
                "message": "access token invalid — run grok login --oauth, then tracker add grok",
            }

        if e.code == 403:
            # Parse the structured error code:
            # "personal-team-blocked:spending-limit" → blocked, reason=spending-limit
            if "spending-limit" in code:
                return {
                    "status": "blocked",
                    "reason": "spending-limit",
                    "message": "out of credits",
                }
            if "weekly-limit" in code or "weekly" in code.lower():
                return {
                    "status": "blocked",
                    "reason": "weekly-limit",
                    "message": "weekly limit reached",
                }
            if "free-usage" in code or "free" in code.lower():
                return {
                    "status": "blocked",
                    "reason": "free-usage-limit",
                    "message": "free usage limit hit",
                }
            return {
                "status": "blocked",
                "reason": code or "unknown",
                "message": msg or "blocked",
            }
        return {"status": "error", "reason": f"http-{e.code}", "message": msg or None}
    except Exception:
        return {"status": "error", "reason": "network"}


def fetch_credit_usage(access_token: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """Fetch live credit usage from the cli-chat-proxy billing endpoints.

    Two windows, both from the same proxy the grok CLI uses:

    1. Weekly credits  — ``/v1/billing?format=credits`` returns
       ``creditUsagePercent`` (0-100, % USED). This is the SuperGrok weekly
       credit window; hitting 100% blocks usage until the period resets.

    2. Monthly billing — ``/v1/billing`` returns ``used`` / ``monthlyLimit``
       (integer token-cost units). This is the monthly dollar-equivalent
       spend cap; it resets on the 1st of each month.

    Returns None on any failure so the caller falls back to transcript data.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    base = "https://cli-chat-proxy.grok.com/v1/billing"

    result: dict[str, Any] = {}

    # 1. Weekly credits
    try:
        req = urllib.request.Request(f"{base}?format=credits", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        cfg = data.get("config") or {}
        period = cfg.get("currentPeriod") or {}
        products = [
            {"product": p.get("product"), "usage_pct": p.get("usagePercent")}
            for p in (cfg.get("productUsage") or [])
        ]
        result["credit_usage_pct"] = cfg.get("creditUsagePercent")
        result["period_start"] = period.get("start")
        result["period_end"] = period.get("end")
        result["product_usage"] = products
    except Exception as e:
        logger.debug("grok weekly billing: %s", e)

    # 2. Monthly billing
    try:
        req = urllib.request.Request(base, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        cfg = data.get("config") or {}
        used = (cfg.get("used") or {}).get("val")
        limit = (cfg.get("monthlyLimit") or {}).get("val")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            result["monthly_used"] = used
            result["monthly_limit"] = limit
            result["monthly_pct"] = round(used / limit * 100, 1)
            result["monthly_period_end"] = cfg.get("billingPeriodEnd")
    except Exception as e:
        logger.debug("grok monthly billing: %s", e)

    return result if result else None