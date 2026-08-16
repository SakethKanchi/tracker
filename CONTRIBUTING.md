# Contributing

Thanks for helping improve **tracker**. This project stays small on purpose:
read-only multi-account usage observability for Claude, Grok, Codex, Gemini, and OpenAI.

## Setup

```bash
git clone https://github.com/SakethKanchi/tracker.git
cd tracker
uv sync          # or: pip install -e .
PYTHONPATH=src python -m tracker.cli --help
```

Requires **Python 3.11+**. Optional: real Claude / Grok CLI logins if you want
to exercise live collectors.

## Development norms

- **Keep it lazy.** Prefer stdlib + `httpx` + `rich`. No new heavy deps without
  a clear win.
- **Read-only by default.** Do not mutate active CLI credentials except the
  existing Grok token write-back (required so OIDC rotation does not force
  re-login).
- **Always render a row.** Collection failures, 429 backoff, and cache hits
  must still produce an `AccountUsage` line.
- **No secrets in the repo.** Credentials, webhook URLs, and local DBs are
  gitignored. Demo screenshots use synthetic data only
  (`scripts/generate_screenshots.py`).

## Project layout

```
src/tracker/
  cli.py            # entrypoint
  usage.py          # collect_all / backoff
  providers/        # claude.py, grok.py, codex.py, gemini.py, apikeys.py
  tui.py            # terminal rendering
  bot.py            # Discord webhook poller
  store.py          # SQLite
  credentials.py    # import/write 0600 blobs
docs/               # architecture + design notes
scripts/            # screenshot generator, helpers
```

## Making a change

1. Branch from `master` (or `main` once renamed).
2. Fix or feature in the smallest diff that works.
3. Manually smoke-test:
   ```bash
   tracker list
   tracker list --refresh
   tracker status
   tracker --help
   ```
4. If you change TUI layout, regenerate screenshots:
   ```bash
   PYTHONPATH=src python scripts/generate_screenshots.py
   ```
5. Open a PR with **why** in the description (what problem, not only what code).

## Bug reports

Include:

- OS + Python version
- Command you ran
- Provider involved (`claude` / `grok` / `codex` / `gemini` / `openai`)
- Sanitized output (redact emails, tokens, webhook URLs)

## Code of conduct

Be kind. No harassment, no spam, no dumping credentials into issues.

## Releasing to PyPI

The distribution is **`ai-quota-tracker`** (both `tracker` and
`ai-usage-tracker` were already taken on PyPI); the console command and import
package stay `tracker`.

Publishing is automated by `.github/workflows/publish.yml` using **PyPI Trusted
Publishing (OIDC)** — no API token is stored in the repo.

**One-time setup** at <https://pypi.org/manage/account/publishing/> → "Add a new
pending publisher":

| Field | Value |
|-------|-------|
| PyPI Project Name | `ai-quota-tracker` |
| Owner | `SakethKanchi` |
| Repository name | `tracker` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

**Each release:**

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/tracker/__init__.py` — the workflow fails if the tag does not match.
2. Add a `CHANGELOG.md` entry.
3. Commit, then tag and push:
   ```bash
   git tag v0.2.1
   git push origin v0.2.1
   ```
4. The workflow builds, runs `twine check --strict`, installs the wheel into a
   clean venv and smoke-tests the CLI, verifies tag == version, then publishes.

Versions on PyPI are **immutable and cannot be reused**, so let the workflow's
checks run instead of uploading by hand.
