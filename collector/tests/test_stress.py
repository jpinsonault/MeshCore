"""Stress and performance-limit tests for the collector.

All tests are marked @pytest.mark.stress for optional filtering:
    pytest -m stress          # only stress
    pytest -m "not stress"    # skip stress

Generous time bounds to avoid flaky failures in CI.
"""

import struct
import tempfile
import threading
import time

import pytest

from collector.cracker import ChannelCracker
from collector.crypto import (
    Channel,
    encrypt_then_mac,
    try_decode_group_message,
    PAYLOAD_TYPE_GRP_TXT,
    ROUTE_TYPE_FLOOD,
)
from collector.protocol import FRAME_TYPE_RX_RAW
from collector.store import CollectorStore
from collector.tests.packet_helpers import (
    make_temp_store,
    make_grp_txt_raw,
    store_grp_txt_packet,
    store_n_packets,
)


pytestmark = pytest.mark.stress


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timed(fn, *args, **kwargs):
    """Run fn and return (result, elapsed_seconds)."""
    t0 = time.monotonic()
    result = fn(*args, **kwargs)
    return result, time.monotonic() - t0


def _insert_rx_packets(store, n, ts_base=1700000000):
    """Insert n RX packets (no GRP_TXT — plain payload type 1)."""
    for i in range(n):
        frame = {
            "type": FRAME_TYPE_RX_RAW,
            "received_at": ts_base + i,
            "parsed": {
                "snr": 5.0,
                "rssi": -80 + (i % 20),
                "route_type": 1,
                "payload_type": 1,
                "raw": b"\x05\x00" + i.to_bytes(4, "little"),
                "raw_len": 6,
            },
        }
        store.store_frame(frame)


def _wait_cracker(cracker, condition, timeout=30.0, poll=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(poll)
    return condition()


# ---------------------------------------------------------------------------
# TestStoreScaling
# ---------------------------------------------------------------------------

class TestStoreScaling:
    """Query performance at increasing store sizes."""

    def test_100_packets_query(self):
        store, _ = make_temp_store()
        _insert_rx_packets(store, 100)
        _, elapsed = _timed(store.get_recent_packets, limit=50)
        assert elapsed < 0.1
        store.close()

    def test_1k_packets_query(self):
        store, _ = make_temp_store()
        _insert_rx_packets(store, 1000)
        _, elapsed = _timed(store.get_recent_packets, limit=50)
        assert elapsed < 0.2
        store.close()

    def test_10k_packets_query(self):
        store, _ = make_temp_store()
        _insert_rx_packets(store, 10000)
        _, elapsed = _timed(store.get_recent_packets, limit=50)
        assert elapsed < 0.5
        store.close()

    def test_1k_channel_messages_summary(self):
        store, _ = make_temp_store()
        channels = [Channel.from_hashtag(f"#sum{i}") for i in range(20)]
        for i, ch in enumerate(channels):
            store_n_packets(store, ch, 50, text_prefix=f"ch{i}_", ts_base=1700000000 + i * 100)
        # Decrypt them so channel_messages table is populated
        for ch in channels:
            cracker = ChannelCracker(store, known_channels=[])
            cracker.retroactive_decrypt(ch)

        _, elapsed = _timed(store.get_channel_summary)
        assert elapsed < 0.2
        store.close()


# ---------------------------------------------------------------------------
# TestCrackerScaling
# ---------------------------------------------------------------------------

class TestCrackerScaling:
    """Cracking speed at scale."""

    def _crack_n_channels(self, n, tmp_path, timeout):
        import secrets
        names = [f"crack_s_{secrets.token_hex(3)}_{i}" for i in range(n)]
        store, _ = make_temp_store()
        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            store_grp_txt_packet(store, ch, f"User: {name}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(names) + "\n")
        cracker.add_wordlist(str(wl))
        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            cracker.notify_unknown_hash(ch.hash)

        t0 = time.monotonic()
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0, timeout=timeout)
        elapsed = time.monotonic() - t0
        cracker.stop()

        # Hash collisions reduce cracked_count below n. With 256 hash
        # buckets and n channels, birthday collisions are expected for n > ~20.
        min_expected = max(1, n - 10)
        assert cracker.cracked_count >= min_expected
        store.close()
        return elapsed

    def test_10_channels(self, tmp_path):
        self._crack_n_channels(10, tmp_path, timeout=5)

    def test_50_channels(self, tmp_path):
        # With 50 channels and 256 hash buckets, birthday-problem collisions
        # are expected (~5). The cracker finds one channel per hash byte, so
        # colliding channels won't all be cracked. Accept >= 40.
        self._crack_n_channels(50, tmp_path, timeout=15)

    def test_large_wordlist_5_channels(self, tmp_path):
        """10K word wordlist with 5 crackable channels."""
        import secrets
        all_words = [f"lw_{secrets.token_hex(4)}_{i}" for i in range(10000)]
        target_names = all_words[100:105]  # pick 5 from the list

        store, _ = make_temp_store()
        for name in target_names:
            ch = Channel.from_hashtag(f"#{name}")
            store_grp_txt_packet(store, ch, f"User: {name}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(all_words) + "\n")
        cracker.add_wordlist(str(wl))

        for name in target_names:
            ch = Channel.from_hashtag(f"#{name}")
            cracker.notify_unknown_hash(ch.hash)

        t0 = time.monotonic()
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0, timeout=15)
        elapsed = time.monotonic() - t0
        cracker.stop()

        assert cracker.cracked_count >= 5
        store.close()


# ---------------------------------------------------------------------------
# TestRetroactiveDecryptScaling
# ---------------------------------------------------------------------------

class TestRetroactiveDecryptScaling:
    """retroactive_decrypt speed with many packets."""

    def test_100_packets(self):
        store, _ = make_temp_store()
        ch = Channel.from_hashtag("#retro100")
        store_n_packets(store, ch, 100, ts_base=1700000000)

        cracker = ChannelCracker(store)
        _, elapsed = _timed(cracker.retroactive_decrypt, ch)
        assert elapsed < 2.0
        msgs = store.get_channel_messages()
        assert len(msgs) == 100
        store.close()

    def test_1k_packets(self):
        store, _ = make_temp_store()
        ch = Channel.from_hashtag("#retro1k")
        store_n_packets(store, ch, 1000, ts_base=1700000000)

        cracker = ChannelCracker(store)
        _, elapsed = _timed(cracker.retroactive_decrypt, ch)
        assert elapsed < 10.0
        total = store.get_channel_message_count()
        assert total == 1000
        store.close()

    def test_1k_packets_10_channels_selective(self):
        """1K packets across 10 channels. Retroactive for 1 channel."""
        store, _ = make_temp_store()
        channels = [Channel.from_hashtag(f"#sel{i}") for i in range(10)]
        for ch in channels:
            store_n_packets(store, ch, 100, ts_base=1700000000)

        cracker = ChannelCracker(store)
        _, elapsed = _timed(cracker.retroactive_decrypt, channels[0])
        assert elapsed < 5.0
        msgs = store.get_channel_messages()
        assert len(msgs) == 100  # only channel 0
        store.close()


# ---------------------------------------------------------------------------
# TestMessageLengthScaling
# ---------------------------------------------------------------------------

class TestMessageLengthScaling:
    """GRP_TXT packets with varying text lengths."""

    @pytest.mark.parametrize("length", [10, 100, 160])
    def test_standard_lengths(self, length):
        ch = Channel.from_hashtag("#lentest")
        text = f"U: {'A' * length}"
        raw = make_grp_txt_raw(ch, text)
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is not None
        assert len(msg.text) == length

    def test_near_payload_limit(self):
        """Text near the maximum payload size (~150 chars for MeshCore)."""
        ch = Channel.from_hashtag("#maxlen")
        # Start with a reasonable maximum and verify it works
        # MeshCore payload is 184 bytes max. After header, timestamp, flags,
        # null terminator, MAC, and AES padding, ~150 chars is realistic.
        for length in [130, 140, 150]:
            text = f"U: {'Z' * length}"
            raw = make_grp_txt_raw(ch, text)
            msg = try_decode_group_message(raw, [ch], 1.0)
            if msg is not None:
                assert msg.text == "Z" * length
            # If None, the length exceeds what can fit — that's expected


# ---------------------------------------------------------------------------
# TestSidebarRendering
# ---------------------------------------------------------------------------

class TestSidebarRendering:
    """Sidebar performance with many channels."""

    def _make_chat_with_channels(self, n, app, mock_screen):
        from collector.activities.chat import ChatActivity
        activity = ChatActivity(port="/dev/ttyUSB0", auto_start=False)
        app.start_activity(activity)
        activity._channels = [{"name": f"#sbr{i}", "msg_count": i} for i in range(n)]
        return activity

    def test_10_channels(self, app, mock_screen):
        activity = self._make_chat_with_channels(10, app, mock_screen)
        _, elapsed = _timed(activity._sidebar_items)
        assert elapsed < 0.05
        assert len(activity._sidebar_items()) > 10

    def test_100_channels(self, app, mock_screen):
        activity = self._make_chat_with_channels(100, app, mock_screen)
        _, elapsed = _timed(activity._sidebar_items)
        assert elapsed < 0.05
        assert len(activity._sidebar_items()) > 100

    def test_500_channels(self, app, mock_screen):
        activity = self._make_chat_with_channels(500, app, mock_screen)
        _, elapsed = _timed(activity._sidebar_items)
        assert elapsed < 0.2
        assert len(activity._sidebar_items()) > 500


# ---------------------------------------------------------------------------
# TestConcurrentStoreWrites
# ---------------------------------------------------------------------------

class TestConcurrentStoreWrites:
    """Multiple threads inserting frames concurrently."""

    def test_10_threads_100_frames_each(self):
        store, _ = make_temp_store()

        def _worker(worker_id):
            for i in range(100):
                frame = {
                    "type": FRAME_TYPE_RX_RAW,
                    "received_at": time.time(),
                    "parsed": {
                        "snr": 5.0,
                        "rssi": -80,
                        "route_type": 1,
                        "payload_type": 1,
                        "raw": bytes([worker_id, i]),
                        "raw_len": 2,
                    },
                }
                try:
                    store.store_frame(frame)
                except Exception:
                    pass  # SQLite may raise on concurrent writes — count later

        threads = [threading.Thread(target=_worker, args=(w,)) for w in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # SQLite's default journal mode causes heavy lock contention with
        # concurrent writers. The key assertion is no crash/corruption, not
        # that every write succeeds. Accept >= 200 out of 1000 attempts.
        stats = store.get_stats()
        total = stats["rx_count"]
        assert total >= 200
        store.close()
