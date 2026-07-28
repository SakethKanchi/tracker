"""Per-account credential storage: import from CLI config dirs, read/write 0600 JSON.

Credential files live at ~/.config/tracker/credentials/<account-id>.json (0600).
Each is the raw JSON blob the provider's CLI writes, plus a small wrapper.

Claude source: ~/.claude/.credentials.json → claudeAiOauth{accessToken,refreshToken,expiresAt,...}
Grok source:   ~/.grok/auth.json → {access_token,refresh_token,expires_at,email,user_id,tier,...}
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import paths

CRED_PERMS = 0o600


def cred_path(account_id: str) -> str:
    return str(paths.credentials_dir() / f"{account_id}.json")


def write_credential(account_id: str, blob: dict[str, Any]) -> None:
    """Write a credential blob to the per-account file (0600)."""
    path = cred_path(account_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CRED_PERMS)
    try:
        os.write(fd, json.dumps(blob, indent=2).encode())
    finally:
        os.close(fd)
    os.chmod(path, CRED_PERMS)


def read_credential(account_id: str) -> dict[str, Any] | None:
    """Read a per-account credential blob, or None if missing/unreadable."""
    path = cred_path(account_id)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def delete_credential(account_id: str) -> None:
    try:
        os.unlink(cred_path(account_id))
    except FileNotFoundError:
        pass


# ── Claude ──

def import_claude_credential(source_path: str | None = None) -> dict[str, Any] | None:
    """Read the live ~/.claude/.credentials.json, extract claudeAiOauth.

    Returns None if the file is missing or has no claudeAiOauth (e.g. logged
    in via API key, not OAuth). The caller resolves identity via the profile
    endpoint and stores the full credential under a tracker account slot.
    """
    path = source_path or str(paths.CLAUDE_CREDENTIALS_PATH)
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    return oauth


# ── Grok ──

def import_grok_credential(source_path: str | None = None) -> dict[str, Any] | None:
    """Read the live ~/.grok/auth.json.

    The file is a dict keyed by ``oidc_issuer::client_id`` → credential object.
    Returns the first credential entry, or None.
    """
    path = source_path or str(paths.GROK_AUTH_PATH)
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    # auth.json is keyed by "issuer::client_id"
    for _key, entry in data.items():
        if isinstance(entry, dict) and entry.get("key"):
            return entry
    # Maybe it's already a flat dict (key = access token)
    if isinstance(data, dict) and data.get("key"):
        return data
    return None