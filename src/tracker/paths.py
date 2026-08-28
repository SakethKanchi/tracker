"""XDG-compliant path resolution for tracker data and credentials."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(env: str, default: str) -> Path:
    return Path(os.environ.get(env, str(Path.home() / default)))


def config_dir() -> Path:
    """~/.config/tracker — config.toml lives here."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "tracker"


def data_dir() -> Path:
    """~/.local/share/tracker — tracker.db lives here."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "tracker"


def credentials_dir() -> Path:
    """~/.config/tracker/credentials/<account-id>.json — per-account, 0600."""
    return config_dir() / "credentials"


def db_path() -> Path:
    return data_dir() / "tracker.db"


def ensure_dirs() -> None:
    """Create all base directories with restrictive perms."""
    data_dir().mkdir(parents=True, exist_ok=True)
    credentials_dir().mkdir(parents=True, exist_ok=True)
    # Credential dir should not be group/other readable
    credentials_dir().chmod(0o700)


# External CLI credential sources
CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
GROK_AUTH_PATH = Path.home() / ".grok" / "auth.json"
GROK_SESSIONS_DIR = Path.home() / ".grok" / "sessions"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"

# Claude Code settings files — where a z.ai Coding Plan key is usually wired in
# as env.ANTHROPIC_AUTH_TOKEN + env.ANTHROPIC_BASE_URL.
CLAUDE_SETTINGS_PATHS = (
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
)