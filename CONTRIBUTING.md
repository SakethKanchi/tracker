# Contributing

Thanks for helping improve **tracker**. This project stays small on purpose:
read-only multi-account usage observability for Claude, Grok, Codex, Gemini,
OpenAI, and Z.ai.

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
- **Guard data correctness with a test.** Anything that changes stored or
  aggregated numbers needs a regression test. Bugs here are silent: a missing
  UNIQUE constraint once let every sync re-insert the full transcript history,
  which inflated reported lifetime cost by ~1,700x before anyone noticed.

## Running the tests

Stdlib `unittest`, no test dependencies:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests run against an isolated `XDG_DATA_HOME`, so they never touch your real
database at `~/.local/share/tracker/tracker.db`.

## Project layout

```
src/tracker/
  cli.py            # entrypoint
  usage.py          # collect_all / backoff
  providers/        # claude.py, grok.py, codex.py, gemini.py, zai.py, apikeys.py
  tui.py            # terminal rendering
  bot.py            # Discord webhook poller
  store.py          # SQLite
  credentials.py    # import/write 0600 blobs
docs/               # architecture + design notes
scripts/            # screenshot generator, helpers
```

## Making a change

1. Branch from `main`.
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
- Provider involved (`claude` / `grok` / `codex` / `gemini` / `openai` / `zai`)
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
   git tag v0.3.0          # must match pyproject version
   git push origin v0.3.0
   ```
4. The workflow builds, runs `twine check --strict`, installs the wheel into a
   clean venv and smoke-tests the CLI, verifies tag == version, then publishes.
   CI additionally runs `python -m unittest discover -s tests` on 3.11-3.13.

Versions on PyPI are **immutable and cannot be reused**, so let the workflow's
checks run instead of uploading by hand.

> A manual `workflow_dispatch` run of `publish.yml` is a **dry run**: it builds
> and verifies but never uploads. Only pushing a `v*` tag publishes.

### If the publish job fails with `invalid-publisher`

```
* `invalid-publisher`: valid token, but no corresponding publisher
  (Publisher with matching claims was not found)
```

This means the **pending publisher has not been created on PyPI yet** — having a
PyPI account is not sufficient on its own. Create it at
<https://pypi.org/manage/account/publishing/> using the table above.

Nothing is uploaded when this happens, so the version is **not** burned: delete
the tag and re-push it after finishing the setup.

```bash
git push --delete origin v0.3.0 && git tag -d v0.3.0
# ...register the publisher, then:
git tag v0.3.0 && git push origin v0.3.0
```

**API-token alternative.** If you prefer a token over trusted publishing, create
one at <https://pypi.org/manage/account/token/>, add it as the repo secret
`PYPI_API_TOKEN`, and give the publish step:

```yaml
        with:
          password: ${{ secrets.PYPI_API_TOKEN }}
```
