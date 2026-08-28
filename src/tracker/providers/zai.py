"""Z.ai / Zhipu GLM Coding Plan provider (subscription quota windows).

The GLM Coding Plan is a flat monthly subscription consumed through an
Anthropic-compatible endpoint, so there is no per-request bill worth following —
the useful signal is the quota windows. Z.ai exposes them on the same
monitoring endpoint Z.ai's own ``glm-plan-usage`` Claude Code plugin calls:

    GET {base}/api/monitor/usage/quota/limit
    Authorization: Bearer <api_key>

Two platforms serve an identical payload — ``api.z.ai`` (global) and
``open.bigmodel.cn`` (CN / Zhipu). The credential blob records which one a key
belongs to so a refresh never asks the wrong host.

The endpoint answers HTTP 200 even for authentication failures, so ``success``
in the body — not the status line — is the real result.

Response shape (only the fields used here):

    {"code": 200, "msg": "Success", "success": true,
     "data": {"level": "lite", "limits": [
       {"type": "TOKENS_LIMIT", "unit": 3, "number": 5,
        "percentage": 1, "nextResetTime": 1771073738808},
       {"type": "TOKENS_LIMIT", "unit": 6, "usage": 500000,
        "currentValue": 120000, "percentage": 24, "nextResetTime": ...},
       {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 100,
        "currentValue": 28, "percentage": 28, "nextResetTime": ...,
        "usageDetails": [{"modelCode": "search-prime", "usage": 67}]}]}}

``unit`` is a calendar unit and ``number`` its count, so unit=3/number=5 is the
5-hour rolling token window and unit=6 the weekly token window (newer plans
only; legacy plans omit it). ``TIME_LIMIT`` is the monthly MCP/tool-call quota.

Windows are emitted under the same keys Claude uses (``five_hour`` /
``seven_day`` / ``scoped``) so the dashboard, the "most headroom" hint, and the
Discord poller all read them without special cases.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("tracker")

USER_AGENT = "tracker/0.2"

DEFAULT_PLATFORM = "zai"
PLATFORMS = {
    "zai": "https://api.z.ai",
    "zhipu": "https://open.bigmodel.cn",
}

# Calendar units seen in the quota payload. Only documented values are mapped;
# anything else falls back to a generic label instead of a wrong one.
_UNIT_HOUR = 3
_UNIT_MONTH = 5
_UNIT_WEEK = 6
_UNIT_SUFFIX = {_UNIT_HOUR: "h", _UNIT_MONTH: "mo", _UNIT_WEEK: "wk"}


@dataclass
class UsageResult:
    usage: dict | None
    error: str | None
    retry_after: float | None


def quota_url(platform: str) -> str:
    base = PLATFORMS.get(platform, PLATFORMS[DEFAULT_PLATFORM])
    return f"{base}/api/monitor/usage/quota/limit"


def platform_for_base_url(base_url: str | None) -> str | None:
    """Map an ``ANTHROPIC_BASE_URL`` to a platform key, or None if unrelated."""
    url = (base_url or "").lower()
    if "z.ai" in url:
        return "zai"
    if "bigmodel.cn" in url:
        return "zhipu"
    return None


def fetch_usage(
    api_key: str, platform: str = DEFAULT_PLATFORM, timeout: float = 10.0
) -> UsageResult:
    """Fetch GLM Coding Plan quota windows for *api_key*."""
    req = urllib.request.Request(
        quota_url(platform),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Accept-Language": "en-US,en",
            # The API gzips when offered the chance; keep the body plain.
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        retry_after = _retry_after(e)
        if e.code in (401, 403):
            return UsageResult(None, "invalid z.ai API key", None)
        return UsageResult(None, f"http-{e.code}", retry_after)
    except TimeoutError:
        return UsageResult(None, "timeout", None)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return UsageResult(None, "timeout", None)
        return UsageResult(None, "network", None)
    except Exception as e:
        logger.debug("z.ai quota fetch failed: %r", e)
        return UsageResult(None, type(e).__name__, None)

    if not isinstance(data, dict) or not data.get("success"):
        return UsageResult(None, _api_error(data), None)
    payload = data.get("data")
    if not isinstance(payload, dict):
        return UsageResult(None, "malformed quota response", None)
    return UsageResult(build_windows(payload, platform), None, None)


def build_windows(payload: dict, platform: str = DEFAULT_PLATFORM) -> dict[str, Any]:
    """Translate a ``data`` payload into tracker's window dict."""
    windows: dict[str, Any] = {"quota_status": "active", "platform": platform}

    level = payload.get("level")
    if isinstance(level, str) and level:
        windows["plan"] = level

    scoped: list[dict[str, Any]] = []
    limits = payload.get("limits")
    for item in limits if isinstance(limits, list) else []:
        if not isinstance(item, dict):
            continue
        win = _window(item)
        if win is None:
            continue
        kind, unit = item.get("type"), item.get("unit")
        if kind == "TOKENS_LIMIT" and unit == _UNIT_HOUR:
            windows["five_hour"] = win
        elif kind == "TOKENS_LIMIT" and unit == _UNIT_WEEK:
            windows["seven_day"] = win
        elif kind == "TIME_LIMIT":
            win["name"] = "mcp"
            scoped.append(win)
        else:
            # An unrecognized window still costs quota — show it rather than
            # dropping it because this version has not learned its name yet.
            win["name"] = _generic_name(item)
            scoped.append(win)

    if scoped:
        windows["scoped"] = scoped

    pcts = [
        w["pct"]
        for w in (windows.get("five_hour"), windows.get("seven_day"), *scoped)
        if isinstance(w, dict) and isinstance(w.get("pct"), float)
    ]
    if pcts and max(pcts) >= 100:
        windows["quota_status"] = "blocked"
        windows["quota_reason"] = "quota exhausted"
    return windows


def _window(item: dict) -> dict[str, Any] | None:
    pct = item.get("percentage")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    win: dict[str, Any] = {"pct": float(pct)}
    resets_at = _iso_from_ms(item.get("nextResetTime"))
    if resets_at:
        win["resets_at"] = resets_at
    used, limit = item.get("currentValue"), item.get("usage")
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        win["used"] = int(used)
    if isinstance(limit, (int, float)) and not isinstance(limit, bool):
        win["limit"] = int(limit)
    return win


def _generic_name(item: dict) -> str:
    """Short label for a limit type this version does not model."""
    suffix = _UNIT_SUFFIX.get(item.get("unit"))
    number = item.get("number")
    if suffix and isinstance(number, int):
        return f"{number}{suffix}"
    return "other"


def _iso_from_ms(ms: object) -> str | None:
    """Millisecond epoch → ISO string, the format the renderers count down from."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# Zhipu/Z.ai reserve the 1000 block for authentication failures (missing key,
# bad token, revoked account). Anything else is a real API-side problem.
def _api_error(data: object) -> str:
    code = data.get("code") if isinstance(data, dict) else None
    msg = (data.get("msg") if isinstance(data, dict) else None) or "request rejected"
    if isinstance(code, int) and 1000 <= code < 1100:
        return f"invalid z.ai API key ({msg})"
    if code:
        return f"zai-{code}: {msg}"
    return str(msg)


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    raw = e.headers.get("Retry-After") if e.headers else None
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None
