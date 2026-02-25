"""
System tests — hardware-in-the-loop.

Each test injects traffic via the firmware CLI and verifies the full pipeline:
firmware encrypt → ring buffer → USB serial → Python parse → SQLite store.

Run:
    MESHCORE_PORT=/dev/cu.usbserial-0001 python -m pytest collector/tests/system/ -v
"""

import time

import pytest

from collector.crypto import Channel
from collector.cracker import ChannelCracker
from collector.protocol import (
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_DIAGNOSTICS,
    FRAME_TYPE_RX_RAW,
    PAYLOAD_TYPE_GRP_TXT,
)

pytestmark = pytest.mark.system


class TestInjectPipeline:
    """Tests that exercise the inject → RX_RAW → decode pipeline."""

    def test_inject_produces_rx_raw_frame(self, hw, unique_tag):
        """Inject a message and verify an RX_RAW frame with GRP_TXT payload arrives."""
        core, fc = hw
        core.send_command(f"collector inject #test sysbot {unique_tag}\r")

        frame = fc.wait_for_frame(
            lambda f: (
                f["type"] == FRAME_TYPE_RX_RAW
                and f.get("parsed", {}).get("payload_type") == PAYLOAD_TYPE_GRP_TXT
            ),
            timeout=10,
        )
        assert frame is not None, "No RX_RAW/GRP_TXT frame received"
        assert frame["parsed"]["raw_len"] > 0

        # Verify it was stored in SQLite
        time.sleep(0.5)
        packets = core.store.get_recent_packets(limit=10, payload_type=PAYLOAD_TYPE_GRP_TXT)
        assert len(packets) >= 1, "GRP_TXT packet not found in raw_packets table"

    def test_inject_decoded_to_channel_message(self, hw, unique_tag):
        """Configure #test channel, inject, verify on_channel_message fires and DB stores it."""
        core, fc = hw
        channel = Channel.from_hashtag("#test")
        core.set_channels([channel])

        core.send_command(f"collector inject #test sysbot {unique_tag}\r")

        msg = fc.wait_for_channel_message(
            lambda m: unique_tag in m.text,
            timeout=10,
        )
        assert msg is not None, f"Channel message with tag {unique_tag} not received"
        assert msg.sender == "sysbot"
        assert msg.channel_name == "#test"

        # Verify in SQLite
        time.sleep(0.5)
        rows = core.store.get_channel_messages(channel_name="#test", limit=50)
        matching = [r for r in rows if unique_tag in (r.get("text") or "")]
        assert len(matching) >= 1, "Channel message not found in DB"

    def test_multiple_injections_all_stored(self, hw, unique_tag):
        """Inject 5 messages on a known channel, verify all 5 are decoded and stored."""
        core, fc = hw
        channel = Channel.from_hashtag("#test")
        core.set_channels([channel])

        tags = [f"{unique_tag}-{i}" for i in range(5)]
        for tag in tags:
            core.send_command(f"collector inject #test sysbot {tag}\r")
            time.sleep(0.5)

        # Wait for the last one
        msg = fc.wait_for_channel_message(
            lambda m: tags[-1] in m.text,
            timeout=15,
        )
        assert msg is not None, "Last injected message not received"

        # Verify all 5 in DB
        time.sleep(0.5)
        rows = core.store.get_channel_messages(channel_name="#test", limit=100)
        for tag in tags:
            matching = [r for r in rows if tag in (r.get("text") or "")]
            assert len(matching) >= 1, f"Message with tag {tag} not found in DB"


class TestDeviceCommands:
    """Tests that exercise firmware CLI commands and verify response frames."""

    def test_heartbeat_on_status_command(self, hw):
        """Send 'collector status' and verify a heartbeat frame arrives."""
        core, fc = hw
        core.send_command("collector status\r")

        frame = fc.wait_for_frame(
            lambda f: f["type"] == FRAME_TYPE_HEARTBEAT,
            timeout=10,
        )
        assert frame is not None, "No heartbeat frame received after 'collector status'"
        parsed = frame["parsed"]
        assert "uptime_secs" in parsed
        assert "free_pkts" in parsed
        assert parsed["uptime_secs"] >= 0

    def test_diagnostics_on_diag_command(self, hw):
        """Send 'collector diag' and verify a diagnostics frame arrives."""
        core, fc = hw
        core.send_command("collector diag\r")

        frame = fc.wait_for_frame(
            lambda f: f["type"] == FRAME_TYPE_DIAGNOSTICS,
            timeout=10,
        )
        assert frame is not None, "No diagnostics frame received after 'collector diag'"
        parsed = frame["parsed"]
        assert "free_heap" in parsed
        assert "mcu_temp" in parsed
        assert parsed["free_heap"] > 0


class TestSequencing:
    """Tests for the v2 reliable delivery sequence numbering."""

    def test_sequence_numbers_increase(self, hw, unique_tag):
        """Inject 3 messages rapidly and verify sequence numbers are monotonically increasing."""
        core, fc = hw
        channel = Channel.from_hashtag("#test")
        core.set_channels([channel])

        for i in range(3):
            core.send_command(f"collector inject #test sysbot {unique_tag}-seq{i}\r")
            time.sleep(0.3)

        # Wait for last frame
        fc.wait_for_channel_message(
            lambda m: f"{unique_tag}-seq2" in m.text,
            timeout=10,
        )

        # Collect all RX_RAW frames with sequence numbers
        rx_frames = [
            f for f in fc.frames
            if f["type"] == FRAME_TYPE_RX_RAW and f.get("seq") is not None
        ]
        assert len(rx_frames) >= 3, f"Expected >=3 RX_RAW frames, got {len(rx_frames)}"

        seqs = [f["seq"] for f in rx_frames]
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1], (
                f"Sequence numbers not monotonically increasing: {seqs}"
            )


class TestCracker:
    """Cracker test — runs last since it modifies core state heavily."""

    def test_cracker_discovers_injected_channel(self, hw, unique_tag):
        """Inject on #hiking (in builtin wordlist, NOT pre-configured) — cracker should find it."""
        core, fc = hw
        # No channels configured — forces packets through the cracker path
        core.set_channels([])

        cracker = ChannelCracker(core.store)
        discovered = []

        def on_discovered(name, count):
            discovered.append((name, count))

        cracker.on_channel_discovered = on_discovered
        core.set_cracker(cracker)
        cracker.start()

        try:
            core.send_command(f"collector inject #hiking sysbot {unique_tag}\r")

            # Wait for cracker to discover the channel
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if any(name == "#hiking" for name, _ in discovered):
                    break
                time.sleep(0.5)

            assert any(name == "#hiking" for name, _ in discovered), (
                f"Cracker did not discover #hiking. Pending: {cracker.pending_hashes}"
            )

            # Verify retroactive decode stored the message
            rows = core.store.get_channel_messages(channel_name="#hiking", limit=50)
            matching = [r for r in rows if unique_tag in (r.get("text") or "")]
            assert len(matching) >= 1, "Cracker did not retroactively decode the message"
        finally:
            # Stop cracker thread first, then detach from core
            cracker.stop()
            # Direct attribute set avoids the race in set_cracker() —
            # by this point the cracker thread is joined and there are
            # no GRP_TXT packets in flight from our inject
            core._cracker = None
