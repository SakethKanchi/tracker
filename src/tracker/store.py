"""SQLite storage layer: accounts, usage_samples, token_usage, rate_limit_events, fetch_state."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from . import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                  TEXT PRIMARY KEY,
    provider            TEXT NOT NULL CHECK(provider IN ('claude','grok')),
    label               TEXT NOT NULL,
    email               TEXT,
    provider_account_id TEXT,
    org_id              TEXT,
    tier                TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    added_at            REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_samples (
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    fetched_at  REAL NOT NULL,
    source      TEXT NOT NULL CHECK(source IN ('api','manual','derived')),
    windows     TEXT NOT NULL,
    PRIMARY KEY (account_id, fetched_at)
);
CREATE TABLE IF NOT EXISTS token_usage (
    account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL,
    ts              REAL NOT NULL,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_tokens    INTEGER,
    reasoning_tokens INTEGER,
    cost_usd_ticks  INTEGER,
    cost_estimate   REAL
);
CREATE TABLE IF NOT EXISTS rate_limit_events (
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ts          REAL NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT,
    retry_after  REAL
);
CREATE TABLE IF NOT EXISTS fetch_state (
    account_id           TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    last_attempt_at      REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    backoff_until        REAL,
    last_error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_samples_account ON usage_samples(account_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_usage_account ON token_usage(account_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_rate_limit_account ON rate_limit_events(account_id, ts DESC);
"""


def connect() -> sqlite3.Connection:
    paths.ensure_dirs()
    conn = sqlite3.connect(paths.db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    paths.db_path().chmod(0o600)
    return conn


# ── accounts ──

def add_account(
    conn: sqlite3.Connection,
    *,
    id: str,
    provider: str,
    label: str,
    email: str | None,
    provider_account_id: str | None,
    org_id: str | None,
    tier: str | None,
) -> None:
    conn.execute(
        """INSERT INTO accounts (id,provider,label,email,provider_account_id,org_id,tier,is_active,added_at)
           VALUES (?,?,?,?,?,?,?,1,?)""",
        (id, provider, label, email, provider_account_id, org_id, tier, time.time()),
    )


def list_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM accounts WHERE is_active=1 ORDER BY provider, label"
    ).fetchall()


def get_account_by_label(conn: sqlite3.Connection, label: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE label=? AND is_active=1", (label,)
    ).fetchone()


def get_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def remove_account(conn: sqlite3.Connection, account_id: str) -> None:
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))

def find_by_provider_account_id(
    conn: sqlite3.Connection, provider: str, provider_account_id: str
) -> sqlite3.Row | None:
    """Find an existing account by provider + provider_account_id (for dedup)."""
    return conn.execute(
        "SELECT * FROM accounts WHERE provider=? AND provider_account_id=? AND is_active=1",
        (provider, provider_account_id),
    ).fetchone()


# ── usage_samples ──

def insert_usage_sample(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    source: str,
    windows: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO usage_samples (account_id,fetched_at,source,windows) VALUES (?,?,?,?)",
        (account_id, time.time(), source, json.dumps(windows)),
    )


def latest_usage_sample(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM usage_samples WHERE account_id=? ORDER BY fetched_at DESC LIMIT 1",
        (account_id,),
    ).fetchone()


# ── token_usage ──

def insert_token_usage(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> int:
    """Batch insert token-usage rows. Returns count inserted."""
    if not rows:
        return 0
    conn.executemany(
        """INSERT OR IGNORE INTO token_usage
           (account_id,session_id,ts,model,input_tokens,output_tokens,cache_tokens,reasoning_tokens,cost_usd_ticks,cost_estimate)
           VALUES (:account_id,:session_id,:ts,:model,:input_tokens,:output_tokens,:cache_tokens,:reasoning_tokens,:cost_usd_ticks,:cost_estimate)""",
        rows,
    )
    return len(rows)  # OR IGNORE may dedupe; callers check the cursor if exact count matters


def token_usage_since(
    conn: sqlite3.Connection, account_id: str | None, since_ts: float = 0
) -> list[sqlite3.Row]:
    if account_id:
        return conn.execute(
            "SELECT * FROM token_usage WHERE account_id=? AND ts>=? ORDER BY ts DESC",
            (account_id, since_ts),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM token_usage WHERE ts>=? ORDER BY ts DESC", (since_ts,)
    ).fetchall()


# ── rate_limit_events ──

def insert_rate_limit_event(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    kind: str,
    message: str | None = None,
    retry_after: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO rate_limit_events (account_id,ts,kind,message,retry_after) VALUES (?,?,?,?,?)",
        (account_id, time.time(), kind, message, retry_after),
    )


def latest_rate_limit(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rate_limit_events WHERE account_id=? ORDER BY ts DESC LIMIT 1",
        (account_id,),
    ).fetchone()


# ── fetch_state ──

def get_fetch_state(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fetch_state WHERE account_id=?", (account_id,)
    ).fetchone()


# Sentinel: distinguish "caller omitted this field" from "caller set it to None"
# (None is meaningful for backoff_until / last_error — it clears them on success).
_UNSET: object = object()


def upsert_fetch_state(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    last_attempt_at: float | None | object = _UNSET,
    consecutive_failures: int | None | object = _UNSET,
    backoff_until: float | None | object = _UNSET,
    last_error: str | None | object = _UNSET,
) -> None:
    """Upsert fetch_state. Omitted fields are preserved; explicit None clears.

    Uses INSERT OR REPLACE because SQLite's ON CONFLICT DO UPDATE clause
    re-evaluates the FOREIGN KEY constraint on this build (3.53.4) even when
    the conflict path doesn't run, whereas INSERT OR REPLACE does not.
    """
    # Read existing row so unspecified columns are preserved (INSERT OR REPLACE
    # would otherwise blank them to defaults).
    existing = conn.execute(
        "SELECT last_attempt_at, consecutive_failures, backoff_until, last_error "
        "FROM fetch_state WHERE account_id=?",
        (account_id,),
    ).fetchone()
    row = dict(existing) if existing else {}

    for name, val in [
        ("last_attempt_at", last_attempt_at),
        ("consecutive_failures", consecutive_failures),
        ("backoff_until", backoff_until),
        ("last_error", last_error),
    ]:
        if val is not _UNSET:
            row[name] = val

    conn.execute(
        """INSERT OR REPLACE INTO fetch_state
           (account_id, last_attempt_at, consecutive_failures, backoff_until, last_error)
           VALUES (?,?,?,?,?)""",
        (
            account_id,
            row.get("last_attempt_at"),
            row.get("consecutive_failures", 0),
            row.get("backoff_until"),
            row.get("last_error"),
        ),
    )