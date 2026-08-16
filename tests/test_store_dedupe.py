"""Regression tests for token_usage de-duplication.

Guards the bug where `insert_token_usage` used INSERT OR IGNORE against a table
with no UNIQUE constraint, so every sync re-inserted the full transcript
history. That inflated SUM()-based totals (lifetime cost showed as $10.4M
instead of $5.9K) and grew the database to 808 MB.

Stdlib unittest only, to honor the project's "no new heavy deps" norm.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest


def _fresh_store(tmp: str):
    """Import tracker.store against an isolated XDG_DATA_HOME."""
    os.environ["XDG_DATA_HOME"] = tmp
    import importlib

    from tracker import paths, store

    importlib.reload(paths)
    importlib.reload(store)
    return store


def _row(account_id: str, session_id: str, ts: float, tokens: int = 10) -> dict:
    return {
        "account_id": account_id,
        "session_id": session_id,
        "ts": ts,
        "model": "grok-4",
        "input_tokens": tokens,
        "output_tokens": tokens,
        "cache_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd_ticks": 1000,
        "cost_estimate": 0.001,
    }


class TokenUsageDedupeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _fresh_store(self._tmp.name)
        self.conn = self.store.connect()
        self.conn.execute(
            "INSERT INTO accounts (id,provider,label,email,tier,is_active,added_at)"
            " VALUES ('a1','grok','main','x@example.com','5',1,0)"
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_repeated_sync_does_not_duplicate(self) -> None:
        """Re-inserting the same rows must be a no-op, not unbounded growth."""
        rows = [_row("a1", "s1", 100.0), _row("a1", "s2", 200.0)]
        for _ in range(50):  # simulate 50 syncs of an unchanged transcript
            self.store.insert_token_usage(self.conn, rows)

        count = self.conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
        self.assertEqual(count, 2, "repeated syncs must not duplicate rows")

    def test_totals_stay_accurate_across_syncs(self) -> None:
        """The real defect: duplicates were SUMmed and inflated reported totals."""
        rows = [_row("a1", "s1", 100.0, tokens=500)]
        for _ in range(20):
            self.store.insert_token_usage(self.conn, rows)

        total = self.conn.execute(
            "SELECT SUM(input_tokens) FROM token_usage WHERE account_id='a1'"
        ).fetchone()[0]
        self.assertEqual(total, 500, "totals must not scale with sync count")

    def test_distinct_events_still_recorded(self) -> None:
        """Dedup must not swallow genuinely different events."""
        self.store.insert_token_usage(
            self.conn,
            [
                _row("a1", "s1", 100.0),
                _row("a1", "s1", 101.0),  # same session, different ts
                _row("a1", "s2", 100.0),  # different session, same ts
            ],
        )
        count = self.conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
        self.assertEqual(count, 3)


class LegacyMigrationTest(unittest.TestCase):
    """A pre-0.2.2 database full of duplicates must migrate cleanly on open."""

    def test_migration_collapses_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_DATA_HOME"] = tmp
            import importlib

            from tracker import paths, store

            importlib.reload(paths)
            importlib.reload(store)

            db = paths.db_path()
            db.parent.mkdir(parents=True, exist_ok=True)

            # Build a legacy DB: no UNIQUE index, duplicated rows.
            legacy = sqlite3.connect(db)
            legacy.executescript(
                """
                CREATE TABLE accounts (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
                    email TEXT, provider_account_id TEXT, org_id TEXT, tier TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    added_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE token_usage (
                    account_id TEXT NOT NULL, session_id TEXT NOT NULL, ts REAL NOT NULL,
                    model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                    cache_tokens INTEGER, reasoning_tokens INTEGER,
                    cost_usd_ticks INTEGER, cost_estimate REAL
                );
                INSERT INTO accounts (id,provider,label,added_at)
                    VALUES ('a1','grok','main',0);
                """
            )
            for _ in range(100):  # 100 copies of 2 real events
                legacy.executemany(
                    "INSERT INTO token_usage"
                    " (account_id,session_id,ts,input_tokens,output_tokens,cost_estimate)"
                    " VALUES (?,?,?,?,?,?)",
                    [("a1", "s1", 100.0, 7, 3, 0.5), ("a1", "s2", 200.0, 7, 3, 0.5)],
                )
            legacy.commit()
            before = legacy.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
            legacy.close()
            self.assertEqual(before, 200)

            # Opening through store.connect() must migrate transparently.
            conn = store.connect()
            after = conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
            self.assertEqual(after, 2, "migration must collapse duplicates")

            total = conn.execute("SELECT SUM(input_tokens) FROM token_usage").fetchone()[0]
            self.assertEqual(total, 14, "post-migration totals must be truthful")

            self.assertTrue(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='idx_token_usage_dedup'"
                ).fetchone(),
                "UNIQUE index must exist so OR IGNORE works from here on",
            )

            # And it must stay clean on the next sync.
            store.insert_token_usage(
                conn,
                [_row("a1", "s1", 100.0), _row("a1", "s2", 200.0)],
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0], 2
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
