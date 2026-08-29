"""Tests for naming and removing accounts — `tracker remove codex` and friends.

Guards two bugs. First, every account-naming command matched on *label only*;
labels are emails, and one email routinely has both a Grok and a Codex account,
so `tracker remove codex` failed outright while `tracker remove me@example.com`
silently picked whichever row sorted first. Second, removal deleted only the
accounts row and trusted a foreign-key cascade that is not always armed, so a
removed account's usage history and backoff stayed in the database.

Stdlib unittest only, to honor the project's "no new heavy deps" norm.
"""

from __future__ import annotations

import os
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


# Real accounts get uuid4 ids; the id selector is gated on hex-ish input, so
# the fixture has to use realistic ones for those tests to mean anything.
ID_CLAUDE = "0c1a5f3e-1111-4111-8111-111111111111"
ID_GROK = "7b9d20a4-2222-4222-8222-222222222222"
ID_CODEX = "9f3c1a20-3333-4333-8333-333333333333"
ID_ZAI = "9f3c1a20-4444-4444-8444-444444444444"  # shares 8 chars with ID_CODEX


class FindAccountsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _fresh_store(self._tmp.name)
        self.conn = self.store.connect()
        for acct_id, provider, label, email in (
            (ID_CLAUDE, "claude", "fundflow", "shared@example.com"),
            (ID_GROK, "grok", "shared@example.com", "shared@example.com"),
            (ID_CODEX, "codex", "shared@example.com", "shared@example.com"),
            (ID_ZAI, "zai", "zai-8f2a", None),
        ):
            self.store.add_account(
                self.conn, id=acct_id, provider=provider, label=label,
                email=email, provider_account_id=None, org_id=None, tier=None,
            )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _ids(self, selector: str) -> list[str]:
        return [r["id"] for r in self.store.find_accounts(self.conn, selector)]

    def test_provider_name_resolves(self) -> None:
        """The reported bug: a bare provider name must name its accounts."""
        self.assertEqual(self._ids("codex"), [ID_CODEX])
        self.assertEqual(self._ids("zai"), [ID_ZAI])

    def test_unique_label_resolves(self) -> None:
        self.assertEqual(self._ids("fundflow"), [ID_CLAUDE])

    def test_shared_label_returns_every_match(self) -> None:
        """An ambiguous label must surface every row, not pick one blind."""
        self.assertEqual(
            self._ids("shared@example.com"), [ID_CLAUDE, ID_CODEX, ID_GROK]
        )

    def test_email_matches_even_when_the_label_differs(self) -> None:
        """The claude row's label is 'fundflow'; its email must name it too."""
        self.assertIn(ID_CLAUDE, self._ids("shared@example.com"))
        self.assertEqual(self._ids("claude:shared@example.com"), [ID_CLAUDE])

    def test_provider_colon_label_disambiguates(self) -> None:
        self.assertEqual(self._ids("codex:shared@example.com"), [ID_CODEX])
        self.assertEqual(self._ids("grok:shared@example.com"), [ID_GROK])

    def test_matching_is_case_insensitive(self) -> None:
        self.assertEqual(self._ids("CODEX"), [ID_CODEX])
        self.assertEqual(self._ids("Codex:Shared@Example.com"), [ID_CODEX])
        self.assertEqual(self._ids(ID_CODEX.upper()), [ID_CODEX])

    def test_id_resolves(self) -> None:
        """An id is the only way to name one of two otherwise identical rows."""
        self.assertEqual(self._ids(ID_GROK), [ID_GROK])

    def test_unique_id_prefix_resolves(self) -> None:
        self.assertEqual(self._ids("9f3c1a20-4"), [ID_ZAI])

    def test_shared_id_prefix_returns_every_match(self) -> None:
        """A prefix two ids share must be reported, not resolved arbitrarily."""
        self.assertEqual(self._ids("9f3c1a20"), [ID_CODEX, ID_ZAI])

    def test_short_id_prefix_misses(self) -> None:
        """Under 8 chars is not specific enough to be treated as an id."""
        self.assertEqual(self._ids("9f3c1a2"), [])

    def test_no_substring_guessing(self) -> None:
        """Partial input must miss rather than hit an unintended account."""
        self.assertEqual(self._ids("cod"), [])
        self.assertEqual(self._ids("shared"), [])
        self.assertEqual(self._ids("shared@example.co"), [])
        self.assertEqual(self._ids(""), [])

    def test_mismatched_provider_and_label_misses(self) -> None:
        self.assertEqual(self._ids("claude:zai-8f2a"), [])

    def test_removed_accounts_are_not_matched(self) -> None:
        self.store.remove_account(self.conn, ID_CODEX)
        self.assertEqual(self._ids("codex"), [])
        self.assertEqual(self._ids("shared@example.com"), [ID_CLAUDE, ID_GROK])


CHILD_TABLES = ("usage_samples", "token_usage", "rate_limit_events", "fetch_state")


class RemoveAccountTest(unittest.TestCase):
    """`remove` must leave nothing behind, whatever the FK pragma is doing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _fresh_store(self._tmp.name)
        self.conn = self.store.connect()
        for acct_id, provider in ((ID_CODEX, "codex"), (ID_GROK, "grok")):
            self.store.add_account(
                self.conn, id=acct_id, provider=provider, label=provider,
                email=None, provider_account_id=None, org_id=None, tier=None,
            )
            self._seed_children(acct_id)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _seed_children(self, account_id: str) -> None:
        self.store.insert_usage_sample(
            self.conn, account_id=account_id, source="api", windows={"five_hour": {"pct": 1}},
        )
        self.store.insert_token_usage(
            self.conn,
            [{"account_id": account_id, "session_id": f"s-{account_id}", "ts": 1.0,
              "model": "m", "input_tokens": 1, "output_tokens": 1, "cache_tokens": 0,
              "reasoning_tokens": 0, "cost_usd_ticks": 0, "cost_estimate": 0.0}],
        )
        self.store.insert_rate_limit_event(
            self.conn, account_id=account_id, kind="hit", message="x", retry_after=None,
        )
        self.store.upsert_fetch_state(
            self.conn, account_id=account_id, consecutive_failures=1,
        )

    def _child_counts(self, account_id: str) -> dict[str, int]:
        return {
            t: self.conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE account_id=?", (account_id,)
            ).fetchone()[0]
            for t in CHILD_TABLES
        }

    def test_children_are_seeded(self) -> None:
        """Guard the fixture: the cascade test is vacuous without child rows."""
        self.assertEqual(self._child_counts(ID_CODEX), dict.fromkeys(CHILD_TABLES, 1))

    def test_remove_deletes_every_child_row(self) -> None:
        self.store.remove_account(self.conn, ID_CODEX)
        self.assertEqual(self._child_counts(ID_CODEX), dict.fromkeys(CHILD_TABLES, 0))

    def test_remove_leaves_other_accounts_intact(self) -> None:
        self.store.remove_account(self.conn, ID_CODEX)
        self.assertEqual(self._child_counts(ID_GROK), dict.fromkeys(CHILD_TABLES, 1))
        self.assertIsNotNone(self.store.get_account(self.conn, ID_GROK))

    def test_remove_survives_foreign_keys_off(self) -> None:
        """The pragma is per-connection and the migrations turn it off."""
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.store.remove_account(self.conn, ID_CODEX)
        self.assertEqual(self._child_counts(ID_CODEX), dict.fromkeys(CHILD_TABLES, 0))

    def test_connect_prunes_orphans_left_by_older_versions(self) -> None:
        """Older removals dropped only the accounts row, orphaning the rest."""
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DELETE FROM accounts WHERE id=?", (ID_CODEX,))
        self.assertEqual(self._child_counts(ID_CODEX), dict.fromkeys(CHILD_TABLES, 1))
        self.conn.close()

        self.conn = self.store.connect()
        self.assertEqual(self._child_counts(ID_CODEX), dict.fromkeys(CHILD_TABLES, 0))
        self.assertEqual(self._child_counts(ID_GROK), dict.fromkeys(CHILD_TABLES, 1))


class DeleteCredentialTest(unittest.TestCase):
    """A failed credential delete must be reported, not swallowed.

    Otherwise `remove` prints success while an OAuth refresh token or API key
    stays on disk under an account that no longer exists.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._tmp.name
        import importlib

        from tracker import credentials, paths

        importlib.reload(paths)
        importlib.reload(credentials)
        self.credentials = credentials
        paths.ensure_dirs()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_deleting_an_existing_credential_succeeds(self) -> None:
        self.credentials.write_credential(ID_CODEX, {"access_token": "x"})
        self.assertIsNone(self.credentials.delete_credential(ID_CODEX))
        self.assertFalse(os.path.exists(self.credentials.cred_path(ID_CODEX)))

    def test_missing_credential_is_not_an_error(self) -> None:
        """Removal has to stay usable for an account whose file is already gone."""
        self.assertIsNone(self.credentials.delete_credential(ID_GROK))

    def test_undeletable_credential_is_reported(self) -> None:
        path = self.credentials.cred_path(ID_CLAUDE)
        os.mkdir(path)  # stands in for any OSError unlink can raise
        error = self.credentials.delete_credential(ID_CLAUDE)
        self.assertIsNotNone(error)
        self.assertIn(path, error)


if __name__ == "__main__":
    unittest.main()
