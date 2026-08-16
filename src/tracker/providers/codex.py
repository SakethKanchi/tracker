"""Codex / ChatGPT subscription usage: OAuth refresh + WHAM usage API.

Auth source: ``~/.codex/auth.json`` (ChatGPT login mode)

  {
    "auth_mode": "chatgpt",
    "tokens": {
      "id_token": "...",
      "access_token": "...",
      "refresh_token": "...",
      "account_id": "..."
    },
    "last_refresh": "..."
  }

Refresh (rotating single-use refresh tokens):
  POST https://auth.openai.com/oauth/token
  body: {client_id, grant_type=refresh_token, refresh_token}
  client_id: app_EMoamEEZ73f0CkXaXp7hrann

Usage:
  GET https://chatgpt.com/backend-api/wham/usage
  Authorization: Bearer <access_token>
  ChatGPT-Account-Id: <account_id>   (when known)

IMPORTANT: Codex refresh tokens are single-use. After a successful refresh the
new tokens MUST be written back to both the tracker credential store AND the
live ``~/.codex/auth.json`` so the Codex CLI is not left with a dead grant
(``refresh_token_reused``).
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("tracker")

OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USER_AGENT = "codex-cli"
EXPIRY_BUFFER_S = 5 * 60  # refresh if access token < 5 min left


@dataclass
class RefreshResult:
    credentials: dict | None  # full auth.json-shaped blob, or None
    error: str | None         # "invalid_grant" | "no_refresh_token" | "transient" | None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def access_token_of(blob: dict) -> str | None:
    """Return the ChatGPT access token from an auth.json-shaped blob."""
    tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else blob
    tok = tokens.get("access_token") if isinstance(tokens, dict) else None
    return tok if isinstance(tok, str) and tok else None


def refresh_token_of(blob: dict) -> str | None:
    tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else blob
    tok = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    return tok if isinstance(tok, str) and tok else None


def account_id_of(blob: dict) -> str | None:
    tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else blob
    if isinstance(tokens, dict):
        aid = tokens.get("account_id")
        if isinstance(aid, str) and aid:
            return aid
    # Fall back to JWT claims
    access = access_token_of(blob)
    if access:
        payload = _decode_jwt_payload(access)
        if payload:
            auth = payload.get("https://api.openai.com/auth") or {}
            if isinstance(auth, dict):
                aid = auth.get("chatgpt_account_id")
                if isinstance(aid, str) and aid:
                    return aid
    return None


def extract_identity(blob: dict) -> dict[str, Any]:
    """Extract email / account / plan from id_token or access_token JWT claims."""
    tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else blob
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    access = access_token_of(blob)

    email: str | None = None
    plan: str | None = None
    user_id: str | None = None
    account_id = account_id_of(blob)

    for jwt in (id_token, access):
        if not isinstance(jwt, str) or not jwt:
            continue
        payload = _decode_jwt_payload(jwt)
        if not payload:
            continue
        if not email:
            email = payload.get("email") if isinstance(payload.get("email"), str) else None
            profile = payload.get("https://api.openai.com/profile")
            if not email and isinstance(profile, dict):
                email = profile.get("email") if isinstance(profile.get("email"), str) else None
        auth = payload.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            if not plan and isinstance(auth.get("chatgpt_plan_type"), str):
                plan = auth["chatgpt_plan_type"]
            if not user_id:
                uid = auth.get("chatgpt_user_id") or auth.get("user_id")
                if isinstance(uid, str) and uid:
                    user_id = uid
            if not account_id and isinstance(auth.get("chatgpt_account_id"), str):
                account_id = auth["chatgpt_account_id"]

    return {
        "email": email,
        "user_id": user_id,
        "account_id": account_id,
        "tier": plan,
    }


def expiry_ts(blob: dict) -> float:
    """Unix seconds when the access token expires, or 0 if unknown."""
    access = access_token_of(blob)
    if not access:
        return 0.0
    payload = _decode_jwt_payload(access)
    if payload and isinstance(payload.get("exp"), (int, float)):
        return float(payload["exp"])
    return 0.0


def is_token_expired(blob: dict) -> bool:
    """True if access token is missing or within the refresh buffer of expiry."""
    access = access_token_of(blob)
    if not access:
        return True
    exp = expiry_ts(blob)
    if exp <= 0:
        # No exp claim — fall back to last_refresh age (Codex uses ~8 day cadence,
        # but we treat unknown as stale to force a refresh attempt).
        last = blob.get("last_refresh")
        if isinstance(last, str) and last:
            try:
                s = last.replace("Z", "+00:00")
                last_ts = datetime.fromisoformat(s).timestamp()
                # If last refresh was > 1 day ago and no exp, try refresh.
                return datetime.now(timezone.utc).timestamp() - last_ts > 86400
            except ValueError:
                return True
        return True
    return datetime.now(timezone.utc).timestamp() + EXPIRY_BUFFER_S >= exp


def refresh_token(blob: dict, timeout: float = 15.0) -> RefreshResult:
    """Refresh ChatGPT OAuth tokens. Returns updated full auth.json blob.

    On success the response may include a new refresh_token (rotation). The
    caller MUST persist the result to both tracker store and ~/.codex/auth.json.
    """
    rt = refresh_token_of(blob)
    if not rt:
        return RefreshResult(None, "no_refresh_token")

    body = json.dumps({
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": rt,
    }).encode()

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
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
        code = _extract_error_code(body_text)
        if e.code in (400, 401, 403) or code in (
            "refresh_token_expired",
            "refresh_token_reused",
            "refresh_token_invalidated",
            "invalid_grant",
        ):
            return RefreshResult(None, "invalid_grant")
        logger.debug("Codex refresh failed: %r, body: %s", e, body_text[:500])
        return RefreshResult(None, "transient")
    except Exception as e:
        logger.debug("Codex refresh failed: %r", e)
        return RefreshResult(None, "transient")

    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return RefreshResult(None, "transient")

    updated = dict(blob)
    tokens = dict(updated.get("tokens") or {})
    tokens["access_token"] = access
    if isinstance(data.get("id_token"), str) and data["id_token"]:
        tokens["id_token"] = data["id_token"]
    if isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
        tokens["refresh_token"] = data["refresh_token"]
    # Preserve account_id if the response does not re-emit it
    if not tokens.get("account_id"):
        aid = account_id_of(blob)
        if aid:
            tokens["account_id"] = aid
    updated["tokens"] = tokens
    updated["auth_mode"] = updated.get("auth_mode") or "chatgpt"
    updated["last_refresh"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return RefreshResult(updated, None)


def _extract_error_code(body: str) -> str | None:
    if not body or not body.strip():
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        return err["code"]
    if isinstance(err, str):
        return err
    if isinstance(data.get("code"), str):
        return data["code"]
    return None


@dataclass
class UsageResult:
    usage: dict | None
    error: str | None
    retry_after: float | None


def fetch_usage(
    access_token: str,
    account_id: str | None = None,
    timeout: float = 10.0,
) -> UsageResult:
    """Fetch and normalize Codex rate-limit windows from WHAM usage API."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    req = urllib.request.Request(USAGE_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        retry_after = None
        raw = e.headers.get("Retry-After") if e.headers else None
        if raw:
            try:
                retry_after = max(0.0, float(raw.strip()))
            except ValueError:
                pass
        if e.code == 401:
            return UsageResult(None, "token-expired", retry_after)
        if e.code == 429:
            return UsageResult(None, "http-429", retry_after)
        return UsageResult(None, f"http-{e.code}", retry_after)
    except TimeoutError:
        return UsageResult(None, "timeout", None)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return UsageResult(None, "timeout", None)
        return UsageResult(None, "network", None)
    except Exception as e:
        logger.debug("Codex usage fetch failed: %r", e)
        return UsageResult(None, type(e).__name__, None)

    return UsageResult(_build_usage(data), None, None)


def _window_entry(win: dict | None) -> dict[str, Any] | None:
    if not isinstance(win, dict):
        return None
    pct = win.get("used_percent")
    if not isinstance(pct, (int, float)):
        return None
    entry: dict[str, Any] = {"pct": float(pct)}
    reset_at = win.get("reset_at")
    if isinstance(reset_at, (int, float)) and reset_at > 0:
        # WHAM returns unix seconds
        entry["resets_at"] = datetime.fromtimestamp(
            float(reset_at), tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    elif isinstance(win.get("reset_after_seconds"), (int, float)):
        reset_s = float(win["reset_after_seconds"])
        if reset_s > 0:
            ts = datetime.now(timezone.utc).timestamp() + reset_s
            entry["resets_at"] = datetime.fromtimestamp(
                ts, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
    window_s = win.get("limit_window_seconds")
    if isinstance(window_s, (int, float)) and window_s > 0:
        entry["window_minutes"] = int(window_s) // 60
    return entry


def _build_usage(data: dict) -> dict:
    """Normalize WHAM usage payload into tracker windows dict."""
    result: dict[str, Any] = {}

    plan = data.get("plan_type")
    if isinstance(plan, str) and plan:
        result["plan_type"] = plan

    rate = data.get("rate_limit")
    # OpenAPI double-option may nest nulls; accept dict only
    if isinstance(rate, dict):
        primary = _window_entry(rate.get("primary_window"))
        if primary:
            # Label by window length when known (5h / weekly / monthly)
            mins = primary.get("window_minutes")
            if mins and mins <= 60 * 6:
                result["primary"] = primary
                result["primary"]["name"] = "5h" if mins <= 360 else f"{mins}m"
            else:
                result["primary"] = primary
        secondary = _window_entry(rate.get("secondary_window"))
        if secondary:
            result["secondary"] = secondary
        if rate.get("limit_reached"):
            result["limit_reached"] = True
        if "allowed" in rate:
            result["allowed"] = bool(rate["allowed"])

    credits = data.get("credits")
    if isinstance(credits, dict):
        result["credits"] = {
            "has_credits": bool(credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        }

    reached = data.get("rate_limit_reached_type")
    if isinstance(reached, dict) and reached.get("type"):
        result["reached_type"] = reached["type"]
    elif isinstance(reached, str):
        result["reached_type"] = reached

    # Additional named limits (e.g. codex_other)
    extra = data.get("additional_rate_limits")
    if isinstance(extra, list):
        scoped: list[dict[str, Any]] = []
        for item in extra:
            if not isinstance(item, dict):
                continue
            name = item.get("limit_name") or item.get("limit_id") or "extra"
            win = item.get("primary_window") or item.get("rate_limit")
            if isinstance(win, dict) and "used_percent" not in win:
                # nested rate_limit shape
                win = (win.get("primary_window") if isinstance(win.get("primary_window"), dict)
                       else win)
            entry = _window_entry(win if isinstance(win, dict) else None)
            if entry:
                entry["name"] = str(name)[:12]
                scoped.append(entry)
        if scoped:
            result["scoped"] = scoped

    return result if result else {}
