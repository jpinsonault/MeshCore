"""MeshCore Collector — channel-cracker web app.

A small HTTP backend plus a single static page for cracking hashtag channels
from captured GRP_TXT packets, using the GPU brute-forcer when available and the
CPU brute-forcer as a fallback.

Two modes:
  - live: wraps a running CollectorCore (serial/network link + store + cracker)
  - offline: opens an existing SQLite DB so already-captured packets can be
    cracked without any hardware attached (used by the tests)
"""

from .cracker_app import CrackerApp

__all__ = ["CrackerApp"]
