"""tracker CLI — unified usage tracker for Claude, Grok, Codex, Gemini, OpenAI.

Usage:
  tracker                         # show all accounts' usage (primary command)
  tracker add <provider>          # import live credential from the CLI's config dir
  tracker add <api_key>           # auto-detect provider from key prefix and add
  tracker --add <api_key>         # same as above (flag form)
  tracker list [--refresh]        # show all accounts (force-refresh with --refresh)
  tracker sync [--all|LABEL]      # force-refresh usage
  tracker tokens [--since]        # historical token/cost report
  tracker status                  # one-line aggregate
  tracker remove <label>          # drop an account
  tracker log <provider> <label> --msgs N --resets-in HhMm  # manual entry
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid

from . import credentials, store, tui, usage
from .providers import apikeys, claude, codex, grok

CLI_PROVIDERS = ("claude", "grok", "codex")
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
    """Insert or update an account; returns the label used."""
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
        label = existing["label"]
        print(update_msg or f"  updated {provider} credentials: {label}")
        return label

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
    return label


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

    label = _add_or_update_account(
        conn,
        provider="claude",
        blob=oauth,
        provider_account_id=acct_uuid,
        email=email,
        org_id=org_id,
        tier=None,
        default_label=email or (acct_uuid[:8] if acct_uuid else "claude-account"),
    )
    return _collect_initial(conn, label)


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

    label = _add_or_update_account(
        conn,
        provider="grok",
        blob=blob,
        provider_account_id=user_id,
        email=email,
        org_id=team_id,
        tier=tier,
        default_label=email or (user_id[:8] if user_id else "grok-account"),
    )
    return _collect_initial(conn, label)


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

    label = _add_or_update_account(
        conn,
        provider="codex",
        blob=blob,
        provider_account_id=provider_account_id,
        email=email,
        org_id=account_id,
        tier=tier,
        default_label=email or (provider_account_id[:8] if provider_account_id else "codex-account"),
    )
    return _collect_initial(conn, label)


def _add_api_key(conn, api_key: str) -> int:
    """Auto-detect provider from API key format, validate, and store."""
    print("  detecting provider from API key...")
    result = apikeys.detect_and_validate(api_key)
    if not result.provider:
        print(f"error: could not detect provider: {result.error or 'unknown'}")
        print("  supported prefixes:")
        print("    sk-ant-...  → Claude (Anthropic)")
        print("    xai-...     → Grok (xAI)")
        print("    AIza...     → Gemini (Google AI Studio)")
        print("    sk-...      → OpenAI (platform API key)")
        return 1

    provider = result.provider
    if result.error:
        print(f"error: {provider} key rejected: {result.error}")
        return 1

    print(f"  detected: {provider}")
    fp = apikeys.fingerprint(api_key)
    blob = apikeys.make_api_key_blob(provider, api_key)

    # Dedup by fingerprint as provider_account_id
    provider_account_id = f"apikey:{fp}"
    email = None
    if result.identity:
        email = result.identity.get("email")

    label = _add_or_update_account(
        conn,
        provider=provider,
        blob=blob,
        provider_account_id=provider_account_id,
        email=email,
        org_id=None,
        tier="api_key",
        default_label=f"{provider}-{fp[-4:]}",
    )

    # Store the initial health sample if we already have windows
    if result.windows:
        account = store.get_account_by_label(conn, label)
        if account:
            store.insert_usage_sample(
                conn, account_id=account["id"], source="api", windows=result.windows
            )
            print(f"  key valid ({result.windows.get('quota_status', 'ok')})")
    else:
        return _collect_initial(conn, label)
    return 0


def _collect_initial(conn, label: str) -> int:
    au = usage.collect_one(conn, label, force=True)
    if au and au.windows:
        print(f"  collected initial usage ({au.source})")
    elif au and au.error:
        print(f"  warning: initial collect: {au.error}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    target = (args.target or "").strip()
    if not target:
        print("error: provide a provider name or API key")
        print("  tracker add claude|grok|codex")
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

    print(f"error: unknown provider '{target}'")
    print(f"  CLI import: {', '.join(CLI_PROVIDERS)}")
    print("  or paste an API key (auto-detects claude/grok/gemini/openai)")
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    conn = store.connect()
    results = usage.collect_all(conn, force=args.refresh)
    tui.render_accounts(results)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    conn = store.connect()
    if args.label:
        au = usage.collect_one(conn, args.label, force=True)
        if au:
            print(f"  synced {au.label}: {au.source}")
        else:
            print(f"  error: no account with label '{args.label}'")
            return 1
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
    account = store.get_account_by_label(conn, args.label)
    if not account:
        print(f"  error: no account with label '{args.label}'")
        return 1
    credentials.delete_credential(account["id"])
    store.remove_account(conn, account["id"])
    print(f"  removed {account['provider']} account: {args.label}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    conn = store.connect()
    account = store.get_account_by_label(conn, args.label)
    if not account:
        print(f"  error: no account with label '{args.label}'")
        return 1
    if account["provider"] != args.provider:
        print(f"  error: account '{args.label}' is a {account['provider']} account, not {args.provider}")
        return 1

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
    print(f"  logged manual usage for {args.label}")
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    """Run the Discord webhook poller (blocking)."""
    from . import bot
    return bot.run_webhook(once=args.once)


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Unified usage tracker for Claude, Grok, Codex, Gemini, and OpenAI",
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
        help="import a CLI credential (claude|grok|codex) or paste an API key",
    )
    p_add.add_argument(
        "target",
        help="provider name (claude|grok|codex) or an API key (auto-detected)",
    )

    # list (primary)
    p_list = sub.add_parser("list", help="show all accounts' usage (primary command)")
    p_list.add_argument("--refresh", action="store_true", help="force-refresh every account first")

    # sync
    p_sync = sub.add_parser("sync", help="force-refresh usage")
    p_sync.add_argument("--label", help="sync a single account by label")
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
    p_remove.add_argument("label")

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
