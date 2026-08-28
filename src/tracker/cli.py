"""tracker CLI — unified usage tracker for Claude, Grok, Codex, Gemini, OpenAI, Z.ai.

Usage:
  tracker                         # show all accounts' usage (primary command)
  tracker add <provider>          # import live credential from the CLI's config dir
  tracker add <api_key>           # auto-detect provider from key prefix and add
  tracker --add <api_key>         # same as above (flag form)
  tracker list [--refresh]        # show all accounts (force-refresh with --refresh)
  tracker sync [--label SEL]      # force-refresh usage
  tracker tokens [--since]        # historical token/cost report
  tracker status                  # one-line aggregate
  tracker remove <provider|label> # drop an account
  tracker log <provider> <label> --msgs N --resets-in HhMm  # manual entry

Commands that name an account take a *selector*: a label, a provider name, or
`provider:label`. Labels are not unique (one email often has both a Grok and a
Codex account), so an ambiguous selector is reported instead of guessed.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import time
import uuid

from . import credentials, store, tui, usage
from .providers import apikeys, claude, codex, grok

CLI_PROVIDERS = ("claude", "grok", "codex", "zai")
ALL_PROVIDERS = store.KNOWN_PROVIDERS


def _parse_duration(s: str) -> float:
    """Parse '1h30m', '45m', '2h' → seconds."""
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?$", s.strip().lower())
    if not m:
        raise ValueError(f"unparseable duration: {s}")
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    return h * 3600 + mi * 60


def _prompt_label(default: str) -> str:
    """Prompt for an account label with a default."""
    try:
        val = input(f"  label [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return val or default


def _add_or_update_account(
    conn,
    *,
    provider: str,
    blob: dict,
    provider_account_id: str | None,
    email: str | None,
    org_id: str | None,
    tier: str | None,
    default_label: str,
    update_msg: str | None = None,
) -> str:
    """Insert or update an account; returns the account id."""
    existing = None
    if provider_account_id:
        existing = store.find_by_provider_account_id(conn, provider, provider_account_id)

    if existing:
        credentials.write_credential(existing["id"], blob)
        store.upsert_fetch_state(
            conn, account_id=existing["id"],
            consecutive_failures=0, backoff_until=None, last_error=None,
        )
        # Refresh identity fields when we have better info
        conn.execute(
            """UPDATE accounts SET
                 email=COALESCE(?, email),
                 org_id=COALESCE(?, org_id),
                 tier=COALESCE(?, tier)
               WHERE id=?""",
            (email, org_id, tier, existing["id"]),
        )
        print(update_msg or f"  updated {provider} credentials: {existing['label']}")
        return existing["id"]

    label = _prompt_label(default_label)
    account_id = str(uuid.uuid4())
    credentials.write_credential(account_id, blob)
    store.add_account(
        conn,
        id=account_id,
        provider=provider,
        label=label,
        email=email,
        provider_account_id=provider_account_id,
        org_id=org_id,
        tier=tier,
    )
    print(f"  added {provider} account: {label}" + (f" ({email})" if email else ""))
    return account_id


def _add_claude(conn) -> int:
    oauth = credentials.import_claude_credential()
    if not oauth:
        print("error: no claudeAiOauth found in ~/.claude/.credentials.json")
        print("  log in with `claude` first, then run: tracker add claude")
        print("  or paste an API key: tracker add sk-ant-...")
        return 1

    result = None
    if claude.is_token_expired(oauth):
        print("  token expired, refreshing...")
        result = claude.refresh_token(oauth)
        if result.credentials:
            oauth = result.credentials
            import json
            from . import paths
            try:
                with open(paths.CLAUDE_CREDENTIALS_PATH) as f:
                    full = json.load(f)
                full["claudeAiOauth"] = oauth
                with open(paths.CLAUDE_CREDENTIALS_PATH, "w") as f:
                    json.dump(full, f, indent=2)
            except Exception:
                pass
        elif result.error == "invalid_grant":
            print("  refresh token is dead — re-login with `claude`, then run: tracker add claude")
            return 1

    identity = claude.fetch_profile(oauth["accessToken"])
    if not identity and result and result.credentials:
        identity = result.identity or {}

    email = identity.get("email") if identity else None
    acct_uuid = identity.get("uuid") if identity else str(uuid.uuid4())
    org_id = identity.get("organizationUuid") if identity else None

    account_id = _add_or_update_account(
        conn,
        provider="claude",
        blob=oauth,
        provider_account_id=acct_uuid,
        email=email,
        org_id=org_id,
        tier=None,
        default_label=email or (acct_uuid[:8] if acct_uuid else "claude-account"),
    )
    return _collect_initial(conn, account_id)


def _add_grok(conn) -> int:
    blob = credentials.import_grok_credential()
    if not blob:
        print("error: no grok credential found in ~/.grok/auth.json")
        print("  log in with `grok login --oauth` first, then run: tracker add grok")
        print("  or paste an API key: tracker add xai-...")
        return 1

    if grok.is_token_expired(blob):
        print("  token expired, refreshing...")
        result = grok.refresh_token(blob)
        if result.credentials:
            blob = result.credentials
            credentials.write_back_grok_auth(blob)
        elif result.error in ("invalid_grant", "no_refresh_token"):
            print("  refresh token is dead — re-login with `grok login --oauth`, then run: tracker add grok")
            return 1

    identity = grok.extract_identity(blob)
    email = identity.get("email")
    user_id = identity.get("user_id")
    team_id = identity.get("team_id")
    tier = identity.get("tier")

    account_id = _add_or_update_account(
        conn,
        provider="grok",
        blob=blob,
        provider_account_id=user_id,
        email=email,
        org_id=team_id,
        tier=tier,
        default_label=email or (user_id[:8] if user_id else "grok-account"),
    )
    return _collect_initial(conn, account_id)


def _add_codex(conn) -> int:
    blob = credentials.import_codex_credential()
    if not blob:
        print("error: no codex credential found in ~/.codex/auth.json")
        print("  log in with `codex` (ChatGPT sign-in) first, then run: tracker add codex")
        print("  or paste an OpenAI API key: tracker add sk-...")
        return 1

    # API-key-only auth.json → route through API-key path
    if apikeys.is_api_key_blob(blob):
        return _add_api_key(conn, blob["api_key"])

    if codex.is_token_expired(blob):
        print("  token expired, refreshing...")
        result = codex.refresh_token(blob)
        if result.credentials:
            blob = result.credentials
            # Critical: write rotated single-use refresh token back to CLI file
            credentials.write_back_codex_auth(blob)
        elif result.error in ("invalid_grant", "no_refresh_token"):
            print("  refresh token is dead — re-login with `codex`, then run: tracker add codex")
            return 1

    identity = codex.extract_identity(blob)
    email = identity.get("email")
    account_id = identity.get("account_id")
    user_id = identity.get("user_id")
    tier = identity.get("tier")
    # Prefer account_id for dedup (workspace), fall back to user_id
    provider_account_id = account_id or user_id

    account_id = _add_or_update_account(
        conn,
        provider="codex",
        blob=blob,
        provider_account_id=provider_account_id,
        email=email,
        org_id=account_id,
        tier=tier,
        default_label=email or (provider_account_id[:8] if provider_account_id else "codex-account"),
    )
    return _collect_initial(conn, account_id)


def _add_zai(conn) -> int:
    """Add a Z.ai / Zhipu GLM Coding Plan subscription."""
    found = credentials.import_zai_credential()
    if found:
        print(f"  found GLM Coding Plan key in {found['source']}")
        return _add_api_key(conn, found["api_key"], platform=found["platform"])

    print("  no z.ai key in the environment or ~/.claude/settings.json")
    key = ""
    # getpass without a terminal warns and echoes the secret — don't ask.
    if sys.stdin.isatty():
        try:
            key = getpass.getpass("  paste your z.ai API key (hidden): ").strip()
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            print()
            key = ""
    if not key:
        print("error: no z.ai key provided")
        print("  create one at https://z.ai/manage-apikey/apikey-list, then either")
        print("    export Z_AI_API_KEY=...  &&  tracker add zai")
        print("    tracker add <key>")
        return 1
    return _add_api_key(conn, key)


def _add_api_key(conn, api_key: str, platform: str | None = None) -> int:
    """Auto-detect provider from API key format, validate, and store."""
    print("  detecting provider from API key...")
    result = apikeys.detect_and_validate(api_key)
    if not result.provider:
        print(f"error: could not detect provider: {result.error or 'unknown'}")
        print("  supported prefixes:")
        print("    sk-ant-...        → Claude (Anthropic)")
        print("    xai-...           → Grok (xAI)")
        print("    AIza...           → Gemini (Google AI Studio)")
        print("    sk-...            → OpenAI (platform API key)")
        print("    <32hex>.<secret>  → Z.ai / Zhipu (GLM Coding Plan)")
        return 1

    provider = result.provider
    if result.error:
        print(f"error: {provider} key rejected: {result.error}")
        return 1

    print(f"  detected: {provider}")
    fp = apikeys.fingerprint(api_key)
    blob = apikeys.make_api_key_blob(provider, api_key)
    identity = result.identity or {}
    if provider == "zai":
        # Remember which host the key authenticated against so refreshes and
        # CN-platform keys never hit the wrong endpoint.
        blob["platform"] = identity.get("platform") or platform or "zai"

    account_id = _add_or_update_account(
        conn,
        provider=provider,
        blob=blob,
        # Dedup by fingerprint — API keys carry no account identity.
        provider_account_id=f"apikey:{fp}",
        email=identity.get("email"),
        org_id=None,
        tier=identity.get("tier") or "api_key",
        default_label=f"{provider}-{fp[-4:]}",
    )

    # Store the initial health sample if we already have windows
    if result.windows:
        store.insert_usage_sample(
            conn, account_id=account_id, source="api", windows=result.windows
        )
        print(f"  key valid ({result.windows.get('quota_status', 'ok')})")
        return 0
    return _collect_initial(conn, account_id)


def _collect_initial(conn, account_id: str) -> int:
    account = store.get_account(conn, account_id)
    if not account:
        return 0
    au = usage.collect_account(conn, account, force=True)
    if au.windows:
        print(f"  collected initial usage ({au.source})")
    elif au.error:
        print(f"  warning: initial collect: {au.error}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    target = (args.target or "").strip()
    if not target:
        print("error: provide a provider name or API key")
        print(f"  tracker add {'|'.join(CLI_PROVIDERS)}")
        print("  tracker add <api_key>")
        return 1

    conn = store.connect()

    # API key path (prefix / shape detection)
    if apikeys.looks_like_api_key(target):
        return _add_api_key(conn, target)

    provider = target.lower()
    # Aliases
    if provider in ("chatgpt", "openai-codex"):
        provider = "codex"
    if provider in ("z.ai", "z-ai", "glm", "zhipu", "bigmodel"):
        provider = "zai"
    if provider == "openai":
        print("error: 'openai' has no CLI import — paste an API key: tracker add sk-...")
        return 1
    if provider == "gemini":
        print("error: 'gemini' has no CLI OAuth import — paste an API key: tracker add AIza...")
        return 1

    if provider == "claude":
        return _add_claude(conn)
    if provider == "grok":
        return _add_grok(conn)
    if provider == "codex":
        return _add_codex(conn)
    if provider == "zai":
        return _add_zai(conn)

    print(f"error: unknown provider '{target}'")
    print(f"  CLI import: {', '.join(CLI_PROVIDERS)}")
    print("  or paste an API key (auto-detects claude/grok/gemini/openai/zai)")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    conn = store.connect()
    if getattr(args, "watch", None):
        return _watch_loop(conn, interval=args.watch, force_first=args.refresh)
    results = usage.collect_all(conn, force=args.refresh)
    tui.render_accounts(results)
    return 0


def _watch_loop(conn, interval: int, force_first: bool = False) -> int:
    """Re-render the dashboard in place until interrupted.

    Collection still honors the normal refresh-if-stale rules and 429 backoff,
    so a 2-second redraw does not mean a 2-second poll of the provider APIs:
    most frames are served from cache. Ctrl-C exits cleanly.
    """
    from rich.live import Live

    interval = max(1, int(interval))

    # Live redraw needs a terminal. When piped or redirected, fall back to a
    # single render so `tracker list --watch > file` is not silently empty.
    if not tui.console.is_terminal:
        tui.render_accounts(usage.collect_all(conn, force=force_first))
        return 0

    first = True
    try:
        with Live(
            tui.build_accounts_renderable(usage.collect_all(conn, force=force_first)),
            console=tui.console,
            screen=False,
            auto_refresh=False,
            transient=False,
        ) as live:
            while True:
                if not first:
                    results = usage.collect_all(conn, force=False)
                    live.update(tui.build_accounts_renderable(results), refresh=True)
                else:
                    live.refresh()
                first = False
                time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


def _describe(account) -> str:
    """`provider:label` — the selector that unambiguously names this account."""
    return f"{account['provider']}:{account['label']}"


def _resolve_accounts(conn, selector: str) -> list:
    """Accounts named by *selector*, or [] after printing what went wrong."""
    matches = store.find_accounts(conn, selector)
    if matches:
        return matches
    print(f"  error: no account matches '{selector}'")
    known = store.list_accounts(conn)
    if not known:
        print(f"  no accounts yet — run: tracker add {'|'.join(CLI_PROVIDERS)}")
        return []
    print("  known accounts (use a provider, a label, or provider:label):")
    for row in known:
        print(f"    {_describe(row)}")
    return []


def cmd_sync(args: argparse.Namespace) -> int:
    conn = store.connect()
    if args.label:
        matches = _resolve_accounts(conn, args.label)
        if not matches:
            return 1
        for account in matches:
            au = usage.collect_account(conn, account, force=True)
            print(f"  synced {_describe(account)}: {au.error or au.source}")
    else:
        results = usage.collect_all(conn, force=True)
        for au in results:
            status = au.error or au.source
            print(f"  {au.provider} {au.label}: {status}")
    print()
    results = usage.collect_all(conn, force=False)
    tui.render_accounts(results)
    return 0


def cmd_tokens(args: argparse.Namespace) -> int:
    conn = store.connect()
    since_ts = 0
    if args.since:
        import time as _time
        from datetime import datetime
        m = re.match(r"(\d+)([dh])$", args.since)
        if m:
            secs = int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)
            since_ts = _time.time() - secs
        else:
            try:
                since_ts = datetime.fromisoformat(args.since).timestamp()
            except ValueError:
                print(f"error: unparseable --since: {args.since}")
                return 1

    rows = store.token_usage_since(
        conn, account_id=None, since_ts=since_ts, provider=args.provider,
    )
    tui.render_tokens(rows, since=args.since)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = store.connect()
    results = usage.collect_all(conn, force=False)
    tui.render_status(results)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    conn = store.connect()
    matches = _resolve_accounts(conn, args.selector)
    if not matches:
        return 1
    if len(matches) > 1 and not args.all:
        print(f"  error: '{args.selector}' matches {len(matches)} accounts:")
        for row in matches:
            print(f"    {_describe(row)}")
        print("  name one of those, or pass --all to remove every match")
        return 1

    for account in matches:
        credentials.delete_credential(account["id"])
        store.remove_account(conn, account["id"])
        print(f"  removed {account['provider']} account: {account['label']}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    conn = store.connect()
    matches = store.find_accounts(conn, f"{args.provider}:{args.label}")
    if not matches:
        print(f"  error: no {args.provider} account with label '{args.label}'")
        return 1
    if len(matches) > 1:
        print(f"  error: {len(matches)} {args.provider} accounts share that label")
        return 1
    account = matches[0]

    windows: dict = {"source": "manual"}
    if args.msgs is not None:
        windows["manual_msgs"] = args.msgs
    if args.resets_in:
        try:
            windows["manual_resets_in_s"] = _parse_duration(args.resets_in)
        except ValueError as e:
            print(f"  error: {e}")
            return 1
    if args.tokens is not None:
        windows["manual_tokens"] = args.tokens

    store.insert_usage_sample(conn, account_id=account["id"], source="manual", windows=windows)
    print(f"  logged manual usage for {_describe(account)}")
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    """Run the Discord webhook poller (blocking)."""
    from . import bot
    return bot.run_webhook(once=args.once)


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Unified usage tracker for Claude, Grok, Codex, Gemini, OpenAI, and Z.ai",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"tracker {__version__}",
    )
    parser.add_argument(
        "--add",
        dest="add_key",
        metavar="API_KEY",
        help="auto-detect provider from API key and add it (same as: tracker add <key>)",
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser(
        "add",
        help=f"import a CLI credential ({'|'.join(CLI_PROVIDERS)}) or paste an API key",
    )
    p_add.add_argument(
        "target",
        help=f"provider name ({'|'.join(CLI_PROVIDERS)}) or an API key (auto-detected)",
    )

    # list (primary)
    p_list = sub.add_parser("list", help="show all accounts' usage (primary command)")
    p_list.add_argument("--refresh", action="store_true", help="force-refresh every account first")
    p_list.add_argument(
        "--watch",
        nargs="?",
        type=int,
        const=5,
        default=None,
        metavar="SECONDS",
        help="live-refresh the dashboard every SECONDS (default 5); Ctrl-C to exit",
    )

    # sync
    p_sync = sub.add_parser("sync", help="force-refresh usage")
    p_sync.add_argument(
        "--label",
        metavar="SELECTOR",
        help="sync one account: a label, a provider name, or provider:label",
    )
    p_sync.add_argument("--all", action="store_true", help="sync all accounts (default)")

    # tokens
    p_tokens = sub.add_parser("tokens", help="historical token/cost report")
    p_tokens.add_argument("--since", help="time filter: '7d', '24h', or ISO date")
    p_tokens.add_argument(
        "--provider",
        choices=list(ALL_PROVIDERS),
        help="filter by provider",
    )

    # status
    sub.add_parser("status", help="one-line aggregate summary")

    # remove
    p_remove = sub.add_parser("remove", help="drop an account")
    p_remove.add_argument(
        "selector",
        metavar="PROVIDER|LABEL",
        help="account to drop: a label, a provider name, or provider:label",
    )
    p_remove.add_argument(
        "--all",
        action="store_true",
        help="remove every account the selector matches",
    )

    # log
    p_log = sub.add_parser("log", help="manual usage entry")
    p_log.add_argument("provider", choices=list(ALL_PROVIDERS))
    p_log.add_argument("label")
    p_log.add_argument("--msgs", type=int, help="message count")
    p_log.add_argument("--resets-in", help="reset duration, e.g. '1h30m'")
    p_log.add_argument("--tokens", type=int, help="total tokens used")

    # webhook
    p_webhook = sub.add_parser("webhook", help="run the Discord webhook poller")
    p_webhook.add_argument("--once", action="store_true", help="post/update once and exit")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Top-level --add KEY shortcut
    if getattr(args, "add_key", None):
        ns = argparse.Namespace(target=args.add_key)
        return cmd_add(ns)

    # Default to `list` when no subcommand is given
    if not args.command:
        args = parser.parse_args(["list"])

    handlers = {
        "add": cmd_add,
        "list": cmd_list,
        "sync": cmd_sync,
        "tokens": cmd_tokens,
        "status": cmd_status,
        "remove": cmd_remove,
        "log": cmd_log,
        "webhook": cmd_webhook,
    }
    handler = handlers.get(args.command)
    if not handler:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
