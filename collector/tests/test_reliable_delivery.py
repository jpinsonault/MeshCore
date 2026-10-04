"""Tests for reliable delivery: RESUME, ACK, seq tracking, replay dedup."""

import struct
import tempfile
import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from collector.protocol import (
    FRAME_START,
    FRAME_TYPE_HANDSHAKE,
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_HOST_ACK,
    FRAME_TYPE_HOST_RESUME,
    FrameReader,
    crc16_ccitt,
    build_ack_frame,
    build_resume_frame,
)
from collector.store import CollectorStore
from collector.core import CollectorCore


def _make_v1_frame(frame_type, payload):
    """Build a v1 wire frame."""
    frame_len = 1 + len(payload)
    return bytes([FRAME_START]) + struct.pack("<H", frame_len) + bytes([frame_type]) + payload


def _make_v2_frame(frame_type, seq, payload):
    """Build a v2 wire frame with seq and CRC."""
    seq_bytes = struct.pack("<I", seq)
    crc_data = bytes([frame_type]) + seq_bytes + payload
    crc = crc16_ccitt(crc_data)
    frame_len = 1 + 4 + len(payload) + 2
    return (
        bytes([FRAME_START])
        + struct.pack("<H", frame_len)
        + crc_data
        + struct.pack("<H", crc)
    )


def _hb_payload():
    """Minimal valid heartbeat payload (27 bytes)."""
    p = struct.pack("<IH", 1700000000, 3700)
    p += struct.pack("<II", 0, 0)
    p += struct.pack("<II", 0, 0)
    p += bytes([10])
    p += struct.pack("<I", 100)
    return p


def _v2_handshake(oldest=0, newest=0):
    """Build a v2 handshake frame (v1 wire format)."""
    payload = b"COLLECTOR" + bytes([2]) + struct.pack("<II", oldest, newest)
    return _make_v1_frame(FRAME_TYPE_HANDSHAKE, payload)


class TestResumeOnV2Connect:
    """Core should send RESUME frame after v2 handshake."""

    def test_resume_sent_on_v2_handshake(self):
        """When handshake is v2, core sends RESUME with last_committed_seq."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            # Pre-seed the DB with a committed seq
            store = CollectorStore(f.name)
            store.open()
            store.set_last_committed_seq(50)
            store.close()

            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._reader = FrameReader()
            core._ser = MagicMock()
            core._ser.is_open = True

            # Reset state
            core._protocol_version = 1
            core._highest_seq_seen = 0
            core._last_ack_time = 0.0
            core._replay_above_seq = 0

            # Simulate handshake parsed result
            parsed = {"valid": True, "version": 2, "oldest_seq": 1, "newest_seq": 100}
            core._handle_handshake(parsed)

            assert core._protocol_version == 2
            assert core._reader.protocol_version == 2
            assert core._replay_above_seq == 50

            # Verify RESUME was sent
            core._ser.write.assert_called()
            sent_data = core._ser.write.call_args[0][0]
            assert sent_data == build_resume_frame(50)

            core._store.close()

    def test_no_resume_on_v1_handshake(self):
        """When handshake is v1, no RESUME or ACK should be sent."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._reader = FrameReader()
            core._ser = MagicMock()

            parsed = {"valid": True, "version": 1}
            core._handle_handshake(parsed)

            assert core._protocol_version == 1
            core._ser.write.assert_not_called()

            core._store.close()


class TestSeqTracking:
    """Core should track highest seq and detect resets."""

    def test_highest_seq_tracked(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._reader = FrameReader()
            core._protocol_version = 2

            frame1 = {"type": FRAME_TYPE_HEARTBEAT, "seq": 5, "received_at": time.time(),
                       "parsed": {"timestamp": 0, "battery_mv": 0, "rx_flood": 0,
                                  "rx_direct": 0, "tx_flood": 0, "tx_direct": 0,
                                  "free_pkts": 0, "uptime_secs": 0}}
            frame2 = {"type": FRAME_TYPE_HEARTBEAT, "seq": 10, "received_at": time.time(),
                       "parsed": {"timestamp": 0, "battery_mv": 0, "rx_flood": 0,
                                  "rx_direct": 0, "tx_flood": 0, "tx_direct": 0,
                                  "free_pkts": 0, "uptime_secs": 0}}

            core._process_frame(frame1)
            assert core._highest_seq_seen == 5
            core._process_frame(frame2)
            assert core._highest_seq_seen == 10

            core._store.close()

    def test_seq_reset_detection(self):
        """When incoming seq < last seen and seq < 100, log a warning."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._reader = FrameReader()
            core._protocol_version = 2

            text_lines = []
            core.on_text = lambda line: text_lines.append(line)

            # See seq 1000 first
            core._highest_seq_seen = 1000

            frame = {"type": FRAME_TYPE_HEARTBEAT, "seq": 1, "received_at": time.time(),
                      "parsed": {"timestamp": 0, "battery_mv": 0, "rx_flood": 0,
                                 "rx_direct": 0, "tx_flood": 0, "tx_direct": 0,
                                 "free_pkts": 0, "uptime_secs": 0}}
            core._process_frame(frame)

            assert any("seq reset" in line for line in text_lines)
            core._store.close()


class TestReplayDedup:
    """Frames with seq <= replay_above_seq should be skipped for storage."""

    def test_replay_frames_not_stored(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._protocol_version = 2
            core._replay_above_seq = 10

            fired_frames = []
            core.on_frame = lambda fr: fired_frames.append(fr)

            rx_parsed = {"snr": 5.0, "rssi": -80, "route_type": 1,
                          "payload_type": 5, "raw": b"\x15" + b"\x00" * 19,
                          "raw_len": 20}

            # Frame with seq <= replay_above_seq: should NOT be stored
            frame_replay = {"type": FRAME_TYPE_RX_RAW, "seq": 5,
                            "received_at": time.time(), "parsed": rx_parsed}
            core._process_frame(frame_replay)

            assert core._store.get_stats()["total_packets"] == 0
            # But callback should still fire
            assert len(fired_frames) == 1

            core._store.close()

    def test_new_frames_stored_after_replay(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._protocol_version = 2
            core._replay_above_seq = 10

            rx_parsed = {"snr": 5.0, "rssi": -80, "route_type": 1,
                          "payload_type": 5, "raw": b"\x15" + b"\x00" * 19,
                          "raw_len": 20}

            # Frame above replay watermark: should be stored
            frame_new = {"type": FRAME_TYPE_RX_RAW, "seq": 11,
                         "received_at": time.time(), "parsed": rx_parsed}
            core._process_frame(frame_new)

            assert core._store.get_stats()["total_packets"] == 1
            # Watermark should be cleared
            assert core._replay_above_seq == 0

            core._store.close()

    def test_replay_watermark_cleared_on_first_new_frame(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._protocol_version = 2
            core._replay_above_seq = 5

            rx_parsed = {"snr": 5.0, "rssi": -80, "route_type": 1,
                          "payload_type": 5, "raw": b"\x15" + b"\x00" * 19,
                          "raw_len": 20}

            # Skip replayed frame
            core._process_frame({"type": FRAME_TYPE_RX_RAW, "seq": 3,
                                 "received_at": time.time(), "parsed": rx_parsed})
            assert core._replay_above_seq == 5  # still set

            # First new frame clears watermark
            core._process_frame({"type": FRAME_TYPE_RX_RAW, "seq": 6,
                                 "received_at": time.time(), "parsed": rx_parsed})
            assert core._replay_above_seq == 0

            # Subsequent frames stored normally
            core._process_frame({"type": FRAME_TYPE_RX_RAW, "seq": 7,
                                 "received_at": time.time(), "parsed": rx_parsed})
            assert core._store.get_stats()["total_packets"] == 2  # seq 6 + 7

            core._store.close()


class TestPeriodicAck:
    """Core should send ACK every ~1 second in v2 mode."""

    def test_ack_sent_after_interval(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._ser = MagicMock()
            core._ser.is_open = True
            core._protocol_version = 2
            core._highest_seq_seen = 42

            core._send_ack(42)

            core._ser.write.assert_called_once_with(build_ack_frame(42))
            assert core._store.get_last_committed_seq() == 42

            core._store.close()


class TestV1Fallback:
    """v1 firmware should work normally without RESUME or ACK."""

    def test_v1_frames_stored_without_seq(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            core = CollectorCore(port="/dev/null", db_path=f.name)
            core._store = CollectorStore(f.name)
            core._store.open()
            core._protocol_version = 1

            rx_parsed = {"snr": 5.0, "rssi": -80, "route_type": 1,
                          "payload_type": 5, "raw": b"\x15" + b"\x00" * 19,
                          "raw_len": 20}

            # v1 frame has no seq
            frame = {"type": FRAME_TYPE_RX_RAW, "received_at": time.time(),
                     "parsed": rx_parsed}
            core._process_frame(frame)

            assert core._store.get_stats()["total_packets"] == 1
            rows = core._store.get_recent_packets(limit=1)
            assert rows[0]["seq"] is None

            core._store.close()
