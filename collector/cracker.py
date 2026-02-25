"""
MeshCore Collector — Channel Cracker.

Passive dictionary attack on hashtag channels. MeshCore's hashtag channels
derive their AES-128 key from SHA-256("#name")[:16], so a short wordlist
cracks nearly all of them instantly.

Algorithm:
  1. Pre-compute Channel.from_hashtag(word) for every candidate, index by hash byte
  2. When an undecryptable GRP_TXT arrives, look up candidates by hash byte (~1-2 per hash)
  3. Try MAC verification against one stored packet — if it passes, cracked
  4. Retroactively decrypt all stored packets for that channel
  5. Fire callback so TUI can show decoded messages inline

The retroactive_decrypt() pipeline is shared with manual /join — both paths
funnel into: "new key → scan stored packets → decode → notify UI."
"""

import threading
import time
from collections import defaultdict
from typing import Callable, Optional

from .crypto import (
    Channel,
    GroupMessage,
    extract_group_payload,
    mac_then_decrypt,
    try_decode_group_message,
    PAYLOAD_TYPE_GRP_TXT,
)

# Built-in wordlist (~300 common hashtag channel names)
BUILTIN_WORDLIST = [
    # Generic chat
    "general", "chat", "random", "lounge", "lobby", "main", "public",
    "hello", "welcome", "test", "testing", "debug", "dev",
    # Mesh / radio
    "mesh", "meshcore", "meshtastic", "lora", "radio", "rf", "ham",
    "hf", "vhf", "uhf", "aprs", "sstv", "cw", "morse", "beacon",
    "repeater", "node", "gateway", "bridge", "relay", "link",
    # Emergency / ARES
    "emergency", "sos", "help", "ares", "races", "skywarn", "emcomm",
    "weather", "wx", "storm", "fire", "earthquake", "flood",
    # Outdoor / adventure
    "hiking", "camping", "trail", "mountain", "summit", "base",
    "offgrid", "off-grid", "backcountry", "wilderness", "nature",
    "outdoor", "outdoors", "adventure", "explore", "travel",
    "hunting", "fishing", "kayak", "bike", "cycling",
    # Community
    "community", "local", "neighborhood", "town", "city", "county",
    "club", "group", "team", "family", "friends", "home",
    "school", "work", "office", "lab", "shop", "garage",
    "meetup", "event", "party", "gathering",
    # Tech
    "tech", "maker", "hacker", "arduino", "esp32", "raspberry",
    "linux", "python", "code", "software", "hardware", "iot",
    "sensor", "gps", "solar", "battery", "power",
    # Regions (common)
    "north", "south", "east", "west", "central", "downtown",
    "bay", "coast", "valley", "ridge", "lake", "river", "island",
    # Numbered variants (common pattern)
    "channel1", "channel2", "channel3", "channel4", "channel5",
    "ch1", "ch2", "ch3", "ch4", "ch5",
    "room1", "room2", "room3", "room4", "room5",
    "net1", "net2", "net3", "net4", "net5",
    "test1", "test2", "test3", "test4", "test5",
    "group1", "group2", "group3", "group4", "group5",
    # Callsign-style
    "net", "roundtable", "ragchew", "swap", "trade",
    "simplex", "duplex", "repeaters", "nodes",
    # Topics
    "news", "info", "announce", "announcements", "updates",
    "alerts", "notifications", "status", "log", "monitor",
    "security", "safety", "rescue", "medical",
    # Fun / social
    "gaming", "games", "music", "movies", "food", "cooking",
    "sports", "fitness", "health", "science", "space", "astronomy",
    "photography", "art", "crafts", "garden", "gardening", "pets",
    "dogs", "cats", "birds",
    # Survival / prepper
    "prepper", "survival", "bugout", "shtf", "grid", "offgrid",
    "ham-radio", "comms", "communications",
    # Misc common words
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
    "default", "demo", "example", "sample", "temp", "tmp",
    "admin", "mod", "ops", "staff", "support",
    "open", "free", "new", "old", "archive",
    # Single letters / numbers (very short names)
    "a", "b", "c", "1", "2", "3",
    # Case variants of very common names
    "General", "Chat", "Mesh", "Test", "Main", "Public",
    "MeshCore", "Meshtastic", "LoRa", "Radio",
    # MeshCore-specific defaults
    "meshcore-general", "meshcore-test", "meshcore-dev",
    "mc-general", "mc-test", "mc-chat",
]


class ChannelCracker:
    """Passive dictionary attacker for hashtag channels.

    Pure logic, no TUI dependency. Can run headless.
    """

    def __init__(self, store, known_channels=None):
        self._store = store
        self._known_hashes = set()
        self._known_names = set()
        self._pending_hashes = set()
        self._word_by_hash = defaultdict(list)  # hash_byte -> [(word, Channel)]
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._cracked_count = 0

        # Callback: (channel_name, decoded_count) -> None
        self.on_channel_discovered: Optional[Callable] = None

        # Register already-known channels
        if known_channels:
            for ch in known_channels:
                self._known_hashes.add(ch.hash)
                self._known_names.add(ch.name)

        # Build hash table from built-in wordlist
        self._build_hash_table(BUILTIN_WORDLIST)

    def _build_hash_table(self, words):
        """Pre-compute Channel.from_hashtag for each word, index by hash byte."""
        for word in words:
            try:
                ch = Channel.from_hashtag(word)
            except Exception:
                continue
            # Only add if not already known
            if ch.name not in self._known_names:
                self._word_by_hash[ch.hash].append((word, ch))

    def load_cache(self):
        """Load previously cracked channels from SQLite and return them as Channel objects."""
        channels = []
        try:
            rows = self._store.get_cracked_channels()
        except Exception:
            return channels
        for row in rows:
            name = row["channel_name"]
            if name in self._known_names:
                continue
            try:
                ch = Channel.from_hashtag(name)
                self._known_hashes.add(ch.hash)
                self._known_names.add(ch.name)
                self._cracked_count += 1
                channels.append(ch)
            except Exception:
                continue
        return channels

    @property
    def running(self):
        return self._running

    @property
    def cracked_count(self):
        return self._cracked_count

    @property
    def pending_count(self):
        with self._lock:
            return len(self._pending_hashes)

    @property
    def pending_hashes(self):
        """Return a copy of the pending hash set."""
        with self._lock:
            return set(self._pending_hashes)

    @property
    def cracked_names(self):
        """Return the set of cracked channel names (excluding pre-configured ones)."""
        with self._lock:
            return set(self._known_names)

    @property
    def wordlist_size(self):
        """Return total number of candidate words in the hash table."""
        return sum(len(v) for v in self._word_by_hash.values())

    def status(self):
        """Return a structured status dict for API/TUI consumption."""
        with self._lock:
            return {
                "running": self._running,
                "cracked_count": self._cracked_count,
                "pending_count": len(self._pending_hashes),
                "pending_hashes": sorted(self._pending_hashes),
                "cracked_names": sorted(self._known_names),
                "wordlist_size": self.wordlist_size,
            }

    def start(self):
        """Start the background cracking thread."""
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background cracking thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._running = False

    def notify_unknown_hash(self, channel_hash):
        """Called by core when a GRP_TXT packet can't be decoded."""
        if channel_hash in self._known_hashes:
            return
        with self._lock:
            self._pending_hashes.add(channel_hash)

    def add_wordlist(self, path):
        """Add words from a file (one per line) to the hash table."""
        try:
            with open(path) as f:
                words = [line.strip() for line in f if line.strip()]
            self._build_hash_table(words)
        except OSError:
            pass

    def retroactive_decrypt(self, channel):
        """Scan stored GRP_TXT packets and try to decrypt with the given channel.

        This is the shared pipeline used by both cracking and manual /join.
        Returns the number of newly decoded messages.
        """
        if not self._store:
            return 0

        packets = self._store.get_grp_txt_packets()
        decoded_count = 0

        for pkt in packets:
            raw_packet_id = pkt["id"]
            # Skip if already decoded
            if self._store.has_channel_message_for_packet(raw_packet_id):
                continue

            raw_hex = pkt.get("raw_hex", "")
            if not raw_hex:
                continue

            try:
                raw_bytes = bytes.fromhex(raw_hex)
            except ValueError:
                continue

            msg = try_decode_group_message(
                raw_bytes, [channel], pkt["timestamp"]
            )
            if msg:
                self._store.store_channel_message(msg, raw_packet_id=raw_packet_id)
                decoded_count += 1

        return decoded_count

    def _run(self):
        """Background thread: try to crack pending hashes."""
        self._running = True
        while not self._stop_event.is_set():
            with self._lock:
                pending = set(self._pending_hashes)

            if not pending:
                self._stop_event.wait(timeout=1.0)
                continue

            for h in pending:
                if self._stop_event.is_set():
                    break
                if h in self._known_hashes:
                    with self._lock:
                        self._pending_hashes.discard(h)
                    continue

                candidates = self._word_by_hash.get(h, [])
                if not candidates:
                    # No dictionary match for this hash byte — can't crack
                    with self._lock:
                        self._pending_hashes.discard(h)
                    continue

                cracked = self._try_crack(h, candidates)
                if cracked:
                    with self._lock:
                        self._pending_hashes.discard(h)

            # Sleep briefly between scan rounds
            self._stop_event.wait(timeout=0.5)

        self._running = False

    def _try_crack(self, hash_byte, candidates):
        """Try candidate channels against stored packets with matching hash.

        Returns True if cracked, False otherwise.
        """
        # Get one stored packet with this hash byte to test against
        packets = self._store.get_grp_txt_packets(limit=500)
        test_packets = []
        for pkt in packets:
            raw_hex = pkt.get("raw_hex", "")
            if not raw_hex:
                continue
            try:
                raw_bytes = bytes.fromhex(raw_hex)
            except ValueError:
                continue
            extracted = extract_group_payload(raw_bytes)
            if extracted and extracted["channel_hash"] == hash_byte:
                test_packets.append((pkt, raw_bytes, extracted))
                if len(test_packets) >= 3:
                    break

        if not test_packets:
            return False

        for word, channel in candidates:
            for pkt, raw_bytes, extracted in test_packets:
                result = mac_then_decrypt(channel.secret, extracted["mac_and_data"])
                if result is not None:
                    # Cracked!
                    self._known_hashes.add(hash_byte)
                    self._known_names.add(channel.name)

                    # Retroactively decrypt all stored packets
                    decoded_count = self.retroactive_decrypt(channel)

                    # Cache in SQLite
                    self._store.store_cracked_channel(
                        channel.name, hash_byte, decoded_count
                    )

                    self._cracked_count += 1

                    # Fire callback
                    if self.on_channel_discovered:
                        self.on_channel_discovered(channel.name, decoded_count)

                    return True

        return False
