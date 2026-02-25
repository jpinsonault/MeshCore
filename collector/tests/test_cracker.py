"""Tests for the ChannelCracker — passive dictionary attack on hashtag channels."""

import base64
import struct
import tempfile
import time
import pytest

from collector.cracker import ChannelCracker, BUILTIN_WORDLIST
from collector.crypto import (
    Channel,
    GroupMessage,
    encrypt_then_mac,
    extract_group_payload,
    mac_then_decrypt,
    PAYLOAD_TYPE_GRP_TXT,
    ROUTE_TYPE_FLOOD,
)
from collector.store import CollectorStore
from collector.protocol import FRAME_TYPE_RX_RAW


def _make_store():
    """Create a fresh in-memory-style store with a temp file."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = CollectorStore(f.name)
    store.open()
    return store, f.name


def _make_grp_txt_raw(channel, text, timestamp=1700000000):
    """Build raw packet bytes for a GRP_TXT message."""
    plaintext = struct.pack("<I", timestamp) + b"\x00" + text.encode("utf-8") + b"\x00"
    mac_and_data = encrypt_then_mac(channel.secret, plaintext)
    payload = bytes([channel.hash]) + mac_and_data
    header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
    return bytes([header, 0x00]) + payload


def _store_grp_txt_packet(store, channel, text, timestamp=1700000000):
    """Store a GRP_TXT packet in raw_packets and return its ID."""
    raw = _make_grp_txt_raw(channel, text, timestamp)
    frame = {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": 5.0,
            "rssi": -80,
            "route_type": ROUTE_TYPE_FLOOD,
            "payload_type": PAYLOAD_TYPE_GRP_TXT,
            "raw": raw,
            "raw_len": len(raw),
        },
    }
    return store.store_frame(frame)


class TestHashTable:
    def test_builtin_wordlist_nonempty(self):
        assert len(BUILTIN_WORDLIST) > 100

    def test_hash_table_built(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        store.close()
        # Hash table should have entries
        total = sum(len(v) for v in cracker._word_by_hash.values())
        assert total > 100

    def test_known_channels_excluded_from_hash_table(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#general")
        cracker = ChannelCracker(store, known_channels=[ch])
        store.close()
        # #general should not appear in hash table candidates
        for candidates in cracker._word_by_hash.values():
            for word, channel in candidates:
                assert channel.name != "#general"

    def test_hash_table_has_expected_channel(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        store.close()
        # "hiking" should be in the wordlist
        ch = Channel.from_hashtag("#hiking")
        candidates = cracker._word_by_hash.get(ch.hash, [])
        names = [c.name for _, c in candidates]
        assert "#hiking" in names


class TestNotifyUnknownHash:
    def test_adds_to_pending(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        store.close()
        cracker.notify_unknown_hash(0xAB)
        assert 0xAB in cracker._pending_hashes

    def test_known_hash_not_added(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#test")
        cracker = ChannelCracker(store, known_channels=[ch])
        store.close()
        cracker.notify_unknown_hash(ch.hash)
        assert ch.hash not in cracker._pending_hashes

    def test_duplicate_not_duplicated(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        store.close()
        cracker.notify_unknown_hash(0xAB)
        cracker.notify_unknown_hash(0xAB)
        assert cracker.pending_count == 1


class TestRetroactiveDecrypt:
    def test_decrypts_stored_packets(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#hiking")

        # Store 3 packets
        for i in range(3):
            _store_grp_txt_packet(store, ch, f"Hiker{i}: Trail report {i}", 1700000000 + i)

        cracker = ChannelCracker(store)
        count = cracker.retroactive_decrypt(ch)

        assert count == 3
        msgs = store.get_channel_messages()
        assert len(msgs) == 3
        store.close()

    def test_skips_already_decoded(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#hiking")

        pkt_id = _store_grp_txt_packet(store, ch, "Hiker: Already decoded")
        # Manually store a channel message for this packet
        msg = GroupMessage(
            timestamp=1700000000, sender="Hiker", text="Already decoded",
            channel_name="#hiking", channel_hash=ch.hash, raw_timestamp=time.time(),
        )
        store.store_channel_message(msg, raw_packet_id=pkt_id)

        cracker = ChannelCracker(store)
        count = cracker.retroactive_decrypt(ch)
        store.close()

        assert count == 0  # already decoded

    def test_returns_zero_for_no_packets(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#empty")
        cracker = ChannelCracker(store)
        count = cracker.retroactive_decrypt(ch)
        store.close()
        assert count == 0

    def test_wrong_channel_returns_zero(self):
        store, _ = _make_store()
        ch_real = Channel.from_hashtag("#hiking")
        ch_wrong = Channel.from_hashtag("#wrongchannel")
        _store_grp_txt_packet(store, ch_real, "Hiker: Hello")

        cracker = ChannelCracker(store)
        count = cracker.retroactive_decrypt(ch_wrong)
        store.close()
        assert count == 0


class TestCacheLoadSave:
    def test_store_and_load_cracked(self):
        store, _ = _make_store()
        store.store_cracked_channel("#hiking", 42, 5)
        rows = store.get_cracked_channels()
        store.close()

        assert len(rows) == 1
        assert rows[0]["channel_name"] == "#hiking"
        assert rows[0]["channel_hash"] == 42
        assert rows[0]["decoded_count"] == 5

    def test_load_cache_returns_channels(self):
        store, _ = _make_store()
        store.store_cracked_channel("#hiking", 42, 5)

        cracker = ChannelCracker(store)
        channels = cracker.load_cache()
        store.close()

        assert len(channels) == 1
        assert channels[0].name == "#hiking"
        assert cracker.cracked_count == 1

    def test_load_cache_skips_already_known(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#hiking")
        store.store_cracked_channel("#hiking", ch.hash, 5)

        cracker = ChannelCracker(store, known_channels=[ch])
        channels = cracker.load_cache()
        store.close()

        assert len(channels) == 0  # already known, not returned again


class TestCracking:
    def test_try_crack_finds_channel(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#hiking")
        _store_grp_txt_packet(store, ch, "Hiker: Trail open!")

        cracker = ChannelCracker(store)
        candidates = cracker._word_by_hash.get(ch.hash, [])
        assert len(candidates) > 0

        result = cracker._try_crack(ch.hash, candidates)
        assert result is True
        assert cracker.cracked_count == 1
        assert "#hiking" in cracker._known_names

        # Should have stored the cracked channel
        rows = store.get_cracked_channels()
        assert any(r["channel_name"] == "#hiking" for r in rows)

        # Should have decoded the message
        msgs = store.get_channel_messages()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Trail open!"

        store.close()

    def test_try_crack_fires_callback(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#hiking")
        _store_grp_txt_packet(store, ch, "Hiker: Hello")

        discovered = []
        cracker = ChannelCracker(store)
        cracker.on_channel_discovered = lambda name, count: discovered.append((name, count))

        candidates = cracker._word_by_hash.get(ch.hash, [])
        cracker._try_crack(ch.hash, candidates)
        store.close()

        assert len(discovered) == 1
        assert discovered[0][0] == "#hiking"
        assert discovered[0][1] >= 1

    def test_try_crack_no_match(self):
        store, _ = _make_store()
        # Use a PSK channel that won't be in the dictionary
        psk = base64.b64encode(b"\xDE\xAD" * 8).decode()
        ch = Channel.from_psk("Private", psk)
        _store_grp_txt_packet(store, ch, "Secret: message")

        cracker = ChannelCracker(store)
        # Use dummy candidates that won't match
        dummy_ch = Channel.from_hashtag("#nomatch_xyz_unlikely")
        result = cracker._try_crack(ch.hash, [("nomatch_xyz_unlikely", dummy_ch)])
        store.close()

        assert result is False

    def test_notify_and_background_crack(self):
        """Integration test: notify_unknown_hash -> background thread cracks it."""
        store, _ = _make_store()
        ch = Channel.from_hashtag("#mesh")
        _store_grp_txt_packet(store, ch, "MeshUser: Hello mesh!")

        discovered = []
        cracker = ChannelCracker(store)
        cracker.on_channel_discovered = lambda name, count: discovered.append((name, count))

        cracker.notify_unknown_hash(ch.hash)
        assert cracker.pending_count == 1

        cracker.start()
        # Wait for cracking to complete
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and cracker.pending_count > 0:
            time.sleep(0.1)
        cracker.stop()

        assert cracker.pending_count == 0
        assert cracker.cracked_count >= 1
        assert len(discovered) >= 1
        assert discovered[0][0] == "#mesh"

        store.close()


class TestRandomNameCracking:
    """Test cracking names NOT in the built-in wordlist, added via add_wordlist."""

    def test_crack_random_name_via_wordlist(self, tmp_path):
        """A totally random channel name can be cracked if added to a custom wordlist."""
        import secrets
        random_name = f"xyzzy_{secrets.token_hex(4)}"
        store, _ = _make_store()
        ch = Channel.from_hashtag(f"#{random_name}")
        _store_grp_txt_packet(store, ch, f"Sender: Message on {random_name}")

        cracker = ChannelCracker(store)

        # Not in built-in wordlist — notify should add to pending
        cracker.notify_unknown_hash(ch.hash)
        assert cracker.pending_count == 1

        # Without the wordlist, background crack won't find it
        candidates_before = cracker._word_by_hash.get(ch.hash, [])
        has_match = any(c.name == f"#{random_name}" for _, c in candidates_before)
        assert not has_match

        # Add custom wordlist containing the random name
        wl = tmp_path / "custom.txt"
        wl.write_text(f"{random_name}\nother_decoy_word\n")
        cracker.add_wordlist(str(wl))

        # Now it should be in candidates
        candidates_after = cracker._word_by_hash.get(ch.hash, [])
        has_match = any(c.name == f"#{random_name}" for _, c in candidates_after)
        assert has_match

        # Crack it
        discovered = []
        cracker.on_channel_discovered = lambda name, count: discovered.append((name, count))
        cracker.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and cracker.pending_count > 0:
            time.sleep(0.1)
        cracker.stop()

        assert cracker.pending_count == 0
        assert len(discovered) == 1
        assert discovered[0][0] == f"#{random_name}"
        assert discovered[0][1] >= 1

        msgs = store.get_channel_messages()
        assert len(msgs) == 1
        assert random_name in msgs[0]["text"]
        store.close()

    def test_crack_random_name_direct(self, tmp_path):
        """Direct _try_crack with a random name added to wordlist."""
        import secrets
        random_name = f"rand_{secrets.token_hex(3)}"
        store, _ = _make_store()
        ch = Channel.from_hashtag(f"#{random_name}")
        _store_grp_txt_packet(store, ch, f"User: {random_name} works")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text(f"{random_name}\n")
        cracker.add_wordlist(str(wl))

        candidates = cracker._word_by_hash.get(ch.hash, [])
        result = cracker._try_crack(ch.hash, candidates)

        assert result is True
        assert f"#{random_name}" in cracker.cracked_names

        msgs = store.get_channel_messages()
        assert len(msgs) == 1
        assert msgs[0]["text"] == f"{random_name} works"
        store.close()

    def test_multiple_random_names_cracked(self, tmp_path):
        """Crack several random names in one session."""
        import secrets
        names = [f"multi_{secrets.token_hex(3)}_{i}" for i in range(4)]
        store, _ = _make_store()

        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            _store_grp_txt_packet(store, ch, f"User: msg on {name}")

        cracker = ChannelCracker(store)
        wl = tmp_path / "words.txt"
        wl.write_text("\n".join(names) + "\n")
        cracker.add_wordlist(str(wl))

        discovered = []
        cracker.on_channel_discovered = lambda n, c: discovered.append(n)

        for name in names:
            ch = Channel.from_hashtag(f"#{name}")
            cracker.notify_unknown_hash(ch.hash)

        cracker.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and cracker.pending_count > 0:
            time.sleep(0.1)
        cracker.stop()

        assert cracker.cracked_count >= 4
        for name in names:
            assert f"#{name}" in cracker.cracked_names

        msgs = store.get_channel_messages()
        assert len(msgs) >= 4
        store.close()


class TestStatus:
    def test_status_dict_fields(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        st = cracker.status()
        store.close()

        assert "running" in st
        assert "cracked_count" in st
        assert "pending_count" in st
        assert "pending_hashes" in st
        assert "cracked_names" in st
        assert "wordlist_size" in st
        assert st["running"] is False
        assert st["cracked_count"] == 0
        assert st["wordlist_size"] > 100

    def test_status_reflects_state(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#hiking")
        _store_grp_txt_packet(store, ch, "Hiker: Status test")

        cracker = ChannelCracker(store)
        cracker.notify_unknown_hash(0xAB)
        cracker.notify_unknown_hash(0xCD)

        st = cracker.status()
        assert st["pending_count"] == 2
        assert 0xAB in st["pending_hashes"]
        assert 0xCD in st["pending_hashes"]

        # Crack hiking
        candidates = cracker._word_by_hash.get(ch.hash, [])
        if candidates:
            cracker._try_crack(ch.hash, candidates)

        st2 = cracker.status()
        assert st2["cracked_count"] >= 1
        assert "#hiking" in st2["cracked_names"]
        store.close()

    def test_pending_hashes_property(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        store.close()
        cracker.notify_unknown_hash(0x11)
        cracker.notify_unknown_hash(0x22)
        assert cracker.pending_hashes == {0x11, 0x22}

    def test_cracked_names_property(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#mesh")
        cracker = ChannelCracker(store, known_channels=[ch])
        store.close()
        assert "#mesh" in cracker.cracked_names

    def test_wordlist_size_property(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        store.close()
        assert cracker.wordlist_size > 100


class TestAddWordlist:
    def test_add_wordlist_file(self, tmp_path):
        store, _ = _make_store()
        cracker = ChannelCracker(store)

        # Create a wordlist file
        wl = tmp_path / "words.txt"
        wl.write_text("supercustomword42\nanothercustomword99\n")

        cracker.add_wordlist(str(wl))
        store.close()

        # Check the custom words are in the hash table
        ch = Channel.from_hashtag("#supercustomword42")
        candidates = cracker._word_by_hash.get(ch.hash, [])
        names = [c.name for _, c in candidates]
        assert "#supercustomword42" in names

    def test_add_wordlist_missing_file(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        store.close()
        # Should not raise
        cracker.add_wordlist("/nonexistent/path/words.txt")


class TestStartStop:
    def test_start_stop(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        assert not cracker.running
        cracker.start()
        assert cracker.running
        cracker.stop()
        assert not cracker.running
        store.close()

    def test_double_start(self):
        store, _ = _make_store()
        cracker = ChannelCracker(store)
        cracker.start()
        cracker.start()  # should be no-op
        assert cracker.running
        cracker.stop()
        store.close()


class TestStoreSchemaV5:
    def test_cracked_channels_table_exists(self):
        store, _ = _make_store()
        # Table should exist after open
        store.store_cracked_channel("#test", 42, 0)
        rows = store.get_cracked_channels()
        assert len(rows) == 1
        store.close()

    def test_store_frame_returns_id(self):
        store, _ = _make_store()
        frame = {
            "type": FRAME_TYPE_RX_RAW,
            "received_at": time.time(),
            "parsed": {
                "snr": 5.0,
                "rssi": -80,
                "route_type": ROUTE_TYPE_FLOOD,
                "payload_type": PAYLOAD_TYPE_GRP_TXT,
                "raw": b"\x15\x00\xAB\x01\x02",
                "raw_len": 5,
            },
        }
        row_id = store.store_frame(frame)
        assert row_id is not None
        assert row_id > 0
        store.close()

    def test_store_frame_returns_none_for_non_packet(self):
        store, _ = _make_store()
        from collector.protocol import FRAME_TYPE_HEARTBEAT
        frame = {
            "type": FRAME_TYPE_HEARTBEAT,
            "received_at": time.time(),
            "parsed": {
                "timestamp": 1700000000,
                "battery_mv": 3700,
                "rx_flood": 10, "rx_direct": 5,
                "tx_flood": 8, "tx_direct": 3,
                "free_pkts": 6, "uptime_secs": 3600,
            },
        }
        row_id = store.store_frame(frame)
        assert row_id is None
        store.close()

    def test_has_channel_message_for_packet(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#test")
        pkt_id = _store_grp_txt_packet(store, ch, "User: Hello")

        assert not store.has_channel_message_for_packet(pkt_id)

        msg = GroupMessage(
            timestamp=1700000000, sender="User", text="Hello",
            channel_name="#test", channel_hash=ch.hash, raw_timestamp=time.time(),
        )
        store.store_channel_message(msg, raw_packet_id=pkt_id)

        assert store.has_channel_message_for_packet(pkt_id)
        store.close()

    def test_get_grp_txt_packets(self):
        store, _ = _make_store()
        ch = Channel.from_hashtag("#test")
        _store_grp_txt_packet(store, ch, "User: Msg1")
        _store_grp_txt_packet(store, ch, "User: Msg2")

        packets = store.get_grp_txt_packets()
        assert len(packets) == 2
        assert all(p.get("raw_hex") for p in packets)
        store.close()

    def test_schema_version_is_5(self):
        store, _ = _make_store()
        row = store._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(row["value"]) == 5
        store.close()
