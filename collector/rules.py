"""
MeshCore Collector — rule-based candidate generation (hashcat-style mangling).

Real hashtag channels are rarely random strings — they're dictionary words with
digits/years appended or hyphenated connectors (#weather2024, #bot-tacoma,
#seattle-mesh). Raw a-z0-9 brute-force at <=7 chars can't reach most of these
(hyphens, length), but mangling the known catalog + built-in wordlist does, for
a few hundred thousand high-value candidates checked in well under a second.

generate_candidates() yields bare names (no '#'); RulesMatcher indexes them by
channel-hash byte so a crack only MAC-checks the ~1/256 that could match.
Verification is unchanged (exact key + strict plaintext), so no quality loss.
"""

import hashlib
import re
from collections import defaultdict

from .crypto import (
    CIPHER_KEY_SIZE,
    Channel,
    grp_txt_plaintext_ok,
    mac_then_decrypt,
)

# Connectors seen in real hyphenated channel names (both #word-conn and #conn-word).
CONNECTORS = [
    "bot", "net", "mesh", "test", "dev", "chat", "news", "info", "alert",
    "emergency", "emcomm", "weather", "hub", "node", "local", "1", "2", "3",
    "north", "south", "east", "west", "central",
]

# Numeric suffixes beyond 0-9: common pick-a-numbers and years.
NUMERIC_SUFFIXES = ["00", "01", "02", "07", "11", "12", "21", "22", "23", "42",
                    "69", "99", "123", "420", "911"]
YEARS = [str(y) for y in range(2018, 2027)]

_VALID_SEGMENT = re.compile(r"^[a-z0-9]{1,29}$")


def _normalize(word):
    """Lowercase, strip a leading '#', keep only names that are valid channel
    segments (or hyphen-joined valid segments)."""
    w = word.strip().lstrip("#").lower()
    if not w:
        return None
    for seg in w.split("-"):
        if not _VALID_SEGMENT.match(seg):
            return None
    return w


def generate_candidates(base_words):
    """Yield mangled candidate names (bare, no '#') from base_words.

    Bounded at ~70 mutations per base word, deduped by the caller's index.
    """
    seen_bases = set()
    for raw in base_words:
        base = _normalize(raw)
        if base is None or base in seen_bases:
            continue
        seen_bases.add(base)

        # Numeric suffixes
        for d in range(10):
            yield f"{base}{d}"
        for n in NUMERIC_SUFFIXES:
            yield f"{base}{n}"
        for y in YEARS:
            yield f"{base}{y}"

        # Hyphenated connectors (both directions)
        for c in CONNECTORS:
            if c != base:
                yield f"{base}-{c}"
                yield f"{c}-{base}"


class RulesMatcher:
    """Hash-indexed rule candidates. build() is ~190K names (strings only, no
    Channel objects) grouped by channel-hash byte; match() MAC-checks the small
    bucket for a target hash."""

    def __init__(self, base_words):
        self._base = list(base_words)
        self._by_hash = defaultdict(list)  # hash_byte -> ['#name', ...]
        self._count = 0
        self._built = False

    @property
    def built(self):
        return self._built

    @property
    def count(self):
        return self._count

    def build(self):
        """Derive each candidate's channel-hash byte and index it. Idempotent."""
        if self._built:
            return
        sha256 = hashlib.sha256
        by_hash = defaultdict(list)
        seen = set()
        for name in generate_candidates(self._base):
            full = "#" + name
            if full in seen:
                continue
            seen.add(full)
            psk = sha256(full.encode("utf-8")).digest()[:CIPHER_KEY_SIZE]
            hb = sha256(psk).digest()[0]
            by_hash[hb].append(full)
        self._by_hash = by_hash
        self._count = len(seen)
        self._built = True

    def match(self, hash_byte, mac_and_data):
        """Return a matching Channel for this hash + packet, or None."""
        for full in self._by_hash.get(hash_byte, ()):
            ch = Channel.from_hashtag(full)
            plaintext = mac_then_decrypt(ch.secret, mac_and_data)
            if plaintext is not None and grp_txt_plaintext_ok(plaintext):
                return ch
        return None
