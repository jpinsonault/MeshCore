"""Integration tests for channel decoding in CollectorCore._process_frame."""

import base64
import struct
import tempfile
import time
import pytest

from collector.core import CollectorCore
from collector.crypto import Channel, encrypt_then_mac, PAYLOAD_TYPE_GRP_TXT, ROUTE_TYPE_FLOOD
from collector.protocol import FRAME_TYPE_RX_RAW, FRAME_TYPE_TX_RAW

TEST_PSK = base64.b64encode(b"\x01" * 16).decode()


def _make_channel():
    return Channel.from_psk("TestChannel", TEST_PSK)


def _make_grp_txt_raw(channel, text, timestamp=1700000000):
    """Build raw packet bytes for a GRP_TXT message."""
    plaintext = struct.pack("<I", timestamp) + b"\x00" + text.encode("utf-8") + b"\x00"
    mac_and_data = encrypt_then_mac(channel.secret, plaintext)
    payload = bytes([channel.hash]) + mac_and_data
    header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
    return bytes([header, 0x00]) + payload


def _make_rx_frame(raw, payload_type=PAYLOAD_TYPE_GRP_TXT):
    """Build a parsed RX_RAW frame dict."""
    return {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": 5.0,
            "rssi": -80,
            "route_type": ROUTE_TYPE_FLOOD,
            "payload_type": payload_type,
            "raw": raw,
            "raw_len": len(raw),
        },
    }


@pytest.fixture
def core_with_store():
    """Create a CollectorCore with an open store but no serial connection."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        core = CollectorCore(port=None, db_path=f.name)
        core._store = __import__("collector.store", fromlist=["CollectorStore"]).CollectorStore(f.name)
        core._store.open()
        yield core
        core._store.close()


class TestProcessFrameChannelDecode:
    def test_decodes_grp_txt_and_fires_callback(self, core_with_store):
        ch = _make_channel()
        core_with_store.set_channels([ch])

        raw = _make_grp_txt_raw(ch, "Alice: Hello!")
        frame = _make_rx_frame(raw)

        received = []
        core_with_store.on_channel_message = lambda msg: received.append(msg)

        core_with_store._process_frame(frame)

        assert len(received) == 1
        assert received[0].sender == "Alice"
        assert received[0].text == "Hello!"
        assert received[0].channel_name == "TestChannel"

    def test_stores_decoded_message(self, core_with_store):
        ch = _make_channel()
        core_with_store.set_channels([ch])

        raw = _make_grp_txt_raw(ch, "Bob: Testing storage")
        frame = _make_rx_frame(raw)
        core_with_store._process_frame(frame)

        msgs = core_with_store._store.get_channel_messages()
        assert len(msgs) == 1
        assert msgs[0]["sender"] == "Bob"
        assert msgs[0]["text"] == "Testing storage"

    def test_no_channels_no_decode(self, core_with_store):
        """No channels configured — frame is stored but no channel decode."""
        ch = _make_channel()
        raw = _make_grp_txt_raw(ch, "Alice: ignored")
        frame = _make_rx_frame(raw)

        received = []
        core_with_store.on_channel_message = lambda msg: received.append(msg)

        core_with_store._process_frame(frame)

        assert len(received) == 0
        assert core_with_store._store.get_channel_message_count() == 0

    def test_wrong_channel_no_decode(self, core_with_store):
        """Channels set but none match — no decode, no callback."""
        ch_real = _make_channel()
        ch_wrong = Channel.from_psk("Wrong", base64.b64encode(b"\xFF" * 16).decode())
        core_with_store.set_channels([ch_wrong])

        raw = _make_grp_txt_raw(ch_real, "Alice: secret")
        frame = _make_rx_frame(raw)

        received = []
        core_with_store.on_channel_message = lambda msg: received.append(msg)

        core_with_store._process_frame(frame)

        assert len(received) == 0

    def test_non_grp_txt_not_decoded(self, core_with_store):
        """RX_RAW with payload_type != GRP_TXT should not attempt decode."""
        ch = _make_channel()
        core_with_store.set_channels([ch])

        raw = b"\x09\x00" + b"\x00" * 20  # payload_type=TXT_MSG
        frame = _make_rx_frame(raw, payload_type=0x02)

        received = []
        core_with_store.on_channel_message = lambda msg: received.append(msg)
        core_with_store._process_frame(frame)

        assert len(received) == 0

    def test_tx_frame_not_decoded(self, core_with_store):
        """TX_RAW frames should never attempt channel decode."""
        ch = _make_channel()
        core_with_store.set_channels([ch])

        frame = {
            "type": FRAME_TYPE_TX_RAW,
            "received_at": time.time(),
            "parsed": {
                "route_type": 1,
                "payload_type": PAYLOAD_TYPE_GRP_TXT,
                "raw": _make_grp_txt_raw(ch, "Alice: tx test"),
                "raw_len": 50,
            },
        }
        received = []
        core_with_store.on_channel_message = lambda msg: received.append(msg)
        core_with_store._process_frame(frame)

        assert len(received) == 0

    def test_raw_not_bytes(self, core_with_store):
        """If raw is not bytes (e.g. None or string), should not crash."""
        ch = _make_channel()
        core_with_store.set_channels([ch])

        for raw_val in [None, "not bytes", 42]:
            frame = {
                "type": FRAME_TYPE_RX_RAW,
                "received_at": time.time(),
                "parsed": {
                    "snr": 5.0,
                    "rssi": -80,
                    "route_type": 1,
                    "payload_type": PAYLOAD_TYPE_GRP_TXT,
                    "raw": raw_val,
                    "raw_len": 0,
                },
            }
            # Should not raise
            core_with_store._process_frame(frame)

    def test_missing_parsed_key(self, core_with_store):
        """Frame with no 'parsed' key should not crash channel decode."""
        ch = _make_channel()
        core_with_store.set_channels([ch])

        frame = {
            "type": FRAME_TYPE_RX_RAW,
            "received_at": time.time(),
            "parsed": None,
        }
        # Should not raise
        core_with_store._process_frame(frame)

    def test_multiple_frames_decoded_in_sequence(self, core_with_store):
        """Three GRP_TXT frames in a row should each fire callback."""
        ch = _make_channel()
        core_with_store.set_channels([ch])

        received = []
        core_with_store.on_channel_message = lambda msg: received.append(msg)

        for i in range(3):
            raw = _make_grp_txt_raw(ch, f"User{i}: Message {i}", timestamp=1700000000 + i)
            frame = _make_rx_frame(raw)
            core_with_store._process_frame(frame)

        assert len(received) == 3
        assert received[0].sender == "User0"
        assert received[1].sender == "User1"
        assert received[2].sender == "User2"

    def test_no_callback_set(self, core_with_store):
        """If on_channel_message is None, decode still stores but doesn't crash."""
        ch = _make_channel()
        core_with_store.set_channels([ch])
        core_with_store.on_channel_message = None

        raw = _make_grp_txt_raw(ch, "Alice: no callback")
        frame = _make_rx_frame(raw)
        core_with_store._process_frame(frame)

        # Message should still be stored
        assert core_with_store._store.get_channel_message_count() == 1


class TestSendChannelMessage:
    def test_sends_correct_command(self):
        """send_channel_message builds the right CLI command."""
        from unittest.mock import MagicMock
        core = CollectorCore(port=None)
        core._ser = MagicMock()
        core._ser.is_open = True
        core._ser.write = MagicMock()

        result = core.send_channel_message("#test", "alice", "hello world")
        assert result is True
        core._ser.write.assert_called_once()
        written = core._ser.write.call_args[0][0]
        assert written == b"collector send #test alice hello world\r"

    def test_adds_hash_prefix(self):
        """If channel name lacks #, it is added."""
        from unittest.mock import MagicMock
        core = CollectorCore(port=None)
        core._ser = MagicMock()
        core._ser.is_open = True
        core._ser.write = MagicMock()

        core.send_channel_message("test", "bob", "hi")
        written = core._ser.write.call_args[0][0]
        assert written == b"collector send #test bob hi\r"

    def test_returns_false_when_disconnected(self):
        """Returns False when no serial connection."""
        core = CollectorCore(port=None)
        result = core.send_channel_message("#test", "alice", "hello")
        assert result is False

    def test_returns_false_on_serial_error(self):
        """Returns False when serial write raises."""
        from unittest.mock import MagicMock
        import serial
        core = CollectorCore(port=None)
        core._ser = MagicMock()
        core._ser.is_open = True
        core._ser.write.side_effect = serial.SerialException("disconnected")

        result = core.send_channel_message("#test", "alice", "hello")
        assert result is False
