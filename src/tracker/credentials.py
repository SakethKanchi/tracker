"""Per-account credential storage: import from CLI config dirs, read/write 0600 JSON.

Credential files live at ~/.config/tracker/credentials/<account-id>.json (0600).
Each is the raw JSON blob the provider's CLI writes, plus a small wrapper.

Claude source: ~/.claude/.credentials.json → claudeAiOauth{accessToken,refreshToken,expiresAt,...}
Grok source:   ~/.grok/auth.json → {access_token,refresh_token,expires_at,email,user_id,tier,...}
Codex source:  ~/.codex/auth.json → {auth_mode, tokens:{access_token,refresh_token,...}, ...}
API keys:      {auth_type: "api_key", provider, api_key}
Z.ai source:   $Z_AI_API_KEY, or $ANTHROPIC_AUTH_TOKEN + a z.ai/bigmodel base URL
               (env or ~/.claude/settings*.json), since z.ai ships no CLI of its own
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


def delete_credential(account_id: str) -> str | None:
    """Delete a per-account credential file; return an error string on failure.

    Returns None when the file is gone (deleted now, or never there). A caller
    removing an account must not report success while the secret survives on
    disk, so an undeletable file is reported rather than swallowed.
    """
    path = cred_path(account_id)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"{path}: {e.strerror or e}"
    return None


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Atomic replace of a JSON file at *path* with mode 0600."""
    payload = json.dumps(data, indent=2).encode()
    tmp_path = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CRED_PERMS)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp_path, CRED_PERMS)
    except OSError:
        pass
    os.replace(tmp_path, path)


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

def _load_grok_auth_file(source_path: str | None = None) -> dict[str, Any] | None:
    """Load ~/.grok/auth.json as a dict, or None."""
    path = source_path or str(paths.GROK_AUTH_PATH)
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def import_grok_credential(source_path: str | None = None) -> dict[str, Any] | None:
    """Read the live ~/.grok/auth.json.

    The file is a dict keyed by ``oidc_issuer::client_id`` → credential object.
    Returns the first credential entry, or None.
    """
    data = _load_grok_auth_file(source_path)
    if not data:
        return None
    # auth.json is keyed by "issuer::client_id"
    for _key, entry in data.items():
        if isinstance(entry, dict) and entry.get("key"):
            return entry
    # Maybe it's already a flat dict (key = access token)
    if data.get("key"):
        return data
    return None


def import_grok_credential_for_user(
    user_id: str | None, source_path: str | None = None
) -> dict[str, Any] | None:
    """Return the live auth.json entry for ``user_id``, or None if not logged in."""
    if not user_id:
        return None
    data = _load_grok_auth_file(source_path)
    if not data:
        return None
    for _key, entry in data.items():
        if isinstance(entry, dict) and entry.get("user_id") == user_id and entry.get("key"):
            return entry
    if data.get("user_id") == user_id and data.get("key"):
        return data
    return None


_GROK_TOKEN_FIELDS = ("key", "refresh_token", "expires_at", "access_token")


def write_back_grok_auth(blob: dict[str, Any], source_path: str | None = None) -> bool:
    """Update ~/.grok/auth.json when it holds the same user_id (token rotation).

    Keeps the Grok CLI in sync after we refresh a matching account. xAI rotates
    the refresh_token on every grant — if we refresh into the tracker store but
    leave auth.json on the previous grant, the next CLI call forces a browser
    re-login. Returns True if the live file was updated.
    """
    user_id = blob.get("user_id")
    if not user_id:
        return False
    path = source_path or str(paths.GROK_AUTH_PATH)
    data = _load_grok_auth_file(path)
    if not data:
        return False

    updated = False
    for key, entry in list(data.items()):
        if isinstance(entry, dict) and entry.get("user_id") == user_id:
            merged = dict(entry)
            for field in _GROK_TOKEN_FIELDS:
                if field in blob and blob[field] is not None:
                    merged[field] = blob[field]
            data[key] = merged
            updated = True

    if not updated and data.get("user_id") == user_id:
        for field in _GROK_TOKEN_FIELDS:
            if field in blob and blob[field] is not None:
                data[field] = blob[field]
        updated = True

    if not updated:
        return False

    _atomic_write_json(path, data)
    return True


# ── Codex ──

def import_codex_credential(source_path: str | None = None) -> dict[str, Any] | None:
    """Read the live ~/.codex/auth.json (ChatGPT OAuth mode).

    Returns the full auth.json blob when it contains usable ChatGPT tokens,
    or an API-key-only auth when OPENAI_API_KEY is set. Returns None when the
    file is missing or empty of credentials.
    """
    path = source_path or str(paths.CODEX_AUTH_PATH)
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    tokens = data.get("tokens")
    if isinstance(tokens, dict) and tokens.get("access_token"):
        return data

    # API-key mode in auth.json
    api_key = data.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key:
        return {
            "auth_type": "api_key",
            "provider": "openai",
            "api_key": api_key,
            "source": "codex_auth_json",
        }
    return None


def write_back_codex_auth(blob: dict[str, Any], source_path: str | None = None) -> bool:
    """Update ~/.codex/auth.json after a successful token refresh.

    Codex refresh tokens are **single-use**. If we refresh into the tracker
    store but leave auth.json on the previous grant, the next Codex CLI call
    hits ``refresh_token_reused`` and forces a browser re-login.

    We only write when the live file exists and belongs to the same account
    (matching account_id / access_token family). Returns True if updated.
    """
    if blob.get("auth_type") == "api_key":
        return False
    tokens = blob.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return False

    path = source_path or str(paths.CODEX_AUTH_PATH)
    try:
        with open(path) as f:
            live = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if not isinstance(live, dict):
        return False

    live_tokens = live.get("tokens") if isinstance(live.get("tokens"), dict) else {}
    # Match by account_id when both have it; otherwise always push if live has tokens
    blob_aid = tokens.get("account_id")
    live_aid = live_tokens.get("account_id") if isinstance(live_tokens, dict) else None
    if blob_aid and live_aid and blob_aid != live_aid:
        return False

    # Merge: keep non-token fields from live, overwrite tokens + last_refresh
    merged = dict(live)
    merged["tokens"] = dict(tokens)
    if blob.get("last_refresh"):
        merged["last_refresh"] = blob["last_refresh"]
    if blob.get("auth_mode"):
        merged["auth_mode"] = blob["auth_mode"]
    # Preserve OPENAI_API_KEY field shape if present
    if "OPENAI_API_KEY" in live and "OPENAI_API_KEY" not in merged:
        merged["OPENAI_API_KEY"] = live["OPENAI_API_KEY"]

    _atomic_write_json(path, merged)
    return True


# ── Z.ai / GLM Coding Plan ──

# Env vars the z.ai ecosystem uses for a Coding Plan key, most specific first.
_ZAI_KEY_ENVS = (
    "Z_AI_API_KEY",
    "ZAI_API_KEY",
    "ZHIPUAI_API_KEY",
    "ZHIPU_API_KEY",
    "GLM_API_KEY",
)


def import_zai_credential() -> dict[str, Any] | None:
    """Find a GLM Coding Plan key in the environment or Claude Code settings.

    Z.ai has no CLI that stores credentials of its own: a Coding Plan key is
    wired into other agents as ``ANTHROPIC_AUTH_TOKEN`` plus a z.ai
    ``ANTHROPIC_BASE_URL``, so that pair is the closest thing to a live
    credential. A bare ``ANTHROPIC_AUTH_TOKEN`` is only trusted when the base
    URL confirms it points at z.ai — otherwise it is somebody else's token.

    Returns ``{api_key, platform, source}`` or None.
    """
    from .providers import zai

    env_platform = zai.platform_for_base_url(os.environ.get("ANTHROPIC_BASE_URL"))
    for name in _ZAI_KEY_ENVS:
        key = (os.environ.get(name) or "").strip()
        if key:
            return {
                "api_key": key,
                "platform": env_platform or zai.DEFAULT_PLATFORM,
                "source": f"${name}",
            }

    token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if token and env_platform:
        return {
            "api_key": token,
            "platform": env_platform,
            "source": "$ANTHROPIC_AUTH_TOKEN",
        }

    for path in paths.CLAUDE_SETTINGS_PATHS:
        found = _zai_from_claude_settings(str(path))
        if found:
            return found
    return None


def _zai_from_claude_settings(path: str) -> dict[str, Any] | None:
    """Pull a z.ai key out of a Claude Code settings file's ``env`` block."""
    from .providers import zai

    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return None

    platform = zai.platform_for_base_url(env.get("ANTHROPIC_BASE_URL"))
    for name in _ZAI_KEY_ENVS:
        key = str(env.get(name) or "").strip()
        if key:
            return {
                "api_key": key,
                "platform": platform or zai.DEFAULT_PLATFORM,
                "source": path,
            }
    token = str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if token and platform:
        return {"api_key": token, "platform": platform, "source": path}
    return None
