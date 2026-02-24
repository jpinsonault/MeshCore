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


def save_config(config, path=None):
    """Persist config to disk."""
    path = Path(path) if path else DEFAULT_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
