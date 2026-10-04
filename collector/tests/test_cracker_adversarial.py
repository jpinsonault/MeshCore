"""Adversarial and edge-case tests for ChannelCracker.

Covers: incremental progress tracking, large wordlists, hash collisions,
out-of-order packet arrival, pause/resume, malformed wordlists, cache
persistence, and concurrent notify.
"""

import os
import tempfile
import threading
import time

import pytest

from collector.cracker import ChannelCracker, BUILTIN_WORDLIST
from collector.crypto import Channel, GroupMessage
from collector.store import CollectorStore
from collector.tests.packet_helpers import (
    make_temp_store,
    store_grp_txt_packet,
    store_n_packets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_cracker(cracker, condition, timeout=10.0, poll=0.05):
    """Poll until condition() is True or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(poll)
    return condition()


# ---------------------------------------------------------------------------
# TestIncrementalProgress
# ---------------------------------------------------------------------------

class TestIncrementalProgress:
    """State tracking through the full cracker lifecycle."""

    def test_ten_channels_cracked_sequentially(self, tmp_path):
        """Store packets for 10 custom channels, crack all, verify counts."""
        store, _ = make_temp_store()
        names = [f"incr_seq_{i}" for i in range(10)]
        channels = []
        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            channels.append(ch)
            store_grp_txt_packet(store, ch, f"User: msg on {name}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(names) + "\n")
        cracker.add_wordlist(str(wl))

        for ch in channels:
            cracker.notify_unknown_hash(ch.hash)

        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()

        assert cracker.cracked_count >= 10
        for name in names:
            assert f"#{name}" in cracker.cracked_names
        assert cracker.pending_count == 0
        store.close()

    def test_status_accuracy_at_each_step(self, tmp_path):
        """Verify status() dict at each lifecycle phase."""
        store, _ = make_temp_store()
        names = ["stat_a", "stat_b", "stat_c"]
        channels = []
        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            channels.append(ch)
            store_grp_txt_packet(store, ch, f"User: {name}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(names) + "\n")
        cracker.add_wordlist(str(wl))

        # Phase 1: initial
        st = cracker.status()
        assert st["running"] is False
        assert st["cracked_count"] == 0

        # Phase 2: after notify
        for ch in channels:
            cracker.notify_unknown_hash(ch.hash)
        st = cracker.status()
        assert st["pending_count"] == 3

        # Phase 3: after start + completion
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()

        st = cracker.status()
        assert st["running"] is False
        assert st["cracked_count"] >= 3
        for name in names:
            assert f"#{name}" in st["cracked_names"]
        store.close()

    def test_callback_fires_for_each_discovery(self, tmp_path):
        """Attach callback, crack 5 channels, assert 5 callbacks."""
        store, _ = make_temp_store()
        names = [f"cb_{i}" for i in range(5)]
        channels = []
        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            channels.append(ch)
            store_grp_txt_packet(store, ch, f"User: {name}")

        discovered = []
        cracker = ChannelCracker(store)
        cracker.on_channel_discovered = lambda n, c: discovered.append((n, c))

        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(names) + "\n")
        cracker.add_wordlist(str(wl))

        for ch in channels:
            cracker.notify_unknown_hash(ch.hash)

        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()

        assert len(discovered) >= 5
        discovered_names = {d[0] for d in discovered}
        for name in names:
            assert f"#{name}" in discovered_names
        store.close()


# ---------------------------------------------------------------------------
# TestLargeWordlists
# ---------------------------------------------------------------------------

class TestLargeWordlists:
    """Scaling the hash table with increasing wordlist sizes."""

    def _crack_random_word(self, word_count, target_idx, tmp_path, timeout):
        import secrets
        words = [f"wl_{secrets.token_hex(4)}_{i}" for i in range(word_count)]
        target = words[target_idx]
        store, _ = make_temp_store()
        ch = Channel.from_hashtag(f"#{target}")
        store_grp_txt_packet(store, ch, f"User: {target}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(words) + "\n")
        cracker.add_wordlist(str(wl))

        assert cracker.wordlist_size >= word_count  # includes builtins

        cracker.notify_unknown_hash(ch.hash)
        cracker.start()
        t0 = time.monotonic()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0, timeout=timeout)
        elapsed = time.monotonic() - t0
        cracker.stop()

        assert f"#{target}" in cracker.cracked_names
        store.close()
        return elapsed

    def test_100_words(self, tmp_path):
        self._crack_random_word(100, 50, tmp_path, timeout=5)

    def test_1000_words(self, tmp_path):
        self._crack_random_word(1000, 500, tmp_path, timeout=10)

    def test_10000_words(self, tmp_path):
        self._crack_random_word(10000, 5000, tmp_path, timeout=30)


# ---------------------------------------------------------------------------
# TestHashCollisions
# ---------------------------------------------------------------------------

class TestHashCollisions:
    """Test behaviour when multiple hashtag channels share a hash byte."""

    @staticmethod
    def _find_collisions(count=2):
        """Find 'count' builtin words whose Channel.from_hashtag hash byte collides."""
        from collections import defaultdict
        hash_to_words = defaultdict(list)
        for word in BUILTIN_WORDLIST:
            ch = Channel.from_hashtag(f"#{word}")
            hash_to_words[ch.hash].append(word)
        for h, words in hash_to_words.items():
            if len(words) >= count:
                return words[:count]
        # Generate collisions if none found in builtin
        import secrets
        target = Channel.from_hashtag("#general")
        found = ["general"]
        for _ in range(100000):
            word = f"col_{secrets.token_hex(4)}"
            ch = Channel.from_hashtag(f"#{word}")
            if ch.hash == target.hash and word != "general":
                found.append(word)
                if len(found) >= count:
                    return found
        pytest.skip("Could not find hash collisions")

    def test_two_colliding_channels_both_have_packets(self, tmp_path):
        words = self._find_collisions(2)
        ch_a = Channel.from_hashtag(f"#{words[0]}")
        ch_b = Channel.from_hashtag(f"#{words[1]}")
        assert ch_a.hash == ch_b.hash

        store, _ = make_temp_store()
        store_grp_txt_packet(store, ch_a, f"User: msg on {words[0]}")
        store_grp_txt_packet(store, ch_b, f"User: msg on {words[1]}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(words) + "\n")
        cracker.add_wordlist(str(wl))

        cracker.notify_unknown_hash(ch_a.hash)
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()

        # At least one should be cracked (both share the hash byte)
        assert cracker.cracked_count >= 1
        msgs = store.get_channel_messages()
        assert len(msgs) >= 1
        store.close()

    def test_collision_only_one_has_packets(self, tmp_path):
        words = self._find_collisions(2)
        ch_a = Channel.from_hashtag(f"#{words[0]}")
        ch_b = Channel.from_hashtag(f"#{words[1]}")

        store, _ = make_temp_store()
        # Only store packets for channel A
        store_grp_txt_packet(store, ch_a, f"User: msg on {words[0]}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(words) + "\n")
        cracker.add_wordlist(str(wl))

        cracker.notify_unknown_hash(ch_a.hash)
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()

        # Channel A should be cracked (has matching packets)
        assert f"#{words[0]}" in cracker.cracked_names
        msgs = store.get_channel_messages()
        assert len(msgs) >= 1
        store.close()

    def test_three_way_collision(self, tmp_path):
        words = self._find_collisions(3)
        channels = [Channel.from_hashtag(f"#{w}") for w in words]
        assert channels[0].hash == channels[1].hash == channels[2].hash

        store, _ = make_temp_store()
        # Only store packets for the first channel
        store_grp_txt_packet(store, channels[0], f"User: msg on {words[0]}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(words) + "\n")
        cracker.add_wordlist(str(wl))

        cracker.notify_unknown_hash(channels[0].hash)
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()

        assert cracker.cracked_count >= 1
        assert f"#{words[0]}" in cracker.cracked_names
        store.close()


# ---------------------------------------------------------------------------
# TestOutOfOrder
# ---------------------------------------------------------------------------

class TestOutOfOrder:
    """Post-crack packet arrival and repeated retroactive decrypt."""

    def test_retroactive_after_new_packets(self):
        store, _ = make_temp_store()
        ch = Channel.from_hashtag("#hiking")
        store_n_packets(store, ch, 3, text_prefix="early", ts_base=1700000000)

        cracker = ChannelCracker(store)
        count = cracker.retroactive_decrypt(ch)
        assert count == 3

        # Add 2 more packets after initial crack
        store_n_packets(store, ch, 2, text_prefix="late", ts_base=1700000010)
        count2 = cracker.retroactive_decrypt(ch)
        assert count2 == 2

        # Total decoded
        msgs = store.get_channel_messages()
        assert len(msgs) == 5
        store.close()

    def test_continuous_flow(self):
        store, _ = make_temp_store()
        ch = Channel.from_hashtag("#mesh")

        cracker = ChannelCracker(store)
        total = 0
        for i in range(5):
            store_grp_txt_packet(store, ch, f"User: flow{i}", timestamp=1700000000 + i)
            count = cracker.retroactive_decrypt(ch)
            assert count == 1
            total += count

        assert total == 5
        msgs = store.get_channel_messages()
        assert len(msgs) == 5
        store.close()


# ---------------------------------------------------------------------------
# TestPauseResume
# ---------------------------------------------------------------------------

class TestPauseResume:
    """Start/stop lifecycle edge cases."""

    def test_stop_then_resume(self, tmp_path):
        store, _ = make_temp_store()
        names = [f"pr_{i}" for i in range(5)]
        channels = []
        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            channels.append(ch)
            store_grp_txt_packet(store, ch, f"User: {name}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(names) + "\n")
        cracker.add_wordlist(str(wl))

        for ch in channels:
            cracker.notify_unknown_hash(ch.hash)

        cracker.start()
        # Wait for at least 1 crack
        _wait_cracker(cracker, lambda: cracker.cracked_count >= 1, timeout=5)
        cracker.stop()
        partial = cracker.cracked_count
        assert partial >= 1

        # Resume
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0, timeout=10)
        cracker.stop()

        assert cracker.cracked_count >= 5
        store.close()

    def test_double_stop_is_noop(self):
        store, _ = make_temp_store()
        cracker = ChannelCracker(store)
        cracker.start()
        cracker.stop()
        cracker.stop()  # second stop should not error
        assert cracker.running is False
        store.close()

    def test_start_with_nothing_pending(self):
        store, _ = make_temp_store()
        cracker = ChannelCracker(store)
        cracker.start()
        time.sleep(0.5)
        cracker.stop()
        assert cracker.cracked_count == 0
        store.close()


# ---------------------------------------------------------------------------
# TestMalformedWordlists
# ---------------------------------------------------------------------------

class TestMalformedWordlists:
    """Edge cases in wordlist file contents."""

    def test_blank_lines(self, tmp_path):
        store, _ = make_temp_store()
        cracker = ChannelCracker(store)
        size_before = cracker.wordlist_size

        wl = tmp_path / "blanks.txt"
        wl.write_text("\n\nword1_blanks\n\n\nword2_blanks\n")
        cracker.add_wordlist(str(wl))
        store.close()

        # Should have added exactly 2 new words
        assert cracker.wordlist_size >= size_before + 2

    def test_duplicate_words(self, tmp_path):
        store, _ = make_temp_store()
        cracker = ChannelCracker(store)

        wl = tmp_path / "dups.txt"
        wl.write_text("myword_dup\n" * 5)
        cracker.add_wordlist(str(wl))
        store.close()

        # All duplicates map to the same hash bucket — check the bucket
        ch = Channel.from_hashtag("#myword_dup")
        candidates = cracker._word_by_hash.get(ch.hash, [])
        # Count how many are our word
        matches = [c for w, c in candidates if c.name == "#myword_dup"]
        # May have duplicates in the list (add_wordlist doesn't dedup),
        # but cracking still works — the test verifies no crash
        assert len(matches) >= 1

    def test_unicode_words(self, tmp_path):
        store, _ = make_temp_store()
        ch = Channel.from_hashtag("#cafe")
        store_grp_txt_packet(store, ch, "User: hello cafe")

        cracker = ChannelCracker(store)
        wl = tmp_path / "unicode.txt"
        wl.write_text("cafe\ncafé\näpfel\n")
        cracker.add_wordlist(str(wl))

        cracker.notify_unknown_hash(ch.hash)
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()

        assert "#cafe" in cracker.cracked_names
        store.close()

    def test_empty_file(self, tmp_path):
        store, _ = make_temp_store()
        cracker = ChannelCracker(store)
        size_before = cracker.wordlist_size

        wl = tmp_path / "empty.txt"
        wl.write_text("")
        cracker.add_wordlist(str(wl))
        store.close()

        assert cracker.wordlist_size == size_before

    def test_missing_file(self):
        store, _ = make_temp_store()
        cracker = ChannelCracker(store)
        # Should not raise
        cracker.add_wordlist("/nonexistent/path/that/does/not/exist.txt")
        store.close()


# ---------------------------------------------------------------------------
# TestCachePersistence
# ---------------------------------------------------------------------------

class TestCachePersistence:
    """Cache load/save across store re-open."""

    def test_reopen_loads_cached_cracks(self):
        # Crack a channel and save to cache
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name

        store = CollectorStore(db_path)
        store.open()
        ch = Channel.from_hashtag("#hiking")
        store_grp_txt_packet(store, ch, "Hiker: Hello!")

        cracker = ChannelCracker(store)
        cracker.notify_unknown_hash(ch.hash)
        cracker.start()
        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0)
        cracker.stop()
        assert "#hiking" in cracker.cracked_names
        store.close()

        # Reopen with fresh cracker
        store2 = CollectorStore(db_path)
        store2.open()
        cracker2 = ChannelCracker(store2)
        cached = cracker2.load_cache()
        store2.close()

        assert len(cached) >= 1
        assert cracker2.cracked_count >= 1
        cached_names = {c.name for c in cached}
        assert "#hiking" in cached_names
        # Notifying for the same hash should be no-op
        cracker2.notify_unknown_hash(ch.hash)
        assert cracker2.pending_count == 0

        os.unlink(db_path)


# ---------------------------------------------------------------------------
# TestConcurrentNotify
# ---------------------------------------------------------------------------

class TestConcurrentNotify:
    """Thread safety of notify_unknown_hash."""

    def test_10_threads_notify(self):
        store, _ = make_temp_store()
        cracker = ChannelCracker(store)

        hashes = list(range(10))
        threads = []
        for h in hashes:
            t = threading.Thread(target=cracker.notify_unknown_hash, args=(h,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cracker.pending_count == 10
        for h in hashes:
            assert h in cracker.pending_hashes
        store.close()

    def test_notify_while_running(self, tmp_path):
        store, _ = make_temp_store()
        names = [f"conc_{i}" for i in range(3)]
        channels = []
        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            channels.append(ch)
            store_grp_txt_packet(store, ch, f"User: {name}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(names) + "\n")
        cracker.add_wordlist(str(wl))

        cracker.start()
        # Notify while running
        for ch in channels:
            cracker.notify_unknown_hash(ch.hash)
            time.sleep(0.05)

        assert _wait_cracker(cracker, lambda: cracker.pending_count == 0, timeout=10)
        cracker.stop()

        assert cracker.cracked_count >= 3
        store.close()
