# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`tracker list --refresh` took ~18s; now ~3s.** Three separate causes:
  - `token_usage` had no UNIQUE constraint, so the `INSERT OR IGNORE` in
    `insert_token_usage` never deduped. Every sync re-inserted the full grok
    transcript history: 4,493,404 rows for 1,464 real events (each counted
    4,450x) in an 808 MB database. Added a UNIQUE index on
    (account_id, session_id, ts) plus a one-time migration that collapses
    existing duplicates and VACUUMs. Database is now 9.1 MB.
  - **This also corrupted reported totals.** Because duplicates were summed,
    lifetime grok cost displayed as $10,463,928 instead of the true $5,892.
    Token counts were inflated by the same factor.
  - `_parse_updates_file` ran `json.loads` on every line of 653 MB of session
    transcripts, though only ~0.8% are `turn_completed`. Added a substring
    pre-filter: 2,736 ms -> 715 ms, same 657 rows parsed.
- `collect_all` fetched accounts serially. It now overlaps them with a thread
  pool (one sqlite connection per worker), since the work is I/O-bound.

## [0.2.1] — 2026-08-16

### Changed

- **PyPI distribution renamed to `ai-quota-tracker`.** The previously documented
  name `ai-usage-tracker` was already claimed on PyPI by an unrelated project.
  The console command and import package are unchanged (`tracker`).
- Packaging metadata: author email, `Changelog` project URL, `End Users/Desktop`
  and `System :: Monitoring` classifiers, `CHANGELOG.md` + `SECURITY.md` shipped
  in the sdist.
- README links and the demo screenshot now use absolute URLs so they render on
  the PyPI project page.

### Added

- `tracker list --watch [SECONDS]` — live dashboard that redraws in place
  (`rich.live`, no new dependency). Collection still honors the refresh-if-stale
  TTL and 429 backoff, so a short redraw interval does not increase API polling.
  Falls back to a single render when output is not a terminal.
- `tracker status` now names the account with the most headroom
  (`best: Grok main  95% free`), computed as `100 - worst window used` across
  every window a provider reports.

### Fixed

- Progress bars adapt to the terminal width. The bar was hard-coded to 20
  chars, so on panes narrower than ~60 columns lines wrapped and the ├/└ tree
  connectors broke apart. Bars now shrink to a floor of 8 chars and long emails
  are elided, so no line overflows at any width (verified 40-200 columns).
- Percentages above 100 or below 0 no longer overflow the bar; the true value
  is still printed.
- Ship a `py.typed` marker. The package declared the `Typing :: Typed`
  classifier but shipped no marker, so type checkers silently ignored its
  annotations in downstream projects (PEP 561).

### Added

- CI workflow (`.github/workflows/ci.yml`): install, byte-compile, CLI smoke
  test, and `twine check` on Python 3.11–3.13.
- README troubleshooting table and FAQ.
- Demo screenshots now cover all five providers (previously only Claude + Grok).

## [0.2.0] — 2026-08-03

### Added

- **Codex** provider: import from `~/.codex/auth.json`, WHAM usage windows
  (`primary` / `secondary`), full OAuth refresh with write-back to the CLI file
  (required because Codex refresh tokens are single-use)
- **Gemini** provider: API-key accounts via Google AI Studio keys (`AIza…`)
- **OpenAI** platform API-key accounts (`sk-…` / `sk-proj-…`)
- Auto-detect API keys: `tracker add <api_key>` or `tracker --add <api_key>`
  recognizes Claude (`sk-ant-`), Grok (`xai-`), Gemini (`AIza`), OpenAI (`sk-`)
- Schema migration drops the legacy `accounts.provider` CHECK so new providers
  work on existing databases

### Changed

- Dashboard / Discord / status cover claude, grok, codex, gemini, openai
- Version bumped to 0.2.0

## [0.1.0] — 2026-08-03

### Added

- Unified Claude + Grok multi-account usage dashboard (`tracker list`)
- Credential import from official CLIs (`tracker add claude|grok`)
- Refresh-if-stale collection with per-account 429 backoff
- Claude live windows: 5h, 7d, scoped models, spend
- Grok weekly/monthly credit bars + quota blocked signal
- Historical token report (`tracker tokens`)
- Manual usage log (`tracker log`) for providers without a live window
- Discord webhook poller (`tracker webhook`) — post once, edit on interval
- FOSS packaging: MIT license, README, architecture docs, security notes

### Fixed

- `tracker log` was registered in argparse but missing from the command dispatch map
- `tracker tokens --provider` was accepted but ignored
