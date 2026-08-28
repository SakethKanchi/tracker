"""Tests for the Z.ai / Zhipu GLM Coding Plan provider.

Two things here are easy to get wrong and impossible to notice by eye:

  1. The quota endpoint answers HTTP 200 for authentication failures, so the
     body's ``success`` flag is the only real status.
  2. Quota windows are identified by (``type``, ``unit``) — not by position —
     and legacy plans omit the weekly window entirely.

The payload below is the shape documented by Z.ai's own usage-query plugin.
Stdlib unittest only, to honor the project's "no new heavy deps" norm.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

from tracker.providers import apikeys, zai

_FIVE_HOUR_RESET_MS = 1771073738808
_WEEK_RESET_MS = 1744137600000

_PAYLOAD = {
    "code": 200,
    "msg": "Success",
    "success": True,
    "data": {
        "level": "pro",
        "limits": [
            {
                "type": "TIME_LIMIT", "unit": 5, "number": 1,
                "usage": 100, "currentValue": 28, "remaining": 72,
                "percentage": 28, "nextResetTime": 1772615765983,
                "usageDetails": [{"modelCode": "search-prime", "usage": 67}],
            },
            {
                "type": "TOKENS_LIMIT", "unit": 3, "number": 5,
                "percentage": 12, "nextResetTime": _FIVE_HOUR_RESET_MS,
            },
            {
                "type": "TOKENS_LIMIT", "unit": 6,
                "usage": 500000, "currentValue": 120000,
                "percentage": 24, "nextResetTime": _WEEK_RESET_MS,
            },
        ],
    },
}


@contextmanager
def _urlopen_returning(body: dict):
    """Patch urlopen so fetch_usage sees *body* with an HTTP 200."""
    payload = json.dumps(body).encode()

    @contextmanager
    def fake(_req, timeout=None):
        yield io.BytesIO(payload)

    with mock.patch.object(zai.urllib.request, "urlopen", fake):
        yield


class BuildWindowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.windows = zai.build_windows(_PAYLOAD["data"])

    def test_five_hour_token_window(self) -> None:
        win = self.windows["five_hour"]
        self.assertEqual(win["pct"], 12.0)
        expected = datetime.fromtimestamp(
            _FIVE_HOUR_RESET_MS / 1000, tz=timezone.utc
        ).isoformat()
        self.assertEqual(win["resets_at"], expected)

    def test_weekly_token_window_carries_totals(self) -> None:
        win = self.windows["seven_day"]
        self.assertEqual(win["pct"], 24.0)
        self.assertEqual((win["used"], win["limit"]), (120000, 500000))

    def test_mcp_window_is_scoped(self) -> None:
        self.assertEqual(
            self.windows["scoped"], [
                {
                    "pct": 28.0,
                    "resets_at": datetime.fromtimestamp(
                        1772615765983 / 1000, tz=timezone.utc
                    ).isoformat(),
                    "used": 28,
                    "limit": 100,
                    "name": "mcp",
                },
            ],
        )

    def test_plan_and_status(self) -> None:
        self.assertEqual(self.windows["plan"], "pro")
        self.assertEqual(self.windows["quota_status"], "active")
        self.assertEqual(self.windows["platform"], "zai")

    def test_legacy_plan_without_weekly_window(self) -> None:
        """Older plans omit unit=6; that must not fabricate a weekly window."""
        data = {"level": "lite", "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 3},
        ]}
        windows = zai.build_windows(data)
        self.assertIn("five_hour", windows)
        self.assertNotIn("seven_day", windows)
        self.assertNotIn("scoped", windows)

    def test_exhausted_window_blocks_the_account(self) -> None:
        """100% must read as blocked so the 'most headroom' hint skips it."""
        data = {"limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 100},
        ]}
        windows = zai.build_windows(data)
        self.assertEqual(windows["quota_status"], "blocked")

    def test_unknown_limit_type_is_kept_visible(self) -> None:
        data = {"limits": [
            {"type": "FUTURE_LIMIT", "unit": 6, "number": 2, "percentage": 40},
        ]}
        scoped = zai.build_windows(data)["scoped"]
        self.assertEqual([(s["name"], s["pct"]) for s in scoped], [("2wk", 40.0)])

    def test_junk_limits_are_skipped(self) -> None:
        data = {"limits": [{"type": "TOKENS_LIMIT", "unit": 3}, "nonsense", None]}
        windows = zai.build_windows(data)
        self.assertNotIn("five_hour", windows)
        self.assertNotIn("scoped", windows)


class FetchUsageTest(unittest.TestCase):
    def test_success_returns_windows(self) -> None:
        with _urlopen_returning(_PAYLOAD):
            result = zai.fetch_usage("k")
        self.assertIsNone(result.error)
        self.assertEqual(result.usage["five_hour"]["pct"], 12.0)

    def test_auth_failure_under_http_200(self) -> None:
        """HTTP 200 + success:false is the endpoint's way of rejecting a key."""
        body = {"code": 1000, "msg": "Authentication Failed", "success": False}
        with _urlopen_returning(body):
            result = zai.fetch_usage("k")
        self.assertIsNone(result.usage)
        # "invalid" is the marker the collector uses to flag a dead credential.
        self.assertIn("invalid", result.error)

    def test_non_auth_api_error_is_not_reported_as_invalid_key(self) -> None:
        body = {"code": 1261, "msg": "Rate limited", "success": False}
        with _urlopen_returning(body):
            result = zai.fetch_usage("k")
        self.assertNotIn("invalid", result.error)
        self.assertIn("1261", result.error)


class PlatformTest(unittest.TestCase):
    def test_base_url_maps_to_platform(self) -> None:
        self.assertEqual(
            zai.platform_for_base_url("https://api.z.ai/api/anthropic"), "zai"
        )
        self.assertEqual(
            zai.platform_for_base_url("https://open.bigmodel.cn/api/anthropic"), "zhipu"
        )
        self.assertIsNone(zai.platform_for_base_url("https://api.anthropic.com"))
        self.assertIsNone(zai.platform_for_base_url(None))

    def test_quota_url_per_platform(self) -> None:
        self.assertEqual(
            zai.quota_url("zhipu"),
            "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
        )
        self.assertEqual(
            zai.quota_url("nonsense"),
            "https://api.z.ai/api/monitor/usage/quota/limit",
        )


class KeyDetectionTest(unittest.TestCase):
    _KEY = "0f8c1b2a3d4e5f60718293a4b5c6d7e8.AbCdEfGhIjKlMnOp"

    def test_glm_key_shape_detects_as_zai(self) -> None:
        self.assertTrue(apikeys.looks_like_api_key(self._KEY))
        self.assertEqual(apikeys.detect_provider_from_prefix(self._KEY), "zai")

    def test_provider_names_are_not_keys(self) -> None:
        for name in ("zai", "z.ai", "zhipu", "glm", "codex"):
            self.assertFalse(apikeys.looks_like_api_key(name), name)

    def test_other_providers_still_win_their_prefixes(self) -> None:
        self.assertEqual(apikeys.detect_provider_from_prefix("sk-ant-abc"), "claude")
        self.assertEqual(apikeys.detect_provider_from_prefix("sk-proj-abc"), "openai")
        self.assertEqual(apikeys.detect_provider_from_prefix("xai-abc"), "grok")


if __name__ == "__main__":
    unittest.main()
