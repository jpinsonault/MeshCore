"""
MeshCore Collector — Configuration persistence.

Stores user preferences (selected serial port, baud rate, etc.) in a JSON
file so the TUI can remember choices across sessions.
"""

import json
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "meshcore-collector"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"

DEFAULTS = {
    "port": None,
    "baud": 115200,
    "db_path": "collector.db",
    "channels": [],
}


def load_config(path=None):
    """Load config from disk, merging with defaults."""
    path = Path(path) if path else DEFAULT_CONFIG_FILE
    config = dict(DEFAULTS)
    if path.exists():
        try:
            with open(path) as f:
                saved = json.load(f)
            config.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def load_channels(config):
    """Parse channel entries from config into Channel objects.

    Config format:
        {"channels": [{"name": "MeshCore Public", "psk": "base64..."}]}

    Returns list of Channel objects. Silently skips invalid entries.
    """
    from .crypto import Channel

    channels = []
    entries = config.get("channels") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        psk = entry.get("psk", "")
        if name and psk:
            try:
                channels.append(Channel.from_psk(name, psk))
            except (ValueError, Exception):
                pass
    return channels


def save_config(config, path=None):
    """Persist config to disk."""
    path = Path(path) if path else DEFAULT_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
