"""
Over-the-air system tests (single-board and two-board).

Single-board tests use the collector's ``collector send`` for transmit + echo.
Two-board tests: sender board transmits real LoRa messages via ``collector send``,
collector board captures them over the air. Both boards run the same repeater
firmware. Python host verifies frames arrive and are decoded correctly.

Run:
    MESHCORE_PORT=/dev/cu.usbserial-0001 \
    MESHCORE_SENDER_PORT=/dev/cu.usbserial-5 \
    python -m pytest collector/tests/system/test_radio.py -v
"""

import random
import secrets
import string
import threading
import time

import pytest

from collector.crypto import Channel, extract_group_payload, try_decode_group_message
from collector.protocol import (
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_RX_RAW,
    PAYLOAD_TYPE_GRP_TXT,
)

pytestmark = pytest.mark.system

RADIO_TIMEOUT = 15  # seconds — generous for LoRa latency


class TestCollectorSend:
    """Single-board tests for `collector send` — validates the full pipeline:
    CLI parse → encrypt → sendFlood → logRxRaw echo → frame → decrypt → store.

    The message IS transmitted over LoRa (sendFlood), but we verify the echo
    path back through logRxRaw, not reception by a second board.

    Only requires MESHCORE_PORT (single board).
    """

    def test_send_echoes_through_pipeline(self, hw, unique_tag):
        """collector send → firmware encrypts + sendFlood + logRxRaw echo → host decrypts."""
        core, fc = hw
        channel = Channel.from_hashtag("#test")
        core.set_channels([channel])

        success = core.send_channel_message("#test", "systest", unique_tag)
        assert success, "send_channel_message returned False"

        msg = fc.wait_for_channel_message(
            lambda m: unique_tag in m.text,
            timeout=RADIO_TIMEOUT,
        )
        assert msg is not None, f"Channel message with tag {unique_tag} not echoed back"
        assert msg.sender == "systest"
        assert unique_tag in msg.text

        # Verify stored in SQLite
        time.sleep(0.5)
        rows = core.store.get_channel_messages(channel_name="#test", limit=50)
        matching = [r for r in rows if unique_tag in (r.get("text") or "")]
        assert len(matching) >= 1, "Sent message not found in channel_messages table"


@pytest.mark.radio
class TestOverTheAir:
    """End-to-end radio tests: sender → air → collector → Python."""

    def test_message_captured_as_rx_raw(self, hw, sender, unique_tag):
        """Send a message from sender board, verify collector gets an RX_RAW frame."""
        core, fc = hw

        sender.write(
            f"collector send #radiotest sender {unique_tag}\r".encode()
        )

        frame = fc.wait_for_frame(
            lambda f: (
                f["type"] == FRAME_TYPE_RX_RAW
                and f.get("parsed", {}).get("payload_type") == PAYLOAD_TYPE_GRP_TXT
            ),
            timeout=RADIO_TIMEOUT,
        )
        assert frame is not None, "No RX_RAW/GRP_TXT frame received over the air"
        assert frame["parsed"]["raw_len"] > 0

    def test_message_decrypted(self, hw, sender, unique_tag):
        """Send a message, verify collector decrypts it and stores in SQLite."""
        core, fc = hw
        channel = Channel.from_hashtag("#radiotest")
        core.set_channels([channel])

        sender.write(
            f"collector send #radiotest sender {unique_tag}\r".encode()
        )

        msg = fc.wait_for_channel_message(
            lambda m: unique_tag in m.text,
            timeout=RADIO_TIMEOUT,
        )
        assert msg is not None, f"Channel message with tag {unique_tag} not received"
        assert msg.sender, "Sender name is empty"
        assert unique_tag in msg.text

        # Verify stored in SQLite
        time.sleep(0.5)
        rows = core.store.get_channel_messages(channel_name="#radiotest", limit=50)
        matching = [r for r in rows if unique_tag in (r.get("text") or "")]
        assert len(matching) >= 1, "Decrypted message not found in channel_messages table"

    def test_advertisement_captured(self, hw, sender):
        """Trigger an advertisement from the sender, verify collector receives it."""
        core, fc = hw

        sender.write(b"advert\r")

        frame = fc.wait_for_frame(
            lambda f: f["type"] == FRAME_TYPE_ADVERTISEMENT,
            timeout=RADIO_TIMEOUT,
        )
        assert frame is not None, "No ADVERTISEMENT frame received over the air"
        parsed = frame["parsed"]
        assert "pub_key" in parsed
        assert len(parsed["pub_key"]) == 32, f"pub_key wrong length: {len(parsed['pub_key'])}"


