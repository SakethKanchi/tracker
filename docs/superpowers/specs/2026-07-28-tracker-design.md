# tracker — Unified Claude + Grok Usage Tracker

**Date:** 2026-07-28
**Status:** Design (approved, pending spec review)
**Phase 1:** CLI + TUI · **Phase 2 (later):** Local web dashboard

## Problem

The user holds multiple Claude subscription accounts and multiple Grok
accounts (all OAuth-signed via Google). Today, checking which account has
usage remaining means manually logging into each one, opening its usage page,
and logging out — repeated across providers. This is slow and error-prone.

## Goal

One local tool that imports every account's credentials, and whose **primary
command** — `tracker list` — shows the current usage of *every* account across
both providers in a single pass, each row labeled with its account. "Which
account has quota left?" becomes one command, not a login/logout ritual
repeated per account.

**Freshness model (refresh-if-stale, else cache):** `tracker list` displays
each account immediately. Per account, if its last sample is older than the
serve TTL (a few minutes), a network refresh runs *for that account* before
its row renders — so successive runs converge to a fully-fresh snapshot
without a separate "fetch" step, while staying polite to Claude's per-token
usage-request budget. A per-token 429 backoff means an account that just got
throttled keeps showing its last-known windows until the backoff lifts, rather
than re-hammering the budget. `--refresh` (or `tracker sync`) forces a full
network pass. The command *always* renders a row for every account — fresh,
cached, or last-known-during-backoff — so "see everything at once" never
degrades to "see the accounts that happened to be fetchable."

**Scope decision:** the tracker is **read-only observability**. It does not
mutate the active credential the CLIs read, and it does not run an auto-switch
loop. An auto-switch phase can be added later on top of the same credential
store (`tracker switch` + a daemon) without a data-layer rewrite.

## Findings (researched)

### Claude — has a live usage API
- Credentials: `~/.claude/.credentials.json` → `claudeAiOauth.{accessToken,
  refreshToken, expiresAt, scopes}`. Multiple accounts managed by swapping this
  blob (this is what `cswap`, installed at `~/.local/bin/cswap`, does).
- Live quota: `GET https://api.anthropic.com/api/oauth/usage` with the access
  token returns the **5-hour** and **7-day** windows (`%` used, `resets_at`),
  per-model scoped weekly windows, and `extra_usage` pay-as-you-go spend.
- Token refresh: `POST https://platform.claude.com/v1/oauth/token`
  (`grant_type=refresh_token`, client ID `9d1c250a-e61b-44d9-88ed-5944d1962f5e`,
  `User-Agent: claude-swap/1.0` — mirroring cswap's exact call).
- Identity: `GET https://api.anthropic.com/api/oauth/profile` →
  `{account.uuid, email, organization.uuid}`. Resolves whose token it is.
- Penalty-429 handling: the usage endpoint budgets *usage requests* per token;
  honor `Retry-After` and back off (cswap documents an hour-scale rolling
  window). `last_good` remains a valid lower bound until the window resets;
  trust must be bounded and never server-controlled-unbounded.

### Grok — no live usage API
- Credentials: `~/.grok/auth.json` → OIDC token issued by `https://auth.x.ai`
  (`access_token`, `refresh_token`, `expires_at`, `email`, `user_id`,
  `principal_id`, `team_id`, `tier`). Login via `grok login --oauth` (Google).
- Sessions: `~/.grok/sessions/<url-encoded-project>/` plus
  `~/.grok/sessions/session_search.sqlite`.
- **No documented live quota/usage endpoint.** Grok consumer limits surface as
  in-message "usage too high" text or 429s during chat — not a queryable
  `/usage` API. So Grok usage is derived, not fetched:
  1. **Historical tokens** parsed from local session transcripts
     (`~/.grok/sessions/...` + `session_search.sqlite`), ccusage-style.
  2. **Rate-limit capture** from observed use (grok `--debug-file` / session
     logs): record when an account last got throttled.
  3. **Manual entry fallback** (`tracker log grok <label> ...`): the user can
     punch in an eyeballed `--msgs`/`--resets-in` value; stored as a synthetic
     `usage_sample` so it still charts alongside the rest.

### Prior art
- `realiti4/claude-swap` (`cswap`): multi-account switcher + usage dashboard +
  auto-switch for Claude. The endpoint/refresh/poll-policy mechanics above are
  taken from its source. We **reuse its endpoint/client-id/refresh logic**, not
  the binary — the tracker is provider-agnostic and owns its own credential
  store.
- `ryoppippi/ccusage`: parses local agent-CLI session JSONL into token/cost
  reports across many CLIs (incl. Claude). Pattern reference for the historical
  token-accounting feature.

## Architecture

**Stack:** Python (matches the cswap ecosystem), stdlib `sqlite3` + `urllib` /
`httpx`, [`Textual`](https://textual.textualize.io/) for the TUI, later
`FastAPI` for the web phase. One `tracker` entrypoint.

**Layout:**
```
tracker/
  src/tracker/
    cli.py            # argparse entry: add/list/sync/tokens/status/remove/log
    store.py          # SQLite: accounts, usage_samples, token_usage, rate_limit_events
    credentials.py    # read/write/refresh per-provider credential blobs (file, 0600)
    providers/
      claude.py       # refresh + GET /api/oauth/usage + profile; parse windows
      grok.py         # refresh OIDC + parse session transcripts + capture rate-limits
    usage.py          # provider-agnostic "collect one account" dispatch + backoff state
    tui.py            # Textual dashboard (phase 1)
    web/              # FastAPI + UI (phase 2, same store)
  docs/superpowers/specs/2026-07-28-tracker-design.md
```

**Stores (credentials separate from data):**
- Credentials: `~/.config/tracker/credentials/<account-id>.json` (0600,
  per-account file; no master password, no keychain dependency).
- Data: `~/.local/share/tracker/tracker.db` (0600).
- Config: `~/.config/tracker/config.toml`.

## Data model (SQLite)

- **`accounts`** — `id` (TEXT uuid PK), `provider` ('claude'|'grok'),
  `label` (TEXT, user-chosen), `email`, `provider_account_id` (Claude `uuid`,
  Grok `user_id`), `org_id` (Claude `organizationUuid`, Grok `team_id`),
  `tier` (Grok OAuth `tier`; NULL for Claude), `is_active`, `added_at`.
- **`usage_samples`** — time-series of live/derived quota:
  `account_id`, `fetched_at` (epoch s), `source` ('api'|'manual'|'derived'),
  `windows` (JSON). Claude JSON: `{five_hour:{pct,resets_at},
  seven_day:{pct,resets_at}, scoped:[{model,pct,resets_at}], extra_usage,
  spend}`. Grok JSON: `{last_msgs, resets_in, last_rate_limit_at}` or
  synthetic manual values.
- **`token_usage`** — historical per-session token accounting (both
  providers, ccusage-style): `account_id`, `session_id`, `ts`, `model`,
  `input_tokens`, `output_tokens`, `cache_tokens`, `cost_estimate`.
- **`rate_limit_events`** — Grok (and any provider) throttle signals:
  `account_id`, `ts`, `kind` ('429'|'in-message'|'window'), `message`,
  `retry_after`.
- **`fetch_state`** — per-account collection bookkeeping for the refresh-if-
  stale model: `account_id` PK, `last_attempt_at`, `consecutive_failures`,
  `backoff_until`, `last_429_at`. Mirrors cswap's `usage_store` row so a 429'd
  token isn't probed again until its backoff lifts (bounded by an hour-scale
  window), while still serving `last_good` from `usage_samples`.

## Credential management

Mirrors cswap's "login with the CLI, then import the live credential" flow —
this avoids reimplementing Google OAuth ourselves.

- **`tracker add claude`** — user runs `claude` for account N, then this
  command reads `~/.claude/.credentials.json`, stores a copy under a new slot,
  resolves identity via `GET /api/oauth/profile`, prompts for a `label`.
- **`tracker add grok`** — user runs `grok login --oauth` for account N, then
  this reads `~/.grok/auth.json`, stores it, reads `email`/`user_id`/`tier`
  from the blob, prompts for a `label`.
- **`tracker remove <label>`** — delete the credential file + DB row.
- **Refresh:** provider-specific, runs at refresh time when `expiresAt` is near
  the buffer window. Dead refresh tokens (HTTP 400/401/403 + `invalid_grant`
  in body) are quarantined with a `relogin-needed` sentinel and not retried
  until the user re-logins and re-runs `tracker add <provider>`.

## Usage collection

`tracker list` is the primary path. For each account it decides, per-account,
whether a network refresh is eligible this run:

1. If `fetch_state.backoff_until` is in the future → serve `last_good` from
   `usage_samples` with an "age / backing off" note. No network call.
2. Else if the account's newest `usage_samples` row is younger than the serve
   TTL (default ~5 min) → serve it as-is. No network call.
3. Else run the provider's collector:
   - **Claude:** refresh token if near expiry → `GET /api/oauth/usage` → parse
     windows → write a `usage_samples` row (`source='api'`). On 429, honor
     `Retry-After`, write `fetch_state` (`last_429_at`, `backoff_until`), record
     a `rate_limit_event`, and serve `last_good` (which stays a valid lower
     bound until its window `resets_at`).
   - **Grok:** parse newly-changed session transcripts since last collection →
     append `token_usage` rows; scan debug/session logs for rate-limit signals →
     append `rate_limit_events`. No live `/usage` call exists, so Grok rows
     render from the latest derived `usage_sample` (plus manual entries).
4. Every account renders a row regardless of which branch it took.

Separate from the per-account-by-default behavior above:

- **`tracker sync`** forces a network/parse pass for all (or `--label`) accounts
  regardless of TTL/backoff — the "I know it's recent, refresh anyway" path.
- **Manual fallback:** `tracker log <provider> <label> --msgs N --resets-in HhMm`
  writes a synthetic `usage_samples` row (`source='manual'`). Primary use is Grok
  (no live API), but the command works for either provider.

No background daemon in phase 1. A later `--watch` / TUI-live mode can add
adaptive polling (per-token AIMD backoff) like cswap's `poll_policy`.

## Commands (phase 1)

| Command | Effect |
|---|---|
| `tracker list` (default) | **Primary command.** Shows every account's current usage at once — Claude 5h/7d % + reset, Grok last-derived + throttle status — refreshing each account per-account only when stale (and honoring 429 backoff). Always renders a row per account. |
| `tracker add <provider>` | import live credential from the CLI's config dir; resolve identity; prompt for label |
| `tracker sync [--all\|<label>]` | force-refresh: network/parse pass regardless of TTL/backoff |
| `tracker list --refresh` | same as `tracker list` but force-refresh every account first (shortcut for `tracker sync` + `tracker list`) |
| `tracker tokens [--since] [--provider]` | historical token/cost report across providers (ccusage-style) |
| `tracker status` | one-line aggregate summary |
| `tracker remove <label>` | drop an account (credential file + DB row) |
| `tracker log <provider> <label> --msgs N --resets-in HhMm` | manual usage entry (Grok fallback, works for either) |

## What is deliberately NOT in phase 1

- **No auto-switch / credential mutation.** Read-only observability. Auto-switch
  is a later phase that reuses this credential store (`tracker switch` + a
  daemon); no data-layer rewrite needed.
- **No web dashboard.** Phase 2 mounts FastAPI on the same SQLite store.
- **No background polling daemon.** `tracker list` is on-demand, adpative per-account.
- **No reimplementing Google OAuth.** Login is delegated to the CLIs; importer.

## Risks / open questions

- **Grok transcript schema drift:** the grok CLI's session/transcript format
  is not a stable public contract. The parser must degrade gracefully
  (skip unparsable entries, log) rather than crash. Validate against the real
  files on this machine during implementation.
- **Grok OIDC refresh (implemented):** `POST {oidc_issuer}/oauth2/token` with
  `grant_type=refresh_token`, `client_id` from auth.json
  (`b1a00492-073a-47ea-816f-4c329264a828` for the Grok CLI). Form-urlencoded
  body; tokens rotate (`refresh_token` replaced). Tracker writes the new blob
  to its credential store and, when `user_id` matches, best-effort updates
  `~/.grok/auth.json` so the CLI stays in sync.
- **Usage-endpoint 429 budget:** Claude's usage endpoint throttles
  per-access-token non-first-party User-Agents. The refresh-if-stale model plus
  `fetch_state` backoff keeps probing within the budget; never blank `last_good`
  on a throttle until the window resets, and never let `Retry-After` park an
  account unbounded (cap at one rolling window).