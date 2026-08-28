"""Tests for account selectors — `tracker remove codex` and friends.

Guards the bug where every account-naming command matched on *label only*.
Labels are emails, and one email routinely has both a Grok and a Codex
account, so `tracker remove codex` failed outright while
`tracker remove me@example.com` silently picked whichever row sorted first.

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


class FindAccountsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _fresh_store(self._tmp.name)
        self.conn = self.store.connect()
        for acct_id, provider, label in (
            ("a1", "claude", "fundflow"),
            ("a2", "grok", "shared@example.com"),
            ("a3", "codex", "shared@example.com"),
            ("a4", "zai", "zai-8f2a"),
        ):
            self.store.add_account(
                self.conn, id=acct_id, provider=provider, label=label,
                email=None, provider_account_id=None, org_id=None, tier=None,
            )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _ids(self, selector: str) -> list[str]:
        return [r["id"] for r in self.store.find_accounts(self.conn, selector)]

    def test_provider_name_resolves(self) -> None:
        """The reported bug: a bare provider name must name its accounts."""
        self.assertEqual(self._ids("codex"), ["a3"])
        self.assertEqual(self._ids("zai"), ["a4"])

    def test_unique_label_resolves(self) -> None:
        self.assertEqual(self._ids("fundflow"), ["a1"])

    def test_shared_label_returns_every_match(self) -> None:
        """An ambiguous label must surface both rows, not pick one blind."""
        self.assertEqual(self._ids("shared@example.com"), ["a3", "a2"])

    def test_provider_colon_label_disambiguates(self) -> None:
        self.assertEqual(self._ids("codex:shared@example.com"), ["a3"])
        self.assertEqual(self._ids("grok:shared@example.com"), ["a2"])

    def test_matching_is_case_insensitive(self) -> None:
        self.assertEqual(self._ids("CODEX"), ["a3"])
        self.assertEqual(self._ids("Codex:Shared@Example.com"), ["a3"])

    def test_no_substring_guessing(self) -> None:
        """Partial input must miss rather than hit an unintended account."""
        self.assertEqual(self._ids("cod"), [])
        self.assertEqual(self._ids("shared"), [])
        self.assertEqual(self._ids(""), [])

    def test_mismatched_provider_and_label_misses(self) -> None:
        self.assertEqual(self._ids("claude:shared@example.com"), [])

    def test_removed_accounts_are_not_matched(self) -> None:
        self.store.remove_account(self.conn, "a3")
        self.assertEqual(self._ids("codex"), [])
        self.assertEqual(self._ids("shared@example.com"), ["a2"])


if __name__ == "__main__":
    unittest.main()
