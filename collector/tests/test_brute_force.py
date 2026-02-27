"""Unit tests for the brute-force channel cracker."""

import struct
import time

import pytest

from collector.brute_force import brute_force_channel, _crack_chunk
from collector.crypto import (
    Channel,
    encrypt_then_mac,
    extract_group_payload,
    PAYLOAD_TYPE_GRP_TXT,
    ROUTE_TYPE_FLOOD,
)


def _make_packet(channel_name):
    """Build a raw GRP_TXT packet encrypted under a hashtag channel."""
    ch = Channel.from_hashtag(channel_name)
    plaintext = struct.pack("<I", 1700000000) + b"\x00" + b"alice: hello\x00"
    mac_and_data = encrypt_then_mac(ch.secret, plaintext)
    payload = bytes([ch.hash]) + mac_and_data
    header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
    return bytes([header, 0x00]) + payload


class TestBruteForce:
    def test_cracks_1_char_name(self):
        """Brute-force a 1-character channel name."""
        raw = _make_packet("#z")
        extracted = extract_group_payload(raw)
        result = brute_force_channel(
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset="abcdefghijklmnopqrstuvwxyz",
            max_length=1,
            num_workers=2,
        )
        assert result == "#z"

    def test_cracks_3_char_name(self):
        """Brute-force a 3-character channel name (17K candidates)."""
        raw = _make_packet("#fox")
        extracted = extract_group_payload(raw)
        result = brute_force_channel(
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset="abcdefghijklmnopqrstuvwxyz",
            max_length=3,
            num_workers=2,
        )
        assert result == "#fox"

    def test_cracks_4_char_alphanumeric(self):
        """Brute-force a 4-char alphanumeric name (1.7M candidates)."""
        raw = _make_packet("#b2c3")
        extracted = extract_group_payload(raw)
        t0 = time.monotonic()
        result = brute_force_channel(
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset="abcdefghijklmnopqrstuvwxyz0123456789",
            max_length=4,
            num_workers=4,
        )
        elapsed = time.monotonic() - t0
        assert result == "#b2c3", f"Got {result} in {elapsed:.2f}s"

    def test_returns_none_when_not_found(self):
        """Returns None when the name exceeds max_length."""
        raw = _make_packet("#toolong")
        extracted = extract_group_payload(raw)
        result = brute_force_channel(
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset="abcdefghijklmnopqrstuvwxyz",
            max_length=2,
            num_workers=2,
        )
        assert result is None

    def test_progress_callback(self):
        """Progress callback fires for each completed length."""
        raw = _make_packet("#ab")
        extracted = extract_group_payload(raw)
        progress = []
        result = brute_force_channel(
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset="abcdefghijklmnopqrstuvwxyz",
            max_length=2,
            num_workers=2,
            on_progress=lambda length, total, elapsed: progress.append(
                (length, total)
            ),
        )
        assert result == "#ab"
        # Length 1 should have been searched and reported before length 2
        assert len(progress) >= 1
        assert progress[0] == (1, 26)

    def test_crack_chunk_direct(self):
        """Test the worker function directly (no multiprocessing)."""
        raw = _make_packet("#hi")
        extracted = extract_group_payload(raw)
        charset = b"abcdefghijklmnopqrstuvwxyz"
        # "hi" = index h*26 + i = 7*26 + 8 = 190
        result = _crack_chunk((
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset, 2, 0, 26 * 26,
        ))
        assert result == "#hi"

    def test_crack_chunk_miss(self):
        """Worker returns None when answer isn't in the range."""
        raw = _make_packet("#zz")
        extracted = extract_group_payload(raw)
        charset = b"abcdefghijklmnopqrstuvwxyz"
        # Search only first 100 candidates — "zz" is at index 675
        result = _crack_chunk((
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset, 2, 0, 100,
        ))
        assert result is None

    def test_single_worker(self):
        """Works with num_workers=1 (no parallelism)."""
        raw = _make_packet("#cat")
        extracted = extract_group_payload(raw)
        result = brute_force_channel(
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset="abcdefghijklmnopqrstuvwxyz",
            max_length=3,
            num_workers=1,
        )
        assert result == "#cat"
