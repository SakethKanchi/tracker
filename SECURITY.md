# Security Policy

## What this tool handles

`tracker` stores **OAuth/OIDC tokens** for your Claude and Grok accounts on
disk so it can query usage APIs. Treat the machine it runs on as trusted.

| Path | Sensitivity |
|------|-------------|
| `~/.config/tracker/credentials/*.json` | **High** — live refresh tokens |
| `~/.config/tracker/webhook.json` | **High** — Discord webhook URL |
| `~/.local/share/tracker/tracker.db` | Medium — usage history + labels/emails |

Files are created with mode `0600` (credentials dir `0700`). Do not copy these
paths into git, backups you share, or CI logs.

## What tracker does *not* do

- No cloud sync of credentials
- No network calls except to Anthropic / xAI / Discord (webhook optional)
- No auto-switch of the active CLI account
- No master password / keychain abstraction (OS user account is the boundary)

## Reporting a vulnerability

If you find a security issue (token leakage, unsafe file permissions, SSRF via
webhook URL handling, etc.):

1. **Do not** open a public GitHub issue.
2. Email the maintainer (see GitHub profile) or open a private security
   advisory on the repository.
3. Include steps to reproduce and impact. We will acknowledge and work on a fix
   before any public disclosure.

## Hardening tips

- Run only on a personal machine with a locked-down user account.
- Prefer `chmod 700 ~/.config/tracker` if you share the host.
- Rotate Discord webhooks if the URL may have leaked.
- Re-login + `tracker add <provider>` after any suspected token compromise;
  then remove old labels with `tracker remove <label>`.
