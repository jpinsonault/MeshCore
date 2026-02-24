"""Tests for the PacketDetailActivity."""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.packet_detail import (
    PacketDetailActivity,
    _hex_dump,
    build_packet_detail_lines,
)
from collector.protocol import FRAME_TYPE_RX_RAW, FRAME_TYPE_TX_RAW


def _rx_frame(snr=5.0, rssi=-80, raw=None):
    """Create a minimal RX frame for testing."""
    if raw is None:
        # Header byte: route_type=1 (FLOOD), payload_type=5 (GRP_TXT), version=0
        # header = (5 << 2) | 1 = 0x15
        raw = bytes([0x15]) + b"\x00" * 19
    return {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": snr,
            "rssi": rssi,
            "route_type": 1,
            "route_name": "FLOOD",
            "payload_type": 5,
            "payload_name": "GRP_TXT",
            "header": raw[0],
            "version": 0,
            "raw_len": len(raw),
            "raw": raw,
        },
    }


def _tx_frame(raw=None):
    """Create a minimal TX frame for testing."""
    if raw is None:
        raw = bytes([0x09]) + b"\x00" * 14
    return {
        "type": FRAME_TYPE_TX_RAW,
        "received_at": time.time(),
        "parsed": {
            "route_type": 2,
            "route_name": "DIRECT",
            "payload_type": 2,
            "payload_name": "TXT_MSG",
            "header": raw[0],
            "version": 0,
            "raw_len": len(raw),
            "raw": raw,
        },
    }


class TestHexDump:
    def test_empty_data(self):
        lines = _hex_dump(b"")
        assert lines == ["  (no raw data)"]

    def test_short_data(self):
        lines = _hex_dump(bytes([0xC0, 0xD0, 0x41]))
        assert len(lines) == 1
        assert "C0 D0 41" in lines[0]
        assert "|..A|" in lines[0]

    def test_offset_formatting(self):
        lines = _hex_dump(bytes(range(32)))
        assert len(lines) == 2
        assert lines[0].startswith("  0000")
        assert lines[1].startswith("  0010")

    def test_ascii_printable(self):
        lines = _hex_dump(b"Hello World!")
        assert "|Hello World!|" in lines[0]

    def test_ascii_non_printable(self):
        lines = _hex_dump(bytes([0x00, 0x01, 0xFF]))
        assert "|...|" in lines[0]

    def test_full_line(self):
        data = bytes(range(16))
        lines = _hex_dump(data)
        assert len(lines) == 1
        # Should contain all 16 hex bytes
        assert "00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F" in lines[0]


class TestBuildPacketDetailLines:
    def test_rx_frame_shows_type(self):
        frame = _rx_frame()
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "RX_RAW" in text

    def test_rx_frame_shows_route(self):
        frame = _rx_frame()
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "FLOOD" in text

    def test_rx_frame_shows_payload(self):
        frame = _rx_frame()
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "GRP_TXT" in text

    def test_rx_frame_shows_snr(self):
        frame = _rx_frame(snr=3.5)
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "+3.5" in text

    def test_rx_frame_shows_rssi(self):
        frame = _rx_frame(rssi=-95)
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "-95" in text

    def test_rx_frame_shows_hex_dump(self):
        frame = _rx_frame(raw=bytes([0x15, 0xAA, 0xBB]))
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "Hex Dump" in text
        assert "15 AA BB" in text

    def test_rx_frame_shows_length(self):
        raw = bytes(42)
        frame = _rx_frame(raw=raw)
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "42 bytes" in text

    def test_tx_frame_shows_direction(self):
        frame = _tx_frame()
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "TX_RAW" in text

    def test_tx_frame_shows_route(self):
        frame = _tx_frame()
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "DIRECT" in text

    def test_header_byte_analysis(self):
        frame = _rx_frame()
        lines = build_packet_detail_lines(frame)
        text = "\n".join(lines)
        assert "Header Byte" in text
        assert "0x15" in text


class TestPacketDetailActivity:
    def test_shows_title(self, app, mock_screen):
        frame = _rx_frame()
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("Packet Detail")

    def test_shows_frame_type_in_subtitle(self, app, mock_screen):
        frame = _rx_frame()
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("RX")
        mock_screen.assert_text_on_screen("GRP_TXT")

    def test_shows_hex_dump(self, app, mock_screen):
        raw = bytes([0x15, 0xCA, 0xFE])
        frame = _rx_frame(raw=raw)
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("Hex Dump")
        mock_screen.assert_text_on_screen("15 CA FE")

    def test_shows_snr(self, app, mock_screen):
        frame = _rx_frame(snr=6.5)
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("+6.5")

    def test_shows_rssi(self, app, mock_screen):
        frame = _rx_frame(rssi=-102)
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("-102")

    def test_shows_route_type(self, app, mock_screen):
        frame = _rx_frame()
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("FLOOD")

    def test_esc_pops_activity(self, app, mock_screen):
        frame = _rx_frame()
        app.start_activity(PacketDetailActivity(frame=frame))
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_scroll_works(self, app, mock_screen):
        import curses
        raw = bytes(range(64))
        frame = _rx_frame(raw=raw)
        activity = PacketDetailActivity(frame=frame)
        app.start_activity(activity)
        initial = activity.display_state["content"]["selected_index"]
        app.send_key(curses.KEY_DOWN)
        assert activity.display_state["content"]["selected_index"] == initial + 1

    def test_tx_frame_title(self, app, mock_screen):
        frame = _tx_frame()
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("TX")

    def test_shows_bottom_bar_with_size(self, app, mock_screen):
        frame = _rx_frame(raw=bytes(20))
        app.start_activity(PacketDetailActivity(frame=frame))
        mock_screen.assert_text_on_screen("20 bytes")
