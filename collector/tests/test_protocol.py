"""Tests for the collector protocol module — frame parsing and FrameReader."""

import struct
import time
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from collector.protocol import (
    FRAME_START,
    FRAME_TYPE_HANDSHAKE,
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
    FRAME_TYPE_ADVERTISEMENT,
    FrameReader,
    parse_handshake,
    parse_heartbeat,
    parse_rx_raw,
    parse_tx_raw,
    parse_advertisement,
)


def _make_frame(frame_type, payload):
    """Build a raw binary frame for testing."""
    frame_len = 1 + len(payload)
    return bytes([FRAME_START]) + struct.pack("<H", frame_len) + bytes([frame_type]) + payload


class TestParseHandshake:
    def test_valid_handshake(self):
        payload = b"COLLECTOR" + bytes([1])
        result = parse_handshake(payload)
        assert result["valid"] is True
        assert result["magic"] == "COLLECTOR"
        assert result["version"] == 1

    def test_invalid_magic(self):
        payload = b"WRONGDATA" + bytes([1])
        result = parse_handshake(payload)
        assert result["valid"] is False

    def test_too_short(self):
        result = parse_handshake(b"SHORT")
        assert "error" in result


class TestParseHeartbeat:
    def test_valid_heartbeat(self):
        payload = struct.pack("<IH", 1700000000, 3700)  # timestamp, battery_mv
        payload += struct.pack("<II", 100, 50)  # rx_flood, rx_direct
        payload += struct.pack("<II", 80, 30)  # tx_flood, tx_direct
        payload += bytes([12])  # free_pkts
        payload += struct.pack("<I", 3600)  # uptime_secs
        result = parse_heartbeat(payload)
        assert result["timestamp"] == 1700000000
        assert result["battery_mv"] == 3700
        assert result["rx_flood"] == 100
        assert result["rx_direct"] == 50
        assert result["tx_flood"] == 80
        assert result["tx_direct"] == 30
        assert result["free_pkts"] == 12
        assert result["uptime_secs"] == 3600

    def test_too_short(self):
        result = parse_heartbeat(b"\x00" * 10)
        assert "error" in result


class TestParseRxRaw:
    def test_valid_rx_raw(self):
        # SNR x4 = 20 (SNR=5.0), RSSI = -80, header byte: route=1(FLOOD), payload_type=5(GRP_TXT), version=0
        header = (0x01) | (0x05 << 2) | (0x00 << 6)  # 0x15
        payload = struct.pack("b", 20) + struct.pack("b", -80) + bytes([header]) + b"\x00" * 10
        result = parse_rx_raw(payload)
        assert result["snr"] == 5.0
        assert result["rssi"] == -80
        assert result["route_type"] == 1
        assert result["route_name"] == "FLOOD"
        assert result["payload_type"] == 5
        assert result["payload_name"] == "GRP_TXT"
        assert result["raw_len"] == 11

    def test_too_short(self):
        result = parse_rx_raw(b"\x00\x00")
        assert "error" in result


class TestParseTxRaw:
    def test_valid_tx_raw(self):
        header = (0x02) | (0x03 << 2)  # route=DIRECT, payload=ACK
        payload = bytes([header]) + b"\x00" * 10
        result = parse_tx_raw(payload)
        assert result["route_type"] == 2
        assert result["route_name"] == "DIRECT"
        assert result["payload_type"] == 3
        assert result["payload_name"] == "ACK"


class TestParseAdvertisement:
    def test_valid_advertisement(self):
        payload = struct.pack("<I", 1700000000)  # timestamp
        payload += struct.pack("b", 24)  # snr_x4 = 24 -> SNR=6.0
        payload += b"\xAB" * 32  # pub_key (32 bytes)
        # app_data: flags=0x81 (type=CHAT, has name)
        payload += bytes([0x81])
        payload += b"TestNode\x00"
        result = parse_advertisement(payload)
        assert result["timestamp"] == 1700000000
        assert result["snr"] == 6.0
        assert len(result["pub_key_hex"]) == 64
        assert result["adv_type"] == 1
        assert result["adv_type_name"] == "CHAT"
        assert result["name"] == "TestNode"

    def test_with_location(self):
        payload = struct.pack("<I", 1700000000)
        payload += struct.pack("b", 0)
        payload += b"\x00" * 32
        # flags: 0x12 = type=REPEATER(2) + has lat/lon (0x10)
        payload += bytes([0x12])
        payload += struct.pack("<i", 37774900)  # lat = 37.7749
        payload += struct.pack("<i", -122419400)  # lon = -122.4194
        result = parse_advertisement(payload)
        assert result["adv_type"] == 2
        assert result["adv_type_name"] == "REPEATER"
        assert abs(result["lat"] - 37.7749) < 0.001
        assert abs(result["lon"] - (-122.4194)) < 0.001

    def test_too_short(self):
        result = parse_advertisement(b"\x00" * 10)
        assert "error" in result


class TestFrameReader:
    def test_parses_handshake_frame(self):
        reader = FrameReader()
        payload = b"COLLECTOR" + bytes([1])
        frame_data = _make_frame(FRAME_TYPE_HANDSHAKE, payload)
        reader.feed(frame_data)
        assert len(reader.frames) == 1
        assert reader.frames[0]["type"] == FRAME_TYPE_HANDSHAKE
        assert reader.frames[0]["parsed"]["valid"] is True

    def test_separates_text_and_frames(self):
        reader = FrameReader()
        text = b"Hello world\r\nOK, started\r\n"
        payload = b"COLLECTOR" + bytes([1])
        frame_data = _make_frame(FRAME_TYPE_HANDSHAKE, payload)
        reader.feed(text + frame_data)
        assert len(reader.text_lines) == 2
        assert "Hello world" in reader.text_lines[0]
        assert len(reader.frames) == 1

    def test_multiple_frames(self):
        reader = FrameReader()
        h1 = _make_frame(FRAME_TYPE_HANDSHAKE, b"COLLECTOR" + bytes([1]))
        hb_payload = struct.pack("<IH", 1700000000, 3700)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += bytes([10])
        hb_payload += struct.pack("<I", 100)
        h2 = _make_frame(FRAME_TYPE_HEARTBEAT, hb_payload)
        reader.feed(h1 + h2)
        assert len(reader.frames) == 2
        assert reader.frames[0]["type"] == FRAME_TYPE_HANDSHAKE
        assert reader.frames[1]["type"] == FRAME_TYPE_HEARTBEAT

    def test_take_frames_clears(self):
        reader = FrameReader()
        reader.feed(_make_frame(FRAME_TYPE_HANDSHAKE, b"COLLECTOR" + bytes([1])))
        frames = reader.take_frames()
        assert len(frames) == 1
        assert len(reader.frames) == 0

    def test_take_text_clears(self):
        reader = FrameReader()
        reader.feed(b"line1\nline2\n")
        text = reader.take_text()
        assert len(text) == 2
        assert len(reader.text_lines) == 0

    def test_byte_at_a_time(self):
        """FrameReader works correctly when fed one byte at a time."""
        reader = FrameReader()
        payload = b"COLLECTOR" + bytes([1])
        data = b"test line\n" + _make_frame(FRAME_TYPE_HANDSHAKE, payload)
        for b in data:
            reader.feed(bytes([b]))
        assert len(reader.text_lines) == 1
        assert len(reader.frames) == 1
        assert reader.frames[0]["parsed"]["valid"] is True

    def test_invalid_frame_length_recovered(self):
        reader = FrameReader()
        # Frame with length 0 (invalid)
        bad = bytes([FRAME_START]) + struct.pack("<H", 0)
        good_text = b"OK\n"
        reader.feed(bad + good_text)
        assert len(reader.errors) == 1
        assert "Invalid frame length" in reader.errors[0]
        assert len(reader.text_lines) == 1

    def test_interleaved_text_and_frames(self):
        reader = FrameReader()
        data = b"text before\n"
        data += _make_frame(FRAME_TYPE_HANDSHAKE, b"COLLECTOR" + bytes([1]))
        data += b"text after\n"
        reader.feed(data)
        assert len(reader.text_lines) == 2
        assert len(reader.frames) == 1
