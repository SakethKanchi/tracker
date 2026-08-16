"""Usage collection: per-account dispatch with refresh-if-stale + 429 backoff.

The primary entry point is collect_all() — called by `tracker list`. For each
account it decides whether a network/parse refresh is eligible this run:
  1. backoff_until in the future → serve last_good (no network call)
  2. newest sample younger than serve_ttl → serve as-is (no network call)
  3. else run the provider's collector

On http-429, honor Retry-After, write fetch_state, keep last_good until reset.
Every account always gets a row, regardless of which branch it took.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from . import credentials, store
from .providers import apikeys, claude, codex, gemini, grok

logger = logging.getLogger("tracker")

SERVE_TTL_S = 300.0          # serve cached data if newer than 5 min
BACKOFF_FLOOR_S = 300.0      # minimum backoff after a 429
BACKOFF_CAP_S = 3600.0       # never back off longer than 1 hour
TRANSIENT_FLOOR_S = 60.0     # minimum backoff on transient errors
TRANSIENT_CAP_S = 1800.0     # cap transient backoff at 30 min


@dataclass
class AccountUsage:
    """One row in the `tracker list` output."""
    account_id: str
    provider: str
    label: str
    email: str | None
    tier: str | None
    windows: dict[str, Any] | None
    source: str           # 'api' | 'manual' | 'derived' | 'cached' | 'backing-off' | 'error' | 'no-data'
    fetched_at: float | None
    error: str | None     # human-readable error if collection failed
    needs_relogin: bool   # refresh token is dead


def _collect_claude(account: Any, conn: Any, force: bool = False) -> AccountUsage:
    """Refresh-if-stale + fetch Claude usage, honoring 429 backoff."""
    acct_id = account["id"]
    cred_blob = credentials.read_credential(acct_id)
    if not cred_blob:
        return _no_data(account, "credential missing — re-add account")

    state = store.get_fetch_state(conn, acct_id)

    # Check backoff
    if not force and state and state["backoff_until"] and state["backoff_until"] > time.time():
        return _serve_cached(account, conn, state, backing_off=True)

    # Check staleness
    if not force:
        latest = store.latest_usage_sample(conn, acct_id)
        if latest and (time.time() - latest["fetched_at"]) < SERVE_TTL_S:
            return _serve_cached(account, conn, state, backing_off=False)

    store.upsert_fetch_state(conn, account_id=acct_id, last_attempt_at=time.time())

    # Refresh if needed
    needs_relogin = False
    if claude.is_token_expired(cred_blob):
        result = claude.refresh_token(cred_blob)
        if result.error == "invalid_grant":
            store.upsert_fetch_state(
                conn, account_id=acct_id,
                last_error="invalid_grant",
                backoff_until=time.time() + BACKOFF_CAP_S,
            )
            return AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"], None, "error", None,
                "refresh token dead — re-login with claude, then tracker add claude",
                needs_relogin=True,
            )
        if result.credentials:
            credentials.write_credential(acct_id, result.credentials)
            cred_blob = result.credentials
        else:
            # Transient refresh failure — try with existing token anyway
            logger.debug("Claude refresh transient failure for %s", acct_id)

    access_token = cred_blob.get("accessToken")
    if not access_token:
        return _no_data(account, "no access token in credential")

    result = claude.fetch_usage(access_token)

    if result.error:
        error_kind = result.error
        retry_after = result.retry_after

        store.insert_rate_limit_event(
            conn, account_id=acct_id, kind=error_kind,
            message=None, retry_after=retry_after,
        )

        # Compute backoff
        if error_kind == "http-429":
            backoff = max(retry_after or 0, BACKOFF_FLOOR_S)
            backoff = min(backoff, BACKOFF_CAP_S)
        else:
            failures = (state["consecutive_failures"] + 1) if state else 1
            backoff = min(TRANSIENT_FLOOR_S * failures, TRANSIENT_CAP_S)

        store.upsert_fetch_state(
            conn, account_id=acct_id,
            consecutive_failures=(state["consecutive_failures"] + 1) if state else 1,
            backoff_until=time.time() + backoff,
            last_error=error_kind,
        )

        # Serve last_good if we have it
        latest = store.latest_usage_sample(conn, acct_id)
        if latest:
            return AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"],
                json.loads(latest["windows"]), "backing-off",
                latest["fetched_at"], f"{error_kind} (serving cached)", False,
            )
        return AccountUsage(
            account["id"], account["provider"], account["label"],
            account["email"], account["tier"], None, "error", None,
            f"fetch failed: {error_kind}", False,
        )

    # Success
    store.insert_usage_sample(conn, account_id=acct_id, source="api", windows=result.usage)
    store.upsert_fetch_state(
        conn, account_id=acct_id,
        consecutive_failures=0, backoff_until=None, last_error=None,
    )
    return AccountUsage(
        account["id"], account["provider"], account["label"],
        account["email"], account["tier"], result.usage,
        "api", time.time(), None, False,
    )


def _persist_grok_blob(account_id: str, blob: dict[str, Any]) -> dict[str, Any]:
    """Save a Grok credential to the tracker store and, when possible, auth.json.

    xAI rotates ``refresh_token`` on every refresh grant. If we keep the new
    tokens only in the tracker store, the Grok CLI is left with a dead refresh
    token and forces a browser re-login. Always write both directions for the
    matching user_id.
    """
    credentials.write_credential(account_id, blob)
    credentials.write_back_grok_auth(blob)
    return blob


def _adopt_live_grok(
    account_id: str, user_id: str | None, cred_blob: dict[str, Any]
) -> dict[str, Any]:
    """Bidirectional sync with ~/.grok/auth.json for the same user_id.

    - Live fresher → adopt into tracker store (CLI already refreshed).
    - Store fresher → push into auth.json so the CLI does not keep a rotated-
      away refresh token.
    """
    if not user_id:
        return cred_blob
    live = credentials.import_grok_credential_for_user(user_id)
    if not live:
        return cred_blob

    live_exp = grok.expiry_ts(live)
    store_exp = grok.expiry_ts(cred_blob)
    live_rt = live.get("refresh_token") or live.get("refreshToken")
    store_rt = cred_blob.get("refresh_token") or cred_blob.get("refreshToken")

    if live_exp > store_exp or (
        live_exp == store_exp and live_rt and live_rt != store_rt
    ):
        credentials.write_credential(account_id, live)
        return live

    # Tracker is ahead (or equal with same RT). If the CLI is logged in as this
    # user with a *staler* access token / different RT, push our copy so a
    # later CLI refresh does not race on a rotated-away grant.
    if store_exp > live_exp or (store_rt and live_rt and store_rt != live_rt):
        credentials.write_back_grok_auth(cred_blob)
    return cred_blob


def _refresh_grok_blob(
    account_id: str, cred_blob: dict[str, Any], user_id: str | None
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Refresh a Grok OIDC token. Returns (blob, error_message, needs_relogin)."""
    result = grok.refresh_token(cred_blob)
    if result.credentials:
        return _persist_grok_blob(account_id, result.credentials), None, False

    if result.error in ("invalid_grant", "no_refresh_token"):
        # CLI may have rotated the grant after we last read the file — one more
        # try against a re-read of live auth.json for the same user.
        live = credentials.import_grok_credential_for_user(user_id) if user_id else None
        live_rt = (live or {}).get("refresh_token") or (live or {}).get("refreshToken")
        store_rt = cred_blob.get("refresh_token") or cred_blob.get("refreshToken")
        if live and live_rt and live_rt != store_rt:
            retry = grok.refresh_token(live)
            if retry.credentials:
                return _persist_grok_blob(account_id, retry.credentials), None, False
        return None, (
            "refresh token dead — re-login with `grok login --oauth`, "
            "then `tracker add grok`"
        ), True

    # Transient refresh failure — fall through with whatever we have.
    logger.debug("Grok refresh transient failure for %s: %s", account_id, result.error)
    return cred_blob, None, False


def _ensure_grok_credentials(
    account: Any,
    cred_blob: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Adopt live auth.json, refresh if expired (or force_refresh).

    Returns (blob, error_message, needs_relogin).
    """
    acct_id = account["id"]
    user_id = cred_blob.get("user_id") or account["provider_account_id"]

    cred_blob = _adopt_live_grok(acct_id, user_id, cred_blob)

    if not force_refresh and not grok.is_token_expired(cred_blob):
        return cred_blob, None, False

    return _refresh_grok_blob(acct_id, cred_blob, user_id)


def _apply_grok_billing(windows: dict[str, Any], access_token: str) -> None:
    """Mutate ``windows`` with billing + quota fields from live Grok APIs."""
    billing = grok.fetch_credit_usage(access_token)
    if billing:
        if billing.get("credit_usage_pct") is not None:
            windows["credit_usage_pct"] = billing["credit_usage_pct"]
            windows["billing_period_end"] = billing["period_end"]
            if billing.get("product_usage"):
                windows["product_usage"] = billing["product_usage"]
        if billing.get("monthly_pct") is not None:
            windows["monthly_pct"] = billing["monthly_pct"]
            windows["monthly_used"] = billing["monthly_used"]
            windows["monthly_limit"] = billing["monthly_limit"]
            windows["monthly_period_end"] = billing["monthly_period_end"]

    # If usage is pinned at 100 OR billing failed, ask v1/models for the
    # structured blocked reason — or a real auth failure.
    pct = windows.get("credit_usage_pct")
    if pct is None or pct >= 100:
        quota = grok.check_live_quota(access_token)
        windows["quota_status"] = quota["status"]
        if quota["status"] == "blocked":
            windows["quota_reason"] = quota.get("reason", "")
            windows["quota_message"] = quota.get("message", "")
        elif quota["status"] == "error":
            windows["quota_reason"] = quota.get("reason", "")
            windows["quota_message"] = quota.get("message", "")
    else:
        windows["quota_status"] = "active"


def _collect_grok(account: Any, conn: Any, force: bool = False) -> AccountUsage:
    """Parse local session transcripts + check live quota via api.x.ai."""
    acct_id = account["id"]

    # For `tracker list` without --refresh, serve cached derived sample if fresh
    if not force:
        latest = store.latest_usage_sample(conn, acct_id)
        if latest and (time.time() - latest["fetched_at"]) < SERVE_TTL_S:
            return AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"],
                json.loads(latest["windows"]), "cached",
                latest["fetched_at"], None, False,
            )

    # Parse transcripts and insert new token_usage rows
    rows = grok.parse_all_sessions(acct_id)
    if rows:
        store.insert_token_usage(conn, rows)

    # Derive summary from all token_usage
    windows = grok.derive_usage_summary(conn, acct_id)

    # Live signal: billing endpoint gives the weekly credit usage % (the same
    # bar grok.com shows). Falls back to v1/models for a blocked reason when
    # billing is unreachable.
    cred_blob = credentials.read_credential(acct_id)
    if not cred_blob:
        return _no_data(account, "credential missing — re-add account")

    cred_blob, auth_err, needs_relogin = _ensure_grok_credentials(account, cred_blob)
    if auth_err:
        store.upsert_fetch_state(
            conn, account_id=acct_id,
            last_error="invalid_grant",
            backoff_until=time.time() + BACKOFF_CAP_S,
        )
        return AccountUsage(
            account["id"], account["provider"], account["label"],
            account["email"], account["tier"], windows, "error",
            time.time(), auth_err, needs_relogin,
        )

    access_token = grok.access_token_of(cred_blob) if cred_blob else None
    if access_token:
        _apply_grok_billing(windows, access_token)

        # Access token rejected despite a non-expired clock → force-refresh once
        # and retry. Without this, a rotated-away grant surfaces as constant
        # "re-login" even when a valid refresh_token is still available.
        if windows.get("quota_status") == "error" and windows.get("quota_reason") == "token-expired":
            refreshed, refresh_err, needs_relogin = _ensure_grok_credentials(
                account, cred_blob, force_refresh=True,
            )
            if refresh_err:
                store.upsert_fetch_state(
                    conn, account_id=acct_id,
                    last_error="invalid_grant",
                    backoff_until=time.time() + BACKOFF_CAP_S,
                )
                return AccountUsage(
                    account["id"], account["provider"], account["label"],
                    account["email"], account["tier"], windows, "error",
                    time.time(), refresh_err, needs_relogin,
                )
            cred_blob = refreshed
            access_token = grok.access_token_of(cred_blob) if cred_blob else None
            if access_token:
                # Clear stale auth error fields before retry.
                for k in ("quota_status", "quota_reason", "quota_message",
                          "credit_usage_pct", "billing_period_end", "product_usage",
                          "monthly_pct", "monthly_used", "monthly_limit",
                          "monthly_period_end"):
                    windows.pop(k, None)
                _apply_grok_billing(windows, access_token)

        if windows.get("quota_status") == "blocked":
            store.insert_rate_limit_event(
                conn, account_id=acct_id,
                kind=windows.get("quota_reason") or "blocked",
                message=windows.get("quota_message"),
            )
        elif (
            windows.get("quota_status") == "error"
            and windows.get("quota_reason") == "token-expired"
        ):
            needs_relogin = True

    store.insert_usage_sample(conn, account_id=acct_id, source="derived", windows=windows)

    # Auth failures are already surfaced via windows["quota_status"] == "error".
    # Only attach a top-level error when we never got a structured quota status
    # (e.g. missing access token after a soft refresh failure).
    err = None
    if needs_relogin and windows.get("quota_status") != "error":
        err = (
            "access token invalid — run grok login --oauth, then tracker add grok"
        )
    elif needs_relogin and windows.get("quota_reason") == "token-expired":
        err = windows.get("quota_message") or (
            "access token invalid — run grok login --oauth, then tracker add grok"
        )

    # Clear prior invalid_grant backoff once a collection succeeds without
    # needing a re-login.
    if not needs_relogin:
        store.upsert_fetch_state(
            conn, account_id=acct_id,
            consecutive_failures=0, backoff_until=None, last_error=None,
        )

    return AccountUsage(
        account["id"], account["provider"], account["label"],
        account["email"], account["tier"], windows,
        "derived", time.time(), err, needs_relogin,
    )


_PROVIDER_ORDER = ("claude", "grok", "codex", "gemini", "openai")


def _collect_for_account(
    account: Any, conn: Any, force: bool = False
) -> AccountUsage:
    """Dispatch to the right collector based on provider + credential type."""
    provider = account["provider"]
    cred_blob = credentials.read_credential(account["id"])
    if cred_blob and apikeys.is_api_key_blob(cred_blob):
        return _collect_api_key(account, conn, cred_blob, force=force)

    if provider == "claude":
        return _collect_claude(account, conn, force=force)
    if provider == "grok":
        return _collect_grok(account, conn, force=force)
    if provider == "codex":
        return _collect_codex(account, conn, force=force)
    if provider in ("gemini", "openai"):
        # These only support API-key mode; missing blob already handled above.
        return _no_data(account, "credential missing — re-add with tracker add <api_key>")
    return AccountUsage(
        account["id"], provider, account["label"],
        account["email"], account["tier"], None,
        "error", None, f"unknown provider {provider}", False,
    )


def _collect_api_key(
    account: Any, conn: Any, cred_blob: dict[str, Any], force: bool = False
) -> AccountUsage:
    """Health-check an API-key account (claude/grok/gemini/openai)."""
    acct_id = account["id"]
    provider = account["provider"]
    api_key = apikeys.api_key_of(cred_blob)
    if not api_key:
        return _no_data(account, "api_key missing from credential")

    state = store.get_fetch_state(conn, acct_id)
    if not force and state and state["backoff_until"] and state["backoff_until"] > time.time():
        return _serve_cached(account, conn, state, backing_off=True)
    if not force:
        latest = store.latest_usage_sample(conn, acct_id)
        if latest and (time.time() - latest["fetched_at"]) < SERVE_TTL_S:
            return _serve_cached(account, conn, state, backing_off=False)

    store.upsert_fetch_state(conn, account_id=acct_id, last_attempt_at=time.time())

    if provider == "gemini":
        result = gemini.fetch_usage(api_key)
        windows, err = result.usage, result.error
        retry_after = result.retry_after
    else:
        ok, err, _identity, windows = apikeys._validate(provider, api_key, timeout=10.0)
        if ok:
            err = None
        retry_after = None

    if err and not windows:
        if err in ("token-expired",) or (err and "invalid" in err):
            store.upsert_fetch_state(
                conn, account_id=acct_id,
                last_error=err,
                consecutive_failures=(state["consecutive_failures"] + 1) if state else 1,
                backoff_until=time.time() + BACKOFF_CAP_S,
            )
            return AccountUsage(
                account["id"], provider, account["label"],
                account["email"], account["tier"], None, "error", None,
                f"{err} — replace with: tracker add <new_api_key>",
                needs_relogin=True,
            )
        failures = (state["consecutive_failures"] + 1) if state else 1
        backoff = min(TRANSIENT_FLOOR_S * failures, TRANSIENT_CAP_S)
        if err == "http-429":
            backoff = min(max(retry_after or 0, BACKOFF_FLOOR_S), BACKOFF_CAP_S)
        store.upsert_fetch_state(
            conn, account_id=acct_id,
            consecutive_failures=failures,
            backoff_until=time.time() + backoff,
            last_error=err,
        )
        latest = store.latest_usage_sample(conn, acct_id)
        if latest:
            return AccountUsage(
                account["id"], provider, account["label"],
                account["email"], account["tier"],
                json.loads(latest["windows"]), "backing-off",
                latest["fetched_at"], f"{err} (serving cached)", False,
            )
        return AccountUsage(
            account["id"], provider, account["label"],
            account["email"], account["tier"], None, "error", None,
            f"fetch failed: {err}", False,
        )

    store.insert_usage_sample(conn, account_id=acct_id, source="api", windows=windows or {})
    store.upsert_fetch_state(
        conn, account_id=acct_id,
        consecutive_failures=0, backoff_until=None, last_error=None,
    )
    return AccountUsage(
        account["id"], provider, account["label"],
        account["email"], account["tier"], windows or {},
        "api", time.time(), None, False,
    )


def _persist_codex_blob(account_id: str, blob: dict[str, Any]) -> dict[str, Any]:
    """Save Codex tokens to tracker store and write-back to ~/.codex/auth.json.

    Refresh tokens are single-use — both sides must stay in sync.
    """
    credentials.write_credential(account_id, blob)
    credentials.write_back_codex_auth(blob)
    return blob


def _adopt_live_codex(
    account_id: str, account_key: str | None, cred_blob: dict[str, Any]
) -> dict[str, Any]:
    """Bidirectional sync with ~/.codex/auth.json for the same account."""
    live = credentials.import_codex_credential()
    if not live or live.get("auth_type") == "api_key":
        return cred_blob

    live_tokens = live.get("tokens") if isinstance(live.get("tokens"), dict) else {}
    store_tokens = cred_blob.get("tokens") if isinstance(cred_blob.get("tokens"), dict) else {}
    live_aid = live_tokens.get("account_id") if isinstance(live_tokens, dict) else None
    store_aid = store_tokens.get("account_id") if isinstance(store_tokens, dict) else None

    # Only adopt when same account (or account_id unknown on one side)
    if account_key and live_aid and live_aid != account_key:
        return cred_blob
    if store_aid and live_aid and store_aid != live_aid:
        return cred_blob

    live_exp = codex.expiry_ts(live)
    store_exp = codex.expiry_ts(cred_blob)
    live_rt = codex.refresh_token_of(live)
    store_rt = codex.refresh_token_of(cred_blob)

    if live_exp > store_exp or (
        live_exp == store_exp and live_rt and live_rt != store_rt
    ):
        credentials.write_credential(account_id, live)
        return live

    if store_exp > live_exp or (store_rt and live_rt and store_rt != live_rt):
        credentials.write_back_codex_auth(cred_blob)
    return cred_blob


def _refresh_codex_blob(
    account_id: str, cred_blob: dict[str, Any], account_key: str | None
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Refresh Codex OAuth. Returns (blob, error_message, needs_relogin)."""
    result = codex.refresh_token(cred_blob)
    if result.credentials:
        return _persist_codex_blob(account_id, result.credentials), None, False

    if result.error in ("invalid_grant", "no_refresh_token"):
        # CLI may have rotated after our last read — retry once against live file
        live = credentials.import_codex_credential()
        if live and live.get("auth_type") != "api_key":
            live_aid = codex.account_id_of(live)
            if not account_key or not live_aid or live_aid == account_key:
                live_rt = codex.refresh_token_of(live)
                store_rt = codex.refresh_token_of(cred_blob)
                if live_rt and live_rt != store_rt:
                    retry = codex.refresh_token(live)
                    if retry.credentials:
                        return _persist_codex_blob(account_id, retry.credentials), None, False
        return None, (
            "refresh token dead — re-login with `codex`, then `tracker add codex`"
        ), True

    logger.debug("Codex refresh transient failure for %s: %s", account_id, result.error)
    return cred_blob, None, False


def _collect_codex(account: Any, conn: Any, force: bool = False) -> AccountUsage:
    """Refresh-if-stale + fetch Codex WHAM usage windows."""
    acct_id = account["id"]
    cred_blob = credentials.read_credential(acct_id)
    if not cred_blob:
        return _no_data(account, "credential missing — re-add account")

    # API-key-only import from codex auth.json
    if apikeys.is_api_key_blob(cred_blob):
        return _collect_api_key(account, conn, cred_blob, force=force)

    state = store.get_fetch_state(conn, acct_id)
    if not force and state and state["backoff_until"] and state["backoff_until"] > time.time():
        return _serve_cached(account, conn, state, backing_off=True)
    if not force:
        latest = store.latest_usage_sample(conn, acct_id)
        if latest and (time.time() - latest["fetched_at"]) < SERVE_TTL_S:
            return _serve_cached(account, conn, state, backing_off=False)

    store.upsert_fetch_state(conn, account_id=acct_id, last_attempt_at=time.time())

    account_key = (
        account["provider_account_id"]
        or codex.account_id_of(cred_blob)
    )
    cred_blob = _adopt_live_codex(acct_id, account_key, cred_blob)

    needs_relogin = False
    if codex.is_token_expired(cred_blob):
        cred_blob, auth_err, needs_relogin = _refresh_codex_blob(
            acct_id, cred_blob, account_key
        )
        if auth_err:
            store.upsert_fetch_state(
                conn, account_id=acct_id,
                last_error="invalid_grant",
                backoff_until=time.time() + BACKOFF_CAP_S,
            )
            return AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"], None, "error", None,
                auth_err, needs_relogin=True,
            )

    access_token = codex.access_token_of(cred_blob) if cred_blob else None
    if not access_token:
        return _no_data(account, "no access token in credential")

    account_id_hdr = codex.account_id_of(cred_blob)
    result = codex.fetch_usage(access_token, account_id=account_id_hdr)

    # Access rejected → force-refresh once and retry
    if result.error == "token-expired" and cred_blob:
        refreshed, refresh_err, needs_relogin = _refresh_codex_blob(
            acct_id, cred_blob, account_key
        )
        if refresh_err:
            store.upsert_fetch_state(
                conn, account_id=acct_id,
                last_error="invalid_grant",
                backoff_until=time.time() + BACKOFF_CAP_S,
            )
            return AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"], None, "error", None,
                refresh_err, needs_relogin=True,
            )
        cred_blob = refreshed
        access_token = codex.access_token_of(cred_blob) if cred_blob else None
        if access_token:
            result = codex.fetch_usage(
                access_token, account_id=codex.account_id_of(cred_blob)
            )

    if result.error:
        error_kind = result.error
        retry_after = result.retry_after
        store.insert_rate_limit_event(
            conn, account_id=acct_id, kind=error_kind,
            message=None, retry_after=retry_after,
        )
        if error_kind == "http-429":
            backoff = min(max(retry_after or 0, BACKOFF_FLOOR_S), BACKOFF_CAP_S)
        else:
            failures = (state["consecutive_failures"] + 1) if state else 1
            backoff = min(TRANSIENT_FLOOR_S * failures, TRANSIENT_CAP_S)
        store.upsert_fetch_state(
            conn, account_id=acct_id,
            consecutive_failures=(state["consecutive_failures"] + 1) if state else 1,
            backoff_until=time.time() + backoff,
            last_error=error_kind,
        )
        latest = store.latest_usage_sample(conn, acct_id)
        if latest:
            return AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"],
                json.loads(latest["windows"]), "backing-off",
                latest["fetched_at"], f"{error_kind} (serving cached)", False,
            )
        return AccountUsage(
            account["id"], account["provider"], account["label"],
            account["email"], account["tier"], None, "error", None,
            f"fetch failed: {error_kind}", needs_relogin,
        )

    # Opportunistically update tier from plan_type
    if result.usage and result.usage.get("plan_type"):
        plan = result.usage["plan_type"]
        if plan and plan != account["tier"]:
            conn.execute(
                "UPDATE accounts SET tier=? WHERE id=?", (plan, acct_id)
            )

    store.insert_usage_sample(conn, account_id=acct_id, source="api", windows=result.usage)
    store.upsert_fetch_state(
        conn, account_id=acct_id,
        consecutive_failures=0, backoff_until=None, last_error=None,
    )
    return AccountUsage(
        account["id"], account["provider"], account["label"],
        account["email"], account["tier"] or (result.usage or {}).get("plan_type"),
        result.usage, "api", time.time(), None, False,
    )


def collect_all(conn: Any, force: bool = False) -> list[AccountUsage]:
    """Collect usage for all active accounts.

    If force=True, refresh every account regardless of TTL/backoff.
    Each account always produces a row.
    """
    accounts = store.list_accounts(conn)
    results: list[AccountUsage] = [
        _collect_for_account(account, conn, force=force) for account in accounts
    ]
    order = {p: i for i, p in enumerate(_PROVIDER_ORDER)}
    results.sort(key=lambda r: (order.get(r.provider, 99), r.label))
    return results


def read_cached_all(conn: Any) -> list[AccountUsage]:
    """Return the latest stored sample for every account — zero network I/O.

    Used by the Discord bot's `/usage` slash command so the initial response is
    always instant. Staleness is visible via ``fetched_at`` / `_age_str`; the
    refresh button is the explicit live-sync trigger.
    """
    accounts = store.list_accounts(conn)
    results: list[AccountUsage] = []
    for account in accounts:
        latest = store.latest_usage_sample(conn, account["id"])
        if latest:
            results.append(AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"],
                json.loads(latest["windows"]), "cached",
                latest["fetched_at"], None, False,
            ))
        else:
            results.append(AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"], None,
                "no-data", None, None, False,
            ))
    order = {p: i for i, p in enumerate(_PROVIDER_ORDER)}
    results.sort(key=lambda r: (order.get(r.provider, 99), r.label))
    return results


def collect_one(conn: Any, label: str, force: bool = True) -> AccountUsage | None:
    """Force-collect a single account by label. Used by `tracker sync <label>`."""
    account = store.get_account_by_label(conn, label)
    if not account:
        return None
    return _collect_for_account(account, conn, force=True)


def _serve_cached(
    account: Any, conn: Any, state: Any | None, *, backing_off: bool
) -> AccountUsage:
    latest = store.latest_usage_sample(conn, account["id"])
    source = "backing-off" if backing_off else "cached"
    if latest:
        return AccountUsage(
            account["id"], account["provider"], account["label"],
            account["email"], account["tier"],
            json.loads(latest["windows"]), source,
            latest["fetched_at"], None, False,
        )
    return AccountUsage(
        account["id"], account["provider"], account["label"],
        account["email"], account["tier"], None, "no-data", None, None, False,
    )


def _no_data(account: Any, error: str) -> AccountUsage:
    return AccountUsage(
        account["id"], account["provider"], account["label"],
        account["email"], account["tier"], None,
        "no-data", None, error, False,
    )