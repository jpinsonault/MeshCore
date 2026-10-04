"""Tests for undecryptable message tracking."""

import tempfile
import time
from unittest.mock import MagicMock

import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.chat import ChatActivity
from collector.core import CollectorCore
from collector.crypto import (
    Channel,
    PAYLOAD_TYPE_GRP_TXT,
    encrypt_then_mac,
)
from collector.events import UndecryptablePacket, CollectorFrame
from collector.protocol import FRAME_TYPE_RX_RAW


def _make_chat():
    return ChatActivity(port="/dev/ttyUSB0", auto_start=False)


def _grp_txt_frame(channel_hash=0xFF, raw=None):
    """Build an RX_RAW frame with GRP_TXT payload type."""
    if raw is None:
        # Build a packet that looks like GRP_TXT but won't decrypt
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | 0x01  # FLOOD
        raw = bytes([header, 0, channel_hash]) + b"\xDE\xAD" + b"\x00" * 16
    return {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": 5.0,
            "rssi": -80,
            "route_type": 1,
            "payload_type": PAYLOAD_TYPE_GRP_TXT,
            "raw": raw,
            "raw_len": len(raw),
        },
    }


class TestCoreUndecryptableCounter:
    def test_counter_starts_at_zero(self):
        core = CollectorCore(port=None, db_path=":memory:")
        assert core._undecryptable_count == 0

    def test_counter_increments_on_failed_decode(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port=None, db_path=f.name)
            core._store = __import__("collector.store", fromlist=["CollectorStore"]).CollectorStore(f.name)
            core._store.open()
            core._channels = [Channel.from_hashtag("#decoy")]

            frame = _grp_txt_frame(channel_hash=0xFF)
            core._process_frame(frame)
            assert core._undecryptable_count == 1

            core._process_frame(frame)
            assert core._undecryptable_count == 2
            core._store.close()

    def test_callback_fires_on_undecryptable(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port=None, db_path=f.name)
            core._store = __import__("collector.store", fromlist=["CollectorStore"]).CollectorStore(f.name)
            core._store.open()
            core._channels = [Channel.from_hashtag("#decoy")]

            counts = []
            core.on_undecryptable = lambda c: counts.append(c)

            core._process_frame(_grp_txt_frame())
            assert counts == [1]

            core._process_frame(_grp_txt_frame())
            assert counts == [1, 2]
            core._store.close()

    def test_no_callback_when_decrypt_succeeds(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port=None, db_path=f.name)
            core._store = __import__("collector.store", fromlist=["CollectorStore"]).CollectorStore(f.name)
            core._store.open()

            ch = Channel.from_hashtag("#test")
            core._channels = [ch]

            # Build a valid encrypted GRP_TXT
            import struct
            ts = struct.pack("<I", 1700000000)
            plaintext = ts + b"\x00" + b"alice: hi\x00"
            mac_and_data = encrypt_then_mac(ch.secret, plaintext)
            header = (PAYLOAD_TYPE_GRP_TXT << 2) | 0x01
            raw = bytes([header, 0, ch.hash]) + mac_and_data

            counts = []
            core.on_undecryptable = lambda c: counts.append(c)
            core._process_frame(_grp_txt_frame(raw=raw))

            assert counts == []
            assert core._undecryptable_count == 0
            core._store.close()


class TestUndecryptableEvent:
    def test_event_has_count(self):
        evt = UndecryptablePacket(42)
        assert evt.count == 42


class TestChatUndecryptableDisplay:
    def test_sidebar_shows_encrypted_count(self, app, mock_screen):
        chat = _make_chat()
        app.start_activity(chat)

        # Simulate undecryptable event
        chat._on_undecryptable(UndecryptablePacket(5))

        # Check sidebar contains encrypted count
        items = chat._sidebar_items()
        text = "\n".join(items)
        assert "5 encrypted" in text

    def test_sidebar_no_encrypted_when_zero(self, app, mock_screen):
        chat = _make_chat()
        app.start_activity(chat)

        items = chat._sidebar_items()
        text = "\n".join(items)
        assert "encrypted" not in text

    def test_count_updates_on_event(self, app, mock_screen):
        chat = _make_chat()
        app.start_activity(chat)

        chat._on_undecryptable(UndecryptablePacket(1))
        assert chat._undecryptable_count == 1

        chat._on_undecryptable(UndecryptablePacket(3))
        assert chat._undecryptable_count == 3
