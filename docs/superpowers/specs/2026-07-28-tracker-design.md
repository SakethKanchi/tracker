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

One local tool that imports every account's credentials, fetches/derives
each account's usage in a single pass, and presents all of them in one
dashboard — so "which account has quota left?" is a single command, not a
login/logout ritual.

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
     punch in a eyeballed `--msgs`/`--resets-in` value; stored as a synthetic
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
    usage.py          # provider-agnostic "collect one account" dispatch
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
- **Refresh:** provider-specific, runs at `sync` time when `expiresAt` is near
  the buffer window. Dead refresh tokens (HTTP 400/401/403 + `invalid_grant`
  in body) are quarantined with a `relogin-needed` sentinel and not retried
  until the user re-logins and re-runs `tracker add <provider>`.

## Usage collection (`tracker sync`)

For each account (or one via `--label`):
1. Refresh token if near expiry.
2. **Claude:** `GET /api/oauth/usage` → parse windows → write a
   `usage_samples` row (`source='api'`). On 429, honor `Retry-After`, record
   the event, keep last_good until window reset.
3. **Grok:** parse freshly-changed session transcripts since last sync →
   append `token_usage` rows; scan debug/session logs for rate-limit signals →
   append `rate_limit_events`. No live `/usage` call exists.
4. Manual fallback: `tracker log grok <label> --msgs 23 --resets-in 1h`
   writes a synthetic `usage_samples` row (`source='manual'`).

Poll cadence is on-demand (`tracker sync`) — no background daemon in phase 1.
A later `--watch`/TUI-live mode can add adaptive polling (per-token backoff)
like cswap's `poll_policy`.

## Commands (phase 1)

| Command | Effect |
|---|---|
| `tracker add <provider>` | import live credential from the CLI's config dir |
| `tracker list` / bare `tracker` | TUI dashboard: every account, 5h/7d% + reset (Claude), last tokens + throttle status (Grok) |
| `tracker sync [--all\|<label>]` | refresh tokens + fetch usage / parse transcripts |
| `tracker tokens [--since] [--provider]` | historical token/cost report across providers |
| `tracker status` | one-line aggregate |
| `tracker remove <label>` | drop an account |
| `tracker log <provider> <label> --msgs N --resets-in HhMm` | manual usage entry (Grok fallback) |

## What is deliberately NOT in phase 1

- **No auto-switch / credential mutation.** Read-only observability. Auto-switch
  is a later phase that reuses this credential store (`tracker switch` + a
  daemon); no data-layer rewrite needed.
- **No web dashboard.** Phase 2 mounts FastAPI on the same SQLite store.
- **No background polling daemon.** `tracker sync` is on-demand.
- **No reimplementing Google OAuth.** Login is delegated to the CLIs; importer.

## Risks / open questions

- **Grok transcript schema drift:** the grok CLI's session/transcript format
  is not a stable public contract. The parser must degrade gracefully
  (skip unparsable entries, log) rather than crash. Validate against the real
  files on this machine during implementation.
- **Grok refresh-token endpoint** (`auth.x.ai` OIDC): the exact refresh URL and
  client-id for `grok login --oauth` accounts need confirming against the
  client's own code/config before implementation (the `auth.json` carries
  `oidc_issuer` and `oidc_client_id` — use those).
- **Usage-endpoint 429 budget:** Claude's usage endpoint throttles
  per-access-token non-first-party User-Agents. Keep `sync` cadence modest,
  honor `Retry-After`, and never let a throttle blank last_good until reset.