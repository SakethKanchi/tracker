# Architecture

`tracker` is a local observability tool for multi-account Claude, Grok, Codex,
Gemini, and OpenAI usage. It does **not** auto-switch which account a CLI uses.
It **does** write rotated OAuth tokens back to the provider CLI file when it
refreshes (required for Grok and Codex single-use refresh tokens).

## High-level flow

```
┌─────────────┐     import      ┌──────────────────────────┐
│ Claude CLI  │ ──────────────► │ ~/.config/tracker/       │
│ Grok CLI    │   credentials   │   credentials/<id>.json  │
│ Codex CLI   │   or API keys   │                          │
│ API keys    │                 └────────────┬─────────────┘
└─────────────┘                              │
                                             ▼
┌─────────────┐   collect_all   ┌──────────────────────────┐
│ tracker list│ ──────────────► │ providers/claude.py      │
│ tracker sync│                 │ providers/grok.py        │
└──────┬──────┘                 │ providers/codex.py       │
       │                        │ providers/gemini.py      │
       │                        │ providers/apikeys.py     │
       │                        └────────────┬─────────────┘
       │                                     ▼
       │                        ┌──────────────────────────┐
       │                        │ ~/.local/share/tracker/  │
       │                        │   tracker.db (SQLite)    │
       │                        └────────────┬─────────────┘
       ▼                                     │
┌─────────────┐                              │
│ rich TUI    │ ◄────────────────────────────┘
│ Discord     │   last-known windows always render
└─────────────┘
```

## Modules

| Module | Role |
|--------|------|
| `cli.py` | argparse entrypoint (`add`, `list`, `sync`, `tokens`, `status`, `remove`, `log`, `webhook`) |
| `store.py` | SQLite schema + CRUD + provider CHECK migration |
| `credentials.py` | import/write/delete per-account credential blobs (mode `0600`) |
| `usage.py` | refresh-if-stale collection, 429 backoff, `AccountUsage` rows |
| `providers/claude.py` | OAuth refresh, profile, live usage API |
| `providers/grok.py` | OIDC refresh, billing windows, session transcript parse |
| `providers/codex.py` | ChatGPT OAuth refresh, WHAM `/wham/usage` windows |
| `providers/gemini.py` | AI Studio API-key health check |
| `providers/apikeys.py` | Prefix detection + validation for all API-key providers |
| `tui.py` | compact rich progress-bar tree for the terminal |
| `bot.py` | Discord webhook poller (POST once, PATCH on each cycle) |
| `paths.py` | XDG config/data paths |

## Freshness model

For each account on `tracker list`:

1. If `fetch_state.backoff_until` is in the future → serve last-known sample
   (tag: `backing-off`). No network call.
2. Else if newest sample is younger than the serve TTL (~5 min) → serve cache
   (tag: `cached`).
3. Else run the provider collector and write a new `usage_samples` row.

`--refresh` / `tracker sync` forces a network pass regardless of TTL (still
respects hard auth failures).

Every account always renders a row — fresh, cached, or last-known during
backoff — so the dashboard never drops accounts that happen to be rate-limited.

## Data stores

| Path | Contents | Mode |
|------|----------|------|
| `~/.config/tracker/credentials/<uuid>.json` | OAuth/OIDC blobs | `0600` |
| `~/.local/share/tracker/tracker.db` | accounts, samples, tokens, backoff | `0600` |
| `~/.config/tracker/webhook.json` | Discord webhook URL + interval | `0600` |
| `~/.local/share/tracker/webhook_message_id` | last Discord message id | — |

Credentials are **never** stored inside the git checkout. Import copies from
the provider CLI, or stores a pasted API key:

- Claude: `~/.claude/.credentials.json` → `claudeAiOauth`
- Grok: `~/.grok/auth.json` → OIDC entry
- Codex: `~/.codex/auth.json` → ChatGPT tokens blob
- API keys: `{auth_type: "api_key", provider, api_key}`

## Provider details

### Claude

- Live quota: `GET https://api.anthropic.com/api/oauth/usage`
- Windows: 5-hour, 7-day, optional per-model scoped windows, optional spend
- Token refresh: Claude OAuth refresh endpoint (same client id pattern as
  [cswap](https://github.com/realiti4/claude-swap))
- On HTTP 429: honor `Retry-After`, cap backoff at 1 hour, keep last-good
- API keys (`sk-ant-…`): health check only (no subscription % windows)

### Grok

- Weekly/monthly credit bars from cli-chat-proxy billing endpoints
- Blocked/active signal via `api.x.ai/v1/models`
- Historical tokens from local `~/.grok/sessions` transcripts
- OIDC refresh writes rotated tokens back to `~/.grok/auth.json` when the
  `user_id` matches (avoids CLI re-login loops)
- API keys (`xai-…`): models-list health check

### Codex

- Live quota: `GET https://chatgpt.com/backend-api/wham/usage`
- Headers: `Authorization: Bearer <access_token>`, optional `ChatGPT-Account-Id`
- Windows: `primary_window` / `secondary_window` (`used_percent`, `reset_at`)
- Refresh: `POST https://auth.openai.com/oauth/token` with client id
  `app_EMoamEEZ73f0CkXaXp7hrann`
- **Refresh tokens are single-use** (`refresh_token_reused` if two clients race).
  After every successful refresh we write the new tokens to both the tracker
  credential store and `~/.codex/auth.json`, and we adopt a fresher live file
  when the CLI has already rotated.

### Gemini

- API keys only (`AIza…` from Google AI Studio)
- Validate via `GET https://generativelanguage.googleapis.com/v1beta/models`
- No public per-key % usage window; dashboard shows key health + sample models

### OpenAI (platform API key)

- API keys only (`sk-…` / `sk-proj-…`)
- Validate via `GET https://api.openai.com/v1/models`
- Distinct from Codex ChatGPT-subscription windows

### API key auto-detect

`tracker add <key>` / `tracker --add <key>`:

| Prefix | Provider |
|--------|----------|
| `sk-ant-` | claude |
| `xai-` | grok |
| `AIza` | gemini |
| `sk-` (else) | openai |

Ambiguous keys are probed against each provider's cheap endpoint until one
accepts the credential.

## Discord webhook

`tracker webhook` posts one embed and then **edits** it on a schedule
(default 5 minutes). No bot token, no gateway, no `discord.py` — only
webhook REST. Suitable for a systemd user unit.

See [discord-webhook.md](discord-webhook.md).

## Non-goals (phase 1)

- Auto-switch / mutating which account the CLI uses
- Web dashboard (phase 2 idea: FastAPI on the same SQLite store)
- Background daemon beyond the optional webhook poller
- Reimplementing browser OAuth (login stays with the provider CLIs)
- Google Cloud Console spend dashboards for Gemini (needs GCP OAuth)

For the original design rationale see
[superpowers/specs/2026-07-28-tracker-design.md](superpowers/specs/2026-07-28-tracker-design.md).
