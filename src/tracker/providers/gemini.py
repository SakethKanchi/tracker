"""Gemini (Google AI Studio) API-key provider.

Gemini subscription usage windows are not exposed via the public Generative
Language API for API keys. What we can do:

  1. Validate a key via ``GET /v1beta/models``
  2. Surface key health (active / invalid / rate-limited)
  3. Count how many models the key can list (cheap signal)

API key format: ``AIza...`` (39+ chars, Google API key prefix).

Auth header: ``x-goog-api-key: <key>``  (or ``?key=`` query param).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("tracker")

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
USER_AGENT = "tracker/0.2"


@dataclass
class UsageResult:
    usage: dict | None
    error: str | None
    retry_after: float | None


def fetch_usage(api_key: str, timeout: float = 10.0) -> UsageResult:
    """Validate a Gemini API key and return a health/status window."""
    from urllib.parse import quote

    url = f"{MODELS_URL}?pageSize=5&key={quote(api_key, safe='')}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        retry_after = None
        raw = e.headers.get("Retry-After") if e.headers else None
        if raw:
            try:
                retry_after = max(0.0, float(raw.strip()))
            except ValueError:
                pass
        if e.code in (400, 401, 403):
            # Invalid / restricted key
            msg = _extract_message(body) or "invalid API key"
            return UsageResult(
                {"quota_status": "error", "quota_reason": "auth", "quota_message": msg},
                "token-expired" if e.code == 401 else f"http-{e.code}",
                retry_after,
            )
        if e.code == 429:
            return UsageResult(
                {
                    "quota_status": "blocked",
                    "quota_reason": "rate-limit",
                    "quota_message": "rate limited",
                },
                "http-429",
                retry_after,
            )
        return UsageResult(None, f"http-{e.code}", retry_after)
    except TimeoutError:
        return UsageResult(None, "timeout", None)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return UsageResult(None, "timeout", None)
        return UsageResult(None, "network", None)
    except Exception as e:
        logger.debug("Gemini models list failed: %r", e)
        return UsageResult(None, type(e).__name__, None)

    models = data.get("models") if isinstance(data, dict) else None
    count = len(models) if isinstance(models, list) else 0
    sample = []
    if isinstance(models, list):
        for m in models[:3]:
            if isinstance(m, dict) and m.get("name"):
                # "models/gemini-2.5-flash" → "gemini-2.5-flash"
                name = str(m["name"]).removeprefix("models/")
                sample.append(name)

    windows: dict[str, Any] = {
        "quota_status": "active",
        "auth_type": "api_key",
        "model_count": count,
        "sample_models": sample,
        "note": "Gemini API keys have no live % usage window; status is key health only",
    }
    return UsageResult(windows, None, None)


def _extract_message(body: str) -> str | None:
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:200] if body else None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        return str(msg)[:200] if msg else None
    if isinstance(err, str):
        return err[:200]
    return None


def key_fingerprint(api_key: str) -> str:
    """Short non-secret label for an API key (prefix + last 4)."""
    if len(api_key) <= 12:
        return api_key[:4] + "…"
    return f"{api_key[:6]}…{api_key[-4:]}"
