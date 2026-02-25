"""Tests for CollectorServer serialization and routing."""

import json
import time
import pytest

from collector.server import _serialize_frame, _serialize_channel_message, CollectorServer
from collector.protocol import FRAME_TYPE_RX_RAW, FRAME_TYPE_HEARTBEAT


class FakeMsg:
    """Minimal channel message stand-in."""
    def __init__(self, sender="Alice", text="Hello", channel_name="Public",
                 channel_hash=0xAA, timestamp=1700000000):
        self.sender = sender
        self.text = text
        self.channel_name = channel_name
        self.channel_hash = channel_hash
        self.timestamp = timestamp


class TestSerializeFrame:
    def test_rx_frame_with_bytes(self):
        frame = {
            "type": FRAME_TYPE_RX_RAW,
            "parsed": {
                "snr": 5.5,
                "rssi": -80,
                "raw": b"\xDE\xAD\xBE\xEF",
                "route_type": 0,
                "payload_type": 5,
            },
        }
        result = _serialize_frame(frame)
        assert result["type_name"] == "RX_RAW"
        assert result["parsed"]["raw"] == "deadbeef"
        assert result["parsed"]["snr"] == 5.5

    def test_heartbeat_frame(self):
        frame = {
            "type": FRAME_TYPE_HEARTBEAT,
            "parsed": {
                "battery_mv": 3700,
                "uptime_secs": 3600,
            },
        }
        result = _serialize_frame(frame)
        assert result["type_name"] == "HEARTBEAT"
        assert result["parsed"]["battery_mv"] == 3700

    def test_unknown_type(self):
        frame = {"type": 0xFF, "parsed": {"foo": "bar"}}
        result = _serialize_frame(frame)
        assert result["type_name"] == "UNKNOWN"

    def test_no_parsed(self):
        frame = {"type": FRAME_TYPE_RX_RAW}
        result = _serialize_frame(frame)
        assert "parsed" not in result

    def test_result_is_json_serializable(self):
        frame = {
            "type": FRAME_TYPE_RX_RAW,
            "parsed": {"raw": b"\x00\x01\x02", "name": "test"},
        }
        result = _serialize_frame(frame)
        dumped = json.dumps(result)
        assert "000102" in dumped


class TestSerializeChannelMessage:
    def test_basic(self):
        msg = FakeMsg()
        result = _serialize_channel_message(msg)
        assert result["sender"] == "Alice"
        assert result["text"] == "Hello"
        assert result["channel_name"] == "Public"
        assert result["channel_hash"] == 0xAA
        assert result["timestamp"] == 1700000000

    def test_json_serializable(self):
        msg = FakeMsg(text="Hello\nWorld")
        result = _serialize_channel_message(msg)
        dumped = json.dumps(result)
        assert "Hello" in dumped


class TestCollectorServerInit:
    def test_default_ports(self):
        server = CollectorServer(core=None)
        assert server.url == "http://0.0.0.0:8080"
        assert server.ws_url == "ws://0.0.0.0:8081"
        assert not server.is_running

    def test_custom_ports(self):
        server = CollectorServer(core=None, http_port=9090, ws_port=9091, host="127.0.0.1")
        assert server.url == "http://127.0.0.1:9090"
        assert server.ws_url == "ws://127.0.0.1:9091"


class TestCallbackChaining:
    def test_chain_preserves_original(self):
        """Chaining should call both original and new handler."""
        calls = []

        class FakeCore:
            on_frame = lambda self, f: None
            on_text = None
            on_connected = None
            on_disconnected = None
            on_channel_message = None
            is_connected = False
            port = "/dev/null"

        core = FakeCore()
        core.on_frame = lambda f: calls.append(("orig", f))

        server = CollectorServer(core, http_port=0, ws_port=0)
        server._chain_callbacks()

        # Call the chained callback
        core.on_frame({"type": 0xD0, "parsed": {}})
        assert ("orig", {"type": 0xD0, "parsed": {}}) in calls

    def test_restore_callbacks(self):
        class FakeCore:
            on_frame = None
            on_text = None
            on_connected = None
            on_disconnected = None
            on_channel_message = None

        core = FakeCore()
        original = lambda f: None
        core.on_frame = original

        server = CollectorServer(core, http_port=0, ws_port=0)
        server._chain_callbacks()
        assert core.on_frame is not original

        server._restore_callbacks()
        assert core.on_frame is original
