# CHECKPOINT

## Now

`tracker remove` is fixed, the fix is live on this machine, and the work is
committed and pushed to `origin/master`. Working tree clean; nothing in flight.

The real reason remove "still didn't work" was **not** in the code: `tracker` on
`PATH` was a `uv tool install` snapshot of PyPI `ai-quota-tracker` 0.2.2, so the
selector fix from commit `2d2ea11` was never running. It has been reinstalled
from this checkout with `uv tool install --force --editable .`, and
`tracker-bot.service` was restarted onto the new code. Future edits to `src/`
now take effect immediately.

The live store still has all 4 accounts, untouched — including the duplicate
Grok account (a second Grok login under a different email, currently blocked).
`tracker remove grok` lists both with their short ids; name the one you want by
`grok:<label>` or by id.

## What changed

- **`uv tool install --force --editable .`** — the installed `tracker` is now
  this checkout, not a PyPI snapshot. `systemctl --user restart
  tracker-bot.service` picked it up.
- `store.find_accounts` gained two tiers: an **account id** (exact, or a unique
  prefix of 8+ chars, gated on hex-ish input) and **email** alongside label.
  Tiers resolve in order id → `provider:name` → label/email → provider.
- `store.remove_account` deletes `usage_samples`, `token_usage`,
  `rate_limit_events` and `fetch_state` explicitly inside one transaction
  instead of trusting `ON DELETE CASCADE`.
- `store._prune_orphans` runs on `connect()` and clears child rows left by
  older removals (the real DB had one orphan `fetch_state` row; now zero).
- `credentials.delete_credential` returns an error string instead of swallowing
  every `OSError`; `remove` prints it and exits non-zero.
- `tracker remove` takes **1+ selectors** (`nargs="+"`). Every selector is
  resolved before anything is deleted — one bad selector aborts the whole
  command with "nothing removed". Overlapping selectors dedupe by id.
- Ambiguity and no-match output now lists the short id of every candidate, so
  there is always a selector that names exactly one row.
- Every `--help` screen ends in an `examples:` block, built by `_epilog` /
  `_subcommand` in `cli.py` with `RawDescriptionHelpFormatter`.
- Test fixtures and doc examples use synthetic account uuids; the real store's
  ids and emails are deliberately not committed to this public repo.

## Locked decisions

- **Selectors never substring-match**, with one carve-out: an id prefix of 8+
  hex chars. 8 chars of a uuid4 is specific, and an id is the only way to name
  one of two accounts sharing provider *and* label. Anything shorter misses.
- **Label and email share one resolution tier.** A collision between them must
  surface as ambiguity, never let one silently shadow the other.
- **Multi-selector remove is atomic in its refusal.** A destructive command
  that half-ran is worse than one that refused while everything is still there.
- **Do not go back to trusting `ON DELETE CASCADE`.** `PRAGMA foreign_keys` is
  per-connection, off by default, and the schema migrations in `store.py`
  switch it off. `test_remove_survives_foreign_keys_off` guards this.
- **The editable install is deliberate.** A snapshot install is what made this
  bug invisible for a whole release. Cost: `tracker` (and the bot service) now
  depend on `~/Code/tracker` existing. Revert with
  `uv tool install --force ai-quota-tracker`.
- Version stays `0.2.2`; everything sits under `## [Unreleased]`. Bumping and
  releasing is the user's call.

## Verification performed

- `python -m unittest discover -s tests` — **41 tests pass** (38 before this
  session's 3 credential tests; 13 new across selector, deletion, and
  credential cases).
- CLI smoke tests on four sandbox copies of the real store
  (`XDG_DATA_HOME`/`XDG_CONFIG_HOME` redirected, live DB never mutated):
  ambiguous `remove grok` refused with ids; `remove <shared email>` refused with
  all 3 matches including the claude row matched by email; `remove a b` with one
  bad selector removed nothing and exited 1; `remove grok:<email>`, `remove
  <id-prefix>`, `remove a b`, and `remove grok --all` all removed exactly the
  right rows; a full teardown left 0 accounts, 0 child rows, 0 credential files.
- Credential failure path end-to-end: replacing a credential file with a
  directory produced `warning: credential file still on disk: … Is a directory`
  and exit 1.
- Against the real store: `tracker remove zzz` printed all 4 accounts with ids,
  `connect()` pruned the pre-existing orphan `fetch_state` row (1 → 0),
  `tracker status` and `tracker sync --label codex` still work, and
  `tracker-bot.service` is active on the new code.

## Not done / deliberately out of scope

- No version bump, no PyPI release, no commit.
- **`tracker add grok` created a second grok account instead of updating the
  existing one** when the live `~/.grok/auth.json` held a different user. That
  is why there was a duplicate to remove. Dedupe on add is a separate fix —
  `_add_or_update_account` only matches on `provider_account_id`.
- The 17 MB WAL next to a 19 MB `tracker.db` was noticed and not investigated.
- No confirmation prompt on `remove`. The complaint was removals that failed,
  not removals that happened by accident.
