"""tracker CLI — unified usage tracker for Claude + Grok accounts.

Usage:
  tracker                    # show all accounts' usage (primary command)
  tracker add <provider>     # import live credential from the CLI's config dir
  tracker list [--refresh]   # show all accounts (force-refresh with --refresh)
  tracker sync [--all|LABEL] # force-refresh usage
  tracker tokens [--since]   # historical token/cost report
  tracker status             # one-line aggregate
  tracker remove <label>     # drop an account
  tracker log <provider> <label> --msgs N --resets-in HhMm  # manual entry
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid

from . import credentials, store, tui, usage
from .providers import claude, grok


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


def cmd_add(args: argparse.Namespace) -> int:
    provider = args.provider
    if provider not in ("claude", "grok"):
        print(f"error: unknown provider '{provider}' (claude or grok)")
        return 1

    conn = store.connect()

    if provider == "claude":
        oauth = credentials.import_claude_credential()
        if not oauth:
            print("error: no claudeAiOauth found in ~/.claude/.credentials.json")
            print("  log in with `claude` first, then run: tracker add claude")
            return 1

        # Refresh if expired, then resolve identity
        result = None
        if claude.is_token_expired(oauth):
            print("  token expired, refreshing...")
            result = claude.refresh_token(oauth)
            if result.credentials:
                oauth = result.credentials
                # Persist refreshed credential back to the CLI's file (lightweight)
                import json
                from . import paths
                try:
                    with open(paths.CLAUDE_CREDENTIALS_PATH) as f:
                        full = json.load(f)
                    full["claudeAiOauth"] = oauth
                    with open(paths.CLAUDE_CREDENTIALS_PATH, "w") as f:
                        json.dump(full, f, indent=2)
                except Exception:
                    pass  # best-effort; we have our own copy
            elif result.error == "invalid_grant":
                print("  refresh token is dead — re-login with `claude`, then run: tracker add claude")
                return 1

        identity = claude.fetch_profile(oauth["accessToken"])
        if not identity and result and result.credentials:
            # Fall back to identity from the token-endpoint response
            identity = result.identity or {}

        email = identity.get("email") if identity else None
        acct_uuid = identity.get("uuid") if identity else str(uuid.uuid4())
        org_id = identity.get("organizationUuid") if identity else None

        default_label = email or acct_uuid[:8] or "claude-account"
        label = _prompt_label(default_label)

        account_id = str(uuid.uuid4())
        credentials.write_credential(account_id, oauth)
        store.add_account(
            conn,
            id=account_id,
            provider="claude",
            label=label,
            email=email,
            provider_account_id=acct_uuid,
            org_id=org_id,
            tier=None,
        )
        print(f"  added claude account: {label} ({email or acct_uuid})")

    elif provider == "grok":
        blob = credentials.import_grok_credential()
        if not blob:
            print("error: no grok credential found in ~/.grok/auth.json")
            print("  log in with `grok login --oauth` first, then run: tracker add grok")
            return 1

        identity = grok.extract_identity(blob)
        email = identity.get("email")
        user_id = identity.get("user_id")
        team_id = identity.get("team_id")
        tier = identity.get("tier")

        default_label = email or user_id[:8] or "grok-account"
        label = _prompt_label(default_label)

        account_id = str(uuid.uuid4())
        credentials.write_credential(account_id, blob)
        store.add_account(
            conn,
            id=account_id,
            provider="grok",
            label=label,
            email=email,
            provider_account_id=user_id,
            org_id=team_id,
            tier=tier,
        )
        print(f"  added grok account: {label} ({email})")

    # Immediately collect usage for the new account
    au = usage.collect_one(conn, label, force=True)
    if au and au.windows:
        print(f"  collected initial usage ({au.source})")
    return 0


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
    # Show the list after sync
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
        # Parse relative ("7d", "24h") or ISO date
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

    rows = store.token_usage_since(conn, account_id=None, since_ts=since_ts)
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

    import time
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Unified usage tracker for Claude + Grok accounts",
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="import a credential from the CLI's config dir")
    p_add.add_argument("provider", choices=["claude", "grok"])

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
    p_tokens.add_argument("--provider", choices=["claude", "grok"], help="filter by provider")

    # status
    sub.add_parser("status", help="one-line aggregate summary")

    # remove
    p_remove = sub.add_parser("remove", help="drop an account")
    p_remove.add_argument("label")

    # log
    p_log = sub.add_parser("log", help="manual usage entry (Grok fallback)")
    p_log.add_argument("provider", choices=["claude", "grok"])
    p_log.add_argument("label")
    p_log.add_argument("--msgs", type=int, help="message count")
    p_log.add_argument("--resets-in", help="reset duration, e.g. '1h30m'")
    p_log.add_argument("--tokens", type=int, help="total tokens used")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

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
    }
    handler = handlers.get(args.command)
    if not handler:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())