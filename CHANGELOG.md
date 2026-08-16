# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

### Fixed

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
