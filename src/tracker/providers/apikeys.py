"""API-key auto-detection and lightweight validation for all providers.

Prefix heuristics (checked in order):

  sk-ant-…   → claude
  xai-…      → grok
  AIza…      → gemini
  sk-…       → openai  (ChatGPT API platform key — not Codex OAuth)

When the prefix is ambiguous we probe a cheap models endpoint and pick the
first provider that accepts the key.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
logger = logging.getLogger("tracker")

USER_AGENT = "tracker/0.2"

# Ordered (prefix_regex, provider) — first match wins for prefix path
_PREFIX_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^sk-ant-", re.I), "claude"),
    (re.compile(r"^xai-", re.I), "grok"),
    (re.compile(r"^AIza", re.I), "gemini"),
    # Z.ai / Zhipu GLM keys are `<32 hex id>.<secret>` — no prefix, but the
    # shape is unmistakable and nothing else here contains a dot.
    (re.compile(r"^[0-9a-f]{32}\.[A-Za-z0-9]{8,}$", re.I), "zai"),
    # OpenAI project keys sk-proj-… and classic sk-…
    (re.compile(r"^sk-", re.I), "openai"),
]


@dataclass
class DetectResult:
    provider: str | None
    error: str | None
    identity: dict[str, Any] | None  # email / tier / fingerprint when known
    windows: dict[str, Any] | None   # initial health sample


def looks_like_api_key(value: str) -> bool:
    """True when *value* looks like a raw API key rather than a provider name."""
    v = value.strip()
    if not v or " " in v or len(v) < 16:
        return False
    if v.lower() in (
        "claude", "grok", "codex", "gemini", "openai", "chatgpt",
        "zai", "z.ai", "glm", "zhipu",
    ):
        return False
    for pat, _ in _PREFIX_RULES:
        if pat.search(v):
            return True
    # Long opaque tokens without spaces (fallback probe candidates)
    return bool(re.match(r"^[A-Za-z0-9_\-]{24,}$", v))


def detect_provider_from_prefix(api_key: str) -> str | None:
    for pat, provider in _PREFIX_RULES:
        if pat.search(api_key.strip()):
            return provider
    return None


def fingerprint(api_key: str) -> str:
    k = api_key.strip()
    if len(k) <= 12:
        return k[:4] + "…"
    return f"{k[:6]}…{k[-4:]}"


def detect_and_validate(api_key: str, timeout: float = 10.0) -> DetectResult:
    """Detect provider for *api_key* and validate it with a live call."""
    key = api_key.strip()
    if not key:
        return DetectResult(None, "empty key", None, None)

    guessed = detect_provider_from_prefix(key)
    candidates = [guessed] if guessed else ["claude", "grok", "gemini", "openai"]
    # Dedup while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    last_err: str | None = None
    for provider in ordered:
        ok, err, identity, windows = _validate(provider, key, timeout=timeout)
        if ok:
            return DetectResult(provider, None, identity, windows)
        last_err = err
        # Prefix was confident — don't fall through to other providers
        if guessed and provider == guessed:
            return DetectResult(provider, err or "validation failed", identity, windows)

    return DetectResult(None, last_err or "could not detect provider", None, None)


def _validate(
    provider: str, api_key: str, timeout: float
) -> tuple[bool, str | None, dict[str, Any] | None, dict[str, Any] | None]:
    if provider == "claude":
        return _validate_claude(api_key, timeout)
    if provider == "grok":
        return _validate_grok(api_key, timeout)
    if provider == "gemini":
        return _validate_gemini(api_key, timeout)
    if provider == "openai":
        return _validate_openai(api_key, timeout)
    if provider == "zai":
        return _validate_zai(api_key, timeout)
    return False, f"unknown provider {provider}", None, None


def _validate_claude(
    api_key: str, timeout: float
) -> tuple[bool, str | None, dict | None, dict | None]:
    # Deliberately incomplete body: valid keys return 400 (auth ok, schema bad);
    # invalid keys return 401. Avoids spending tokens on a real completion.
    url = "https://api.anthropic.com/v1/messages"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _ = resp.read()
        # Unexpected success on empty body — still means the key worked
        windows = {
            "quota_status": "active",
            "auth_type": "api_key",
            "note": "Claude API keys have no subscription % window; key is valid",
        }
        return True, None, {"fingerprint": fingerprint(api_key)}, windows
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "invalid Claude API key", None, None
        if e.code in (400, 404, 422):
            windows = {
                "quota_status": "active",
                "auth_type": "api_key",
                "note": "Claude API key accepted",
            }
            return True, None, {"fingerprint": fingerprint(api_key)}, windows
        if e.code == 429:
            windows = {
                "quota_status": "blocked",
                "quota_reason": "rate-limit",
                "auth_type": "api_key",
            }
            return True, None, {"fingerprint": fingerprint(api_key)}, windows
        return False, f"http-{e.code}", None, None
    except Exception as e:
        return False, str(e), None, None


def _validate_grok(
    api_key: str, timeout: float
) -> tuple[bool, str | None, dict | None, dict | None]:
    url = "https://api.x.ai/v1/models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        models = data.get("data") if isinstance(data, dict) else None
        count = len(models) if isinstance(models, list) else 0
        windows = {
            "quota_status": "active",
            "auth_type": "api_key",
            "model_count": count,
            "note": "Grok API key valid (no SuperGrok subscription windows)",
        }
        return True, None, {"fingerprint": fingerprint(api_key)}, windows
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "invalid Grok API key", None, None
        return False, f"http-{e.code}", None, None
    except Exception as e:
        return False, str(e), None, None


def _validate_gemini(
    api_key: str, timeout: float
) -> tuple[bool, str | None, dict | None, dict | None]:
    from . import gemini

    result = gemini.fetch_usage(api_key, timeout=timeout)
    if result.usage and result.usage.get("quota_status") == "active":
        return True, None, {"fingerprint": fingerprint(api_key)}, result.usage
    if result.error in ("token-expired", "http-401", "http-403"):
        return False, "invalid Gemini API key", None, result.usage
    if result.usage:
        # Key accepted enough to return structured status
        return True, None, {"fingerprint": fingerprint(api_key)}, result.usage
    return False, result.error or "validation failed", None, None


def _validate_openai(
    api_key: str, timeout: float
) -> tuple[bool, str | None, dict | None, dict | None]:
    url = "https://api.openai.com/v1/models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        models = data.get("data") if isinstance(data, dict) else None
        count = len(models) if isinstance(models, list) else 0
        windows = {
            "quota_status": "active",
            "auth_type": "api_key",
            "model_count": count,
            "note": "OpenAI API key valid (platform usage is billing, not Codex % windows)",
        }
        return True, None, {"fingerprint": fingerprint(api_key)}, windows
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "invalid OpenAI API key", None, None
        return False, f"http-{e.code}", None, None
    except Exception as e:
        return False, str(e), None, None


def _validate_zai(
    api_key: str, timeout: float
) -> tuple[bool, str | None, dict | None, dict | None]:
    """Validate a GLM Coding Plan key against both Z.ai platforms.

    A key belongs to exactly one platform (global or CN) and the other rejects
    it as invalid, so an auth failure on the default host is retried on the
    other before giving up. The winning platform is reported back so the
    credential remembers where the key lives.
    """
    from . import zai

    last_err: str | None = None
    for platform in (zai.DEFAULT_PLATFORM, "zhipu"):
        result = zai.fetch_usage(api_key, platform=platform, timeout=timeout)
        if result.usage:
            identity = {
                "fingerprint": fingerprint(api_key),
                "platform": platform,
                "tier": result.usage.get("plan"),
            }
            return True, None, identity, result.usage
        last_err = result.error
        if last_err and "invalid" not in last_err:
            break  # network / server-side problem: the other host won't help
    return False, last_err or "validation failed", None, None


def make_api_key_blob(provider: str, api_key: str) -> dict[str, Any]:
    """Credential blob stored under ~/.config/tracker/credentials/<id>.json."""
    return {
        "auth_type": "api_key",
        "provider": provider,
        "api_key": api_key.strip(),
    }


def is_api_key_blob(blob: dict) -> bool:
    return blob.get("auth_type") == "api_key" and isinstance(blob.get("api_key"), str)


def api_key_of(blob: dict) -> str | None:
    k = blob.get("api_key")
    return k if isinstance(k, str) and k else None
