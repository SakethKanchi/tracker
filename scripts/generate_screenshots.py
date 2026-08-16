#!/usr/bin/env python3
"""Generate demo screenshots for the README (no real accounts required).

Usage (from repo root):
  PYTHONPATH=src python scripts/generate_screenshots.py
"""

from __future__ import annotations

import sys
import time
from io import StringIO
from pathlib import Path

# Allow running without install
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rich.console import Console  # noqa: E402

from tracker.tui import render_accounts, render_status  # noqa: E402
from tracker.usage import AccountUsage  # noqa: E402

OUT = ROOT / "docs" / "images"
NOW = time.time()


def demo_accounts() -> list[AccountUsage]:
    """Synthetic accounts with redacted identities — safe for public docs."""
    return [
        AccountUsage(
            account_id="demo-claude-1",
            provider="claude",
            label="work",
            email="you@example.com",
            tier=None,
            windows={
                "five_hour": {
                    "pct": 20.0,
                    "resets_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 22 * 60)
                    ),
                },
                "seven_day": {
                    "pct": 63.0,
                    "resets_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 3 * 86400 + 3600)
                    ),
                },
                "scoped": [
                    {
                        "name": "Sonnet",
                        "pct": 48.0,
                        "resets_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 3 * 86400)
                        ),
                    },
                ],
            },
            source="api",
            fetched_at=NOW - 12,
            error=None,
            needs_relogin=False,
        ),
        AccountUsage(
            account_id="demo-claude-2",
            provider="claude",
            label="personal",
            email="me@example.com",
            tier=None,
            windows={
                "five_hour": {
                    "pct": 91.0,
                    "resets_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 45 * 60)
                    ),
                },
                "seven_day": {
                    "pct": 88.0,
                    "resets_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 2 * 86400)
                    ),
                },
            },
            source="cached",
            fetched_at=NOW - 180,
            error=None,
            needs_relogin=False,
        ),
        AccountUsage(
            account_id="demo-grok-1",
            provider="grok",
            label="main",
            email="you@example.com",
            tier="5",
            windows={
                "credit_usage_pct": 5.0,
                "billing_period_end": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 6 * 86400 + 21 * 3600)
                ),
                "monthly_pct": 2.0,
                "monthly_period_end": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 28 * 86400)
                ),
                "quota_status": "active",
                "last_activity": "2026-08-01T12:00:00Z",
            },
            source="api",
            fetched_at=NOW - 40,
            error=None,
            needs_relogin=False,
        ),
        AccountUsage(
            account_id="demo-grok-2",
            provider="grok",
            label="spare",
            email="alt@example.com",
            tier="5",
            windows={
                "credit_usage_pct": 100.0,
                "billing_period_end": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 1 * 86400 + 17 * 3600)
                ),
                "monthly_pct": 0.0,
                "monthly_period_end": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 28 * 86400)
                ),
                "quota_status": "blocked",
                "quota_message": "out of credits",
                "last_activity": "2026-07-30T09:00:00Z",
            },
            source="cached",
            fetched_at=NOW - 90,
            error=None,
            needs_relogin=False,
        ),
        AccountUsage(
            account_id="demo-codex-1",
            provider="codex",
            label="chatgpt",
            email="you@example.com",
            tier="plus",
            windows={
                "primary": {
                    "pct": 34.0,
                    "window_minutes": 300,
                    "resets_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 2 * 3600 + 10 * 60)
                    ),
                },
                "secondary": {
                    "pct": 71.0,
                    "window_minutes": 60 * 24 * 7,
                    "resets_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 4 * 86400)
                    ),
                },
            },
            source="api",
            fetched_at=NOW - 25,
            error=None,
            needs_relogin=False,
        ),
        AccountUsage(
            account_id="demo-gemini-1",
            provider="gemini",
            label="aistudio",
            email=None,
            tier=None,
            windows={
                "quota_status": "active",
                "model_count": 47,
                "sample_models": ["gemini-2.5-pro", "gemini-2.5-flash"],
            },
            source="api",
            fetched_at=NOW - 60,
            error=None,
            needs_relogin=False,
        ),
        AccountUsage(
            account_id="demo-openai-1",
            provider="openai",
            label="platform",
            email=None,
            tier=None,
            windows={
                "quota_status": "blocked",
                "quota_message": "insufficient_quota",
            },
            source="api",
            fetched_at=NOW - 75,
            error=None,
            needs_relogin=False,
        ),
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    accounts = demo_accounts()

    import tracker.tui as tui

    old = tui.console
    try:
        # list dashboard — export text before SVG (save_svg clears the record)
        def _console() -> Console:
            # file=StringIO keeps stdout clean while still recording for export
            return Console(
                file=StringIO(),
                record=True,
                width=78,
                force_terminal=True,
                color_system="truecolor",
            )

        list_console = _console()
        tui.console = list_console
        render_accounts(accounts)
        (OUT / "list.txt").write_text(list_console.export_text() + "\n", encoding="utf-8")
        # Re-render for SVG (export_text with default clear=True emptied the buffer)
        list_console = _console()
        tui.console = list_console
        render_accounts(accounts)
        list_console.save_svg(str(OUT / "list.svg"), title="tracker list")

        status_console = _console()
        tui.console = status_console
        render_status(accounts)
        (OUT / "status.txt").write_text(status_console.export_text() + "\n", encoding="utf-8")
        status_console = _console()
        tui.console = status_console
        render_status(accounts)
        status_console.save_svg(str(OUT / "status.svg"), title="tracker status")
    finally:
        tui.console = old

    print(f"wrote {OUT / 'list.svg'}")
    print(f"wrote {OUT / 'status.svg'}")
    print(f"wrote {OUT / 'list.txt'}")
    print(f"wrote {OUT / 'status.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
