"""Tests for the collector protocol module — frame parsing and FrameReader."""

import struct
import time
import pytest

from collector.protocol import (
    FRAME_START,
    FRAME_TYPE_HANDSHAKE,
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_HOST_ACK,
    FRAME_TYPE_HOST_RESUME,
    FrameReader,
    crc16_ccitt,
    build_host_frame,
    build_ack_frame,
    build_resume_frame,
    parse_handshake,
    parse_heartbeat,
    parse_rx_raw,
    parse_tx_raw,
    parse_advertisement,
)


def _make_frame(frame_type, payload):
    """Build a raw binary v1 frame for testing."""
    frame_len = 1 + len(payload)
    return bytes([FRAME_START]) + struct.pack("<H", frame_len) + bytes([frame_type]) + payload


def _make_v2_frame(frame_type, seq, payload):
    """Build a raw binary v2 frame (with seq + CRC) for testing."""
    seq_bytes = struct.pack("<I", seq)
    crc_data = bytes([frame_type]) + seq_bytes + payload
    crc = crc16_ccitt(crc_data)
    frame_len = 1 + 4 + len(payload) + 2  # type + seq + payload + crc
    return (
        bytes([FRAME_START])
        + struct.pack("<H", frame_len)
        + crc_data
        + struct.pack("<H", crc)
    )


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


class TestParseHandshakeV2:
    def test_v2_handshake_with_seq_range(self):
        payload = b"COLLECTOR" + bytes([2])
        payload += struct.pack("<I", 42)   # oldest_seq
        payload += struct.pack("<I", 100)  # newest_seq
        result = parse_handshake(payload)
        assert result["valid"] is True
        assert result["version"] == 2
        assert result["oldest_seq"] == 42
        assert result["newest_seq"] == 100

    def test_v2_handshake_empty_buffer(self):
        payload = b"COLLECTOR" + bytes([2])
        payload += struct.pack("<I", 0)  # oldest_seq
        payload += struct.pack("<I", 0)  # newest_seq
        result = parse_handshake(payload)
        assert result["oldest_seq"] == 0
        assert result["newest_seq"] == 0

    def test_v1_handshake_no_seq_fields(self):
        """v1 handshake should not have oldest/newest_seq."""
        payload = b"COLLECTOR" + bytes([1])
        result = parse_handshake(payload)
        assert result["version"] == 1
        assert "oldest_seq" not in result
        assert "newest_seq" not in result

    def test_v2_handshake_backwards_compat(self):
        """v1 host should parse v2 handshake payload without error (extra bytes ignored)."""
        payload = b"COLLECTOR" + bytes([2]) + struct.pack("<II", 1, 50)
        result = parse_handshake(payload)
        assert result["valid"] is True
        assert result["version"] == 2


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


class TestCRC16CCITT:
    def test_known_vector(self):
        """Standard CRC-16-CCITT test vector."""
        assert crc16_ccitt(b"123456789") == 0x29B1

    def test_empty(self):
        assert crc16_ccitt(b"") == 0xFFFF

    def test_single_byte(self):
        result = crc16_ccitt(b"\x00")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_deterministic(self):
        data = b"hello world"
        assert crc16_ccitt(data) == crc16_ccitt(data)


class TestBuildHostFrames:
    def test_ack_frame_structure(self):
        frame = build_ack_frame(42)
        # [0xC0] [len_lo] [len_hi] [type=0xA0] [seq=0(4B)] [last_seq=42(4B)] [crc(2B)]
        assert frame[0] == FRAME_START
        frame_len = struct.unpack("<H", frame[1:3])[0]
        assert frame_len == 11  # 1(type) + 4(seq=0) + 4(payload) + 2(crc)
        assert frame[3] == FRAME_TYPE_HOST_ACK
        # seq field should be 0
        assert struct.unpack("<I", frame[4:8])[0] == 0
        # payload is last_seq
        assert struct.unpack("<I", frame[8:12])[0] == 42
        # CRC should be valid
        crc_data = frame[3:12]  # type + seq + payload
        crc_expected = struct.unpack("<H", frame[12:14])[0]
        assert crc16_ccitt(crc_data) == crc_expected

    def test_resume_frame_structure(self):
        frame = build_resume_frame(100)
        assert frame[0] == FRAME_START
        frame_len = struct.unpack("<H", frame[1:3])[0]
        assert frame_len == 11
        assert frame[3] == FRAME_TYPE_HOST_RESUME
        assert struct.unpack("<I", frame[8:12])[0] == 100

    def test_ack_frame_crc_valid(self):
        frame = build_ack_frame(999)
        # Verify CRC is correct
        frame_len = struct.unpack("<H", frame[1:3])[0]
        data = frame[3:3 + frame_len]
        crc_data = data[:-2]
        crc_val = struct.unpack("<H", data[-2:])[0]
        assert crc16_ccitt(crc_data) == crc_val


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


class TestFrameReaderV2:
    def test_auto_upgrade_on_v2_handshake(self):
        """FrameReader upgrades protocol_version after v2 handshake."""
        reader = FrameReader()
        assert reader.protocol_version == 1
        hs_payload = b"COLLECTOR" + bytes([2]) + struct.pack("<II", 1, 50)
        reader.feed(_make_frame(FRAME_TYPE_HANDSHAKE, hs_payload))
        assert reader.protocol_version == 2
        assert reader.frames[0]["parsed"]["version"] == 2

    def test_v2_frame_parsed_with_seq_and_crc(self):
        """After v2 upgrade, data frames include seq and validate CRC."""
        reader = FrameReader()
        # Send v2 handshake first (v1 wire format)
        hs = _make_frame(FRAME_TYPE_HANDSHAKE, b"COLLECTOR" + bytes([2]) + struct.pack("<II", 0, 0))
        reader.feed(hs)
        reader.take_frames()

        # Now send a v2 heartbeat frame
        hb_payload = struct.pack("<IH", 1700000000, 3700)
        hb_payload += struct.pack("<II", 100, 50)
        hb_payload += struct.pack("<II", 80, 30)
        hb_payload += bytes([12])
        hb_payload += struct.pack("<I", 3600)

        v2_frame = _make_v2_frame(FRAME_TYPE_HEARTBEAT, 42, hb_payload)
        reader.feed(v2_frame)

        frames = reader.take_frames()
        assert len(frames) == 1
        assert frames[0]["seq"] == 42
        assert frames[0]["type"] == FRAME_TYPE_HEARTBEAT
        assert frames[0]["parsed"]["uptime_secs"] == 3600

    def test_v2_crc_mismatch_rejected(self):
        """Frames with bad CRC are rejected in v2 mode."""
        reader = FrameReader()
        reader.protocol_version = 2  # Force v2 mode

        # Build valid v2 frame then corrupt the CRC
        hb_payload = struct.pack("<IH", 1700000000, 3700)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += bytes([10])
        hb_payload += struct.pack("<I", 100)

        v2_frame = bytearray(_make_v2_frame(FRAME_TYPE_HEARTBEAT, 1, hb_payload))
        # Corrupt last byte (CRC)
        v2_frame[-1] ^= 0xFF
        reader.feed(bytes(v2_frame))

        assert len(reader.frames) == 0
        assert any("CRC mismatch" in e for e in reader.errors)

    def test_v1_frames_still_work_before_upgrade(self):
        """v1 frames work normally before v2 handshake."""
        reader = FrameReader()
        hb_payload = struct.pack("<IH", 1700000000, 3700)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += bytes([10])
        hb_payload += struct.pack("<I", 100)
        reader.feed(_make_frame(FRAME_TYPE_HEARTBEAT, hb_payload))
        assert len(reader.frames) == 1
        assert "seq" not in reader.frames[0]

    def test_handshake_always_v1_format(self):
        """Handshake frames are always parsed as v1 even after v2 upgrade."""
        reader = FrameReader()
        reader.protocol_version = 2  # Already in v2 mode

        # Send v1-format handshake (no seq/CRC)
        hs = _make_frame(FRAME_TYPE_HANDSHAKE, b"COLLECTOR" + bytes([2]) + struct.pack("<II", 5, 100))
        reader.feed(hs)
        assert len(reader.frames) == 1
        assert reader.frames[0]["parsed"]["valid"] is True
        assert reader.frames[0]["parsed"]["oldest_seq"] == 5

    def test_v2_multiple_frames_incrementing_seq(self):
        """Multiple v2 frames have different seq numbers."""
        reader = FrameReader()
        reader.protocol_version = 2

        hb_payload = struct.pack("<IH", 1700000000, 3700)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += struct.pack("<II", 0, 0)
        hb_payload += bytes([10])
        hb_payload += struct.pack("<I", 100)

        for seq in [1, 2, 3]:
            reader.feed(_make_v2_frame(FRAME_TYPE_HEARTBEAT, seq, hb_payload))

        frames = reader.take_frames()
        assert len(frames) == 3
        assert [f["seq"] for f in frames] == [1, 2, 3]
