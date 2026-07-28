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
from .providers import claude, grok

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
    if cred_blob:
        access_token = cred_blob.get("key") or cred_blob.get("access_token")
        if access_token:
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

            # If usage is pinned at 100 OR billing failed, ask v1/models for
            # the structured blocked reason (gives the friendly "out of credits"
            # message).
            pct = windows.get("credit_usage_pct")
            if pct is None or pct >= 100:
                quota = grok.check_live_quota(access_token)
                windows["quota_status"] = quota["status"]
                if quota["status"] == "blocked":
                    windows["quota_reason"] = quota.get("reason", "")
                    windows["quota_message"] = quota.get("message", "")
                    store.insert_rate_limit_event(
                        conn, account_id=acct_id,
                        kind=quota.get("reason", "blocked"),
                        message=quota.get("message"),
                    )
            else:
                windows["quota_status"] = "active"
    store.insert_usage_sample(conn, account_id=acct_id, source="derived", windows=windows)

    return AccountUsage(
        account["id"], account["provider"], account["label"],
        account["email"], account["tier"], windows,
        "derived", time.time(), None, False,
    )


def collect_all(conn: Any, force: bool = False) -> list[AccountUsage]:
    """Collect usage for all active accounts.

    If force=True, refresh every account regardless of TTL/backoff.
    Each account always produces a row.
    """
    accounts = store.list_accounts(conn)
    results: list[AccountUsage] = []
    for account in accounts:
        if account["provider"] == "claude":
            results.append(_collect_claude(account, conn, force=force))
        elif account["provider"] == "grok":
            results.append(_collect_grok(account, conn, force=force))
        else:
            results.append(AccountUsage(
                account["id"], account["provider"], account["label"],
                account["email"], account["tier"], None,
                "error", None, f"unknown provider {account['provider']}", False,
            ))
    # Sort: Claude first, then Grok, by label
    results.sort(key=lambda r: (r.provider, r.label))
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
    results.sort(key=lambda r: (r.provider, r.label))
    return results


def collect_one(conn: Any, label: str, force: bool = True) -> AccountUsage | None:
    """Force-collect a single account by label. Used by `tracker sync <label>`."""
    account = store.get_account_by_label(conn, label)
    if not account:
        return None
    if account["provider"] == "claude":
        return _collect_claude(account, conn, force=True)
    else:
        return _collect_grok(account, conn, force=True)


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