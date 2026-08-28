# CHECKPOINT

## Now

Committed and pushed to `origin/master`: **Z.ai (GLM Coding Plan) provider** +
**account selector fix** for `remove` / `sync --label` / `log`. Working tree
clean; nothing is in flight.

Only remaining step is the user's own: add the real subscription with

```bash
tracker add zai        # reads $Z_AI_API_KEY / $ANTHROPIC_AUTH_TOKEN / ~/.claude/settings.json
# or
tracker add <32hex>.<secret>
```

Version stays `0.2.2` and the changelog entry sits under `## [Unreleased]`, so
no release has been cut.

## What changed

- `providers/zai.py` — quota client for `GET {base}/api/monitor/usage/quota/limit`
  on `api.z.ai` and `open.bigmodel.cn`. Emits `five_hour` / `seven_day` /
  `scoped` windows, so tui, `tracker status` headroom, and the Discord poller
  needed no new plumbing.
- `store.find_accounts(conn, selector)` replaced `get_account_by_label`.
  Selector = label | provider | `provider:label`, case-insensitive, exact.
- `tracker remove` refuses an ambiguous selector (lists the `provider:label`
  alternatives) unless `--all` is passed. `sync --label` refreshes every match.
  `_collect_initial` now resolves the new account by id.
- Discovery for z.ai keys lives in `credentials.import_zai_credential()`.

## Locked decisions

- **Windows reuse Claude's keys** (`five_hour`, `seven_day`, `scoped`) instead of
  z.ai-specific names. This is what makes `_headroom_pct`, the Discord severity
  calculation, and `tracker status` work for z.ai without special cases.
- **The z.ai quota endpoint returns HTTP 200 for auth failures.** Only the body's
  `success` flag and `code` (1000-1099 = dead key) may be trusted. Do not
  "simplify" that to a status-code check.
- **No token history for z.ai.** The `model-usage` endpoint returns 24h
  aggregates with no session id, so rows cannot be deduped into `token_usage`.
  `tracker tokens --provider zai` is intentionally empty.
- **Selectors never substring-match.** A partial string is a miss with a list of
  the real options — silently hitting the wrong account is the bug being fixed.
- Version stays `0.2.2`; the changelog entry sits under `## [Unreleased]`.
  Bumping and releasing is the user's call.

## Verification performed

- `python -m unittest discover -s tests` — 28 tests, all pass (4 pre-existing,
  24 new across `test_zai_windows.py` and `test_account_selector.py`).
- Live network check against `api.z.ai`: `tracker add zai` with a bogus key in
  `$Z_AI_API_KEY`, from `~/.claude/settings.json`, and pasted directly — all
  three report `invalid z.ai API key (Authentication Failed)`. A real key is
  needed to see populated bars.
- CLI smoke test on an isolated store seeded to mirror the real one
  (grok + codex sharing one email, plus a z.ai account): `list`, `status`,
  `remove codex`, ambiguous `remove <email>` (refused), `--all`,
  `remove provider:label`, `log`, missing selector.
- Read-only check against the real `~/.local/share/tracker/tracker.db`:
  `codex` resolves to the real codex account; the shared email reports both.
- `scripts/generate_screenshots.py` regenerated; the README sample matches.

## Not done / deliberately out of scope

- No version bump, no PyPI release.
- No z.ai token/cost history (see locked decisions).
- `tracker sync --label` keeps its flag name even though it now takes a
  selector; renaming it would break existing muscle memory and scripts.
