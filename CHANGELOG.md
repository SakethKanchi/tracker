# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
