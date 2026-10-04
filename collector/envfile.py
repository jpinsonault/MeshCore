"""
Load KEY=VALUE settings from a .env file into os.environ (existing variables win).

Looks in the current directory, then the repo root. Used for MESHCORE_HOST / MESHCORE_PASSWORD,
so device credentials live in one gitignored file instead of on command lines.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def find_env_file():
    for d in (Path.cwd(), REPO_ROOT):
        p = d / ".env"
        if p.is_file():
            return p
    return None


def load_env():
    """Merge the .env file into os.environ without overriding variables already set. Returns its path."""
    path = find_env_file()
    if not path:
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
    return path
