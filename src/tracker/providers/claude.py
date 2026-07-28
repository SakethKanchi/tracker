"""Claude OAuth: token refresh, profile resolution, usage API fetch.

Mirrors the HTTP mechanics validated by claude-swap (cswap):
  - Refresh: POST https://platform.claude.com/v1/oauth/token (grant_type=refresh_token)
  - Profile: GET https://api.anthropic.com/api/oauth/profile
  - Usage:   GET https://api.anthropic.com/api/oauth/usage (anthropic-beta: oauth-2025-04-20)
All calls use User-Agent: claude-swap/1.0 to match the same fingerprint that
works against the usage endpoint's per-token budget.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("tracker")

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USER_AGENT = "claude-swap/1.0"
EXPIRY_BUFFER_MS = 5 * 60 * 1000  # refresh if < 5 min left


@dataclass
class RefreshResult:
    credentials: dict | None  # updated claudeAiOauth blob, or None on failure
    error: str | None         # "invalid_grant" | "transient" | None
    identity: dict | None     # account info from token-endpoint response


def is_token_expired(oauth: dict) -> bool:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return True
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms + EXPIRY_BUFFER_MS >= int(expires_at)


def refresh_token(oauth: dict, timeout: float = 10.0) -> RefreshResult:
    """Refresh an OAuth access token. Returns updated blob or None."""
    refresh_tok = oauth.get("refreshToken")
    if not refresh_tok:
        return RefreshResult(None, "no_refresh_token", None)

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        if e.code in (400, 401, 403) and (
            "invalid_grant" in body_text or "invalid_client" in body_text
        ):
            return RefreshResult(None, "invalid_grant", None)
        logger.debug("Claude refresh failed: %r, body: %s", e, body_text[:500])
        return RefreshResult(None, "transient", None)
    except Exception as e:
        logger.debug("Claude refresh failed: %r", e)
        return RefreshResult(None, "transient", None)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    oauth["accessToken"] = resp_data["access_token"]
    oauth["expiresAt"] = now_ms + resp_data["expires_in"] * 1000
    if resp_data.get("refresh_token"):
        oauth["refreshToken"] = resp_data["refresh_token"]
    if resp_data.get("scope"):
        oauth["scopes"] = resp_data["scope"].split()

    # Opportunistic identity from token response
    identity = None
    account = resp_data.get("account")
    if isinstance(account, dict) and isinstance(account.get("uuid"), str):
        org = resp_data.get("organization")
        identity = {
            "uuid": account["uuid"].strip(),
            "email": account.get("email_address") if isinstance(account.get("email_address"), str) else None,
            "organizationUuid": org.get("uuid") if isinstance(org, dict) else None,
        }
    return RefreshResult(oauth, None, identity)


def fetch_profile(access_token: str, timeout: float = 5.0) -> dict | None:
    """Resolve an access token to {uuid, email, organizationUuid} or None."""
    req = urllib.request.Request(
        PROFILE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("Claude profile fetch failed: %r", e)
        return None
    account = data.get("account") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    uuid = account.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        return None
    org = data.get("organization")
    return {
        "uuid": uuid.strip(),
        "email": account.get("email") if isinstance(account.get("email"), str) else None,
        "organizationUuid": org.get("uuid") if isinstance(org, dict) else None,
    }


def _request_usage(access_token: str, timeout: float = 5.0) -> dict:
    """Raw usage API call. Raises on HTTP error."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _classify_error(e: Exception) -> tuple[str, float | None]:
    """Return (error_kind, retry_after_seconds) from an HTTP exception."""
    if isinstance(e, urllib.error.HTTPError):
        retry_after = None
        raw = e.headers.get("Retry-After") if e.headers else None
        if raw:
            try:
                retry_after = max(0.0, float(raw.strip()))
            except ValueError:
                pass
        return f"http-{e.code}", retry_after
    if isinstance(e, TimeoutError):
        return "timeout", None
    if isinstance(e, urllib.error.URLError):
        if isinstance(e.reason, TimeoutError):
            return "timeout", None
        return "network", None
    if isinstance(e, json.JSONDecodeError):
        return "bad-response", None
    return type(e).__name__, None


@dataclass
class UsageResult:
    usage: dict | None       # normalized windows, or None on failure
    error: str | None        # error kind, or None
    retry_after: float | None


def fetch_usage(access_token: str, timeout: float = 5.0) -> UsageResult:
    """Fetch and normalize 5h/7d usage windows. Never raises."""
    try:
        data = _request_usage(access_token, timeout=timeout)
    except Exception as e:
        kind, retry_after = _classify_error(e)
        return UsageResult(None, kind, retry_after)
    return UsageResult(_build_usage(data), None, None)


def _build_usage(data: dict) -> dict:
    """Normalize raw API data into {five_hour, seven_day, scoped, spend}."""
    result: dict[str, Any] = {}

    h5 = data.get("five_hour")
    if isinstance(h5, dict):
        entry: dict[str, Any] = {"pct": h5.get("utilization")}
        if h5.get("resets_at"):
            entry["resets_at"] = h5["resets_at"]
        result["five_hour"] = entry

    d7 = data.get("seven_day")
    if isinstance(d7, dict):
        entry = {"pct": d7.get("utilization")}
        if d7.get("resets_at"):
            entry["resets_at"] = d7["resets_at"]
        result["seven_day"] = entry

    # Per-model weekly scoped windows
    limits = data.get("limits")
    if isinstance(limits, list):
        scoped: list[dict[str, Any]] = []
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            scope = lim.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            pct = lim.get("percent")
            if not name or not isinstance(pct, (int, float)):
                continue
            e: dict[str, Any] = {"name": name, "pct": float(pct)}
            if lim.get("resets_at"):
                e["resets_at"] = lim["resets_at"]
            scoped.append(e)
        if scoped:
            result["scoped"] = scoped

    # Pay-as-you-go spend
    eu = data.get("extra_usage")
    if isinstance(eu, dict) and eu.get("is_enabled"):
        used = eu.get("used_credits")
        limit_ = eu.get("monthly_limit")
        util = eu.get("utilization")
        if all(isinstance(x, (int, float)) for x in (used, limit_, util)):
            result["spend"] = {
                "used": float(used) / 100,
                "limit": float(limit_) / 100,
                "pct": float(util),
                "currency": eu.get("currency", "USD"),
            }
            if eu.get("resets_at"):
                result["spend"]["resets_at"] = eu["resets_at"]

    return result if result else {}