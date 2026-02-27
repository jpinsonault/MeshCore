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
    "sender_name": "collector",
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
        {"channels": [
            {"name": "#catlovers"},
            {"name": "Private", "psk": "base64..."}
        ]}

    Hashtag channels (name starts with '#', no psk) auto-derive their key
    from the channel name via SHA-256, matching firmware behaviour.

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
        if not name:
            continue
        if name.startswith("#") and not psk:
            try:
                channels.append(Channel.from_hashtag(name))
            except (ValueError, Exception):
                pass
        elif psk:
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


def add_channel_to_config(name, psk=None, path=None):
    """Add a channel entry to config. For hashtag channels, psk is omitted.

    Returns True if added, False if already exists.
    """
    config = load_config(path)
    channels = config.get("channels", [])
    for ch in channels:
        if ch.get("name") == name:
            return False
    entry = {"name": name}
    if psk:
        entry["psk"] = psk
    channels.append(entry)
    config["channels"] = channels
    save_config(config, path)
    return True


def remove_channel_from_config(name, path=None):
    """Remove a channel entry from config by name."""
    config = load_config(path)
    channels = config.get("channels", [])
    config["channels"] = [ch for ch in channels if ch.get("name") != name]
    save_config(config, path)


def set_sender_name(name, path=None):
    """Persist the sender display name in config."""
    config = load_config(path)
    config["sender_name"] = name
    save_config(config, path)
