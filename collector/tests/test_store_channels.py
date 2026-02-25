"""Tests for the channel_messages storage layer."""

import tempfile
import time
import pytest

from collector.store import CollectorStore
from collector.crypto import GroupMessage


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        s = CollectorStore(f.name)
        s.open()
        yield s
        s.close()


def _msg(channel="Public", sender="Alice", text="Hello", ts=1700000000, hash=0xAA):
    return GroupMessage(
        timestamp=ts,
        sender=sender,
        text=text,
        channel_name=channel,
        channel_hash=hash,
        raw_timestamp=time.time(),
    )


class TestStoreChannelMessages:
    def test_store_and_retrieve(self, store):
        store.store_channel_message(_msg())
        msgs = store.get_channel_messages()
        assert len(msgs) == 1
        assert msgs[0]["sender"] == "Alice"
        assert msgs[0]["text"] == "Hello"
        assert msgs[0]["channel_name"] == "Public"

    def test_filter_by_channel(self, store):
        store.store_channel_message(_msg(channel="Public"))
        store.store_channel_message(_msg(channel="Private"))
        store.store_channel_message(_msg(channel="Public"))

        public = store.get_channel_messages(channel_name="Public")
        assert len(public) == 2
        private = store.get_channel_messages(channel_name="Private")
        assert len(private) == 1

    def test_ordering_ascending(self, store):
        now = time.time()
        m1 = _msg(sender="First")
        m1 = GroupMessage(1000, "First", "msg1", "Public", 0xAA, now - 10)
        m2 = GroupMessage(2000, "Second", "msg2", "Public", 0xAA, now)
        store.store_channel_message(m1)
        store.store_channel_message(m2)

        msgs = store.get_channel_messages()
        assert msgs[0]["sender"] == "First"
        assert msgs[1]["sender"] == "Second"

    def test_pagination(self, store):
        for i in range(10):
            store.store_channel_message(_msg(sender=f"User{i}"))
        msgs = store.get_channel_messages(limit=3, offset=2)
        assert len(msgs) == 3
        assert msgs[0]["sender"] == "User2"


class TestChannelSummary:
    def test_summary_query(self, store):
        store.store_channel_message(_msg(channel="Public", sender="Alice"))
        store.store_channel_message(_msg(channel="Public", sender="Bob"))
        store.store_channel_message(_msg(channel="Private", sender="Alice"))

        summary = store.get_channel_summary()
        assert len(summary) == 2
        by_name = {s["channel_name"]: s for s in summary}
        assert by_name["Public"]["msg_count"] == 2
        assert by_name["Public"]["unique_senders"] == 2
        assert by_name["Private"]["msg_count"] == 1
        assert by_name["Private"]["unique_senders"] == 1

    def test_message_count(self, store):
        assert store.get_channel_message_count() == 0
        store.store_channel_message(_msg())
        store.store_channel_message(_msg())
        assert store.get_channel_message_count() == 2


class TestSchemaVersion:
    def test_fresh_db_has_latest_version(self, store):
        row = store._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(row["value"]) == 4

    def test_migration_from_v1(self):
        """Simulate a V1 database and verify migration adds channel_messages."""
        import sqlite3
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            # Create a V1 database manually
            conn = sqlite3.connect(f.name)
            conn.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta (key, value) VALUES ('schema_version', '1');
                CREATE TABLE raw_packets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL, direction TEXT NOT NULL,
                    snr REAL, rssi INTEGER, route_type INTEGER,
                    payload_type INTEGER, raw_hex TEXT NOT NULL, raw_len INTEGER NOT NULL
                );
                CREATE TABLE advertisements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL, device_time INTEGER, snr REAL,
                    pub_key_hex TEXT NOT NULL, adv_type INTEGER, adv_type_name TEXT,
                    name TEXT, lat REAL, lon REAL
                );
                CREATE TABLE nodes (
                    pub_key_hex TEXT PRIMARY KEY, name TEXT, adv_type INTEGER,
                    adv_type_name TEXT, lat REAL, lon REAL,
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    advert_count INTEGER DEFAULT 0, rx_count INTEGER DEFAULT 0
                );
                CREATE TABLE heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL, device_time INTEGER, battery_mv INTEGER,
                    rx_flood INTEGER, rx_direct INTEGER, tx_flood INTEGER,
                    tx_direct INTEGER, free_pkts INTEGER, uptime_secs INTEGER
                );
            """)
            conn.close()

            # Now open with CollectorStore — should migrate
            s = CollectorStore(f.name)
            s.open()

            # Check version is now 4 (migrated through v2, v3, and v4)
            row = s._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            assert int(row["value"]) == 4

            # channel_messages table should exist
            s.store_channel_message(_msg())
            msgs = s.get_channel_messages()
            assert len(msgs) == 1

            s.close()


class TestEdgeCases:
    def test_offset_larger_than_count(self, store):
        """Offset beyond message count should return empty list."""
        store.store_channel_message(_msg())
        msgs = store.get_channel_messages(offset=100)
        assert msgs == []

    def test_limit_zero(self, store):
        """Limit=0 should return empty list."""
        store.store_channel_message(_msg())
        msgs = store.get_channel_messages(limit=0)
        assert msgs == []

    def test_unicode_sender_and_text(self, store):
        """Unicode characters in sender and text should round-trip."""
        m = GroupMessage(1700000000, "Ren\u00e9", "Caf\u00e9 \u2764\ufe0f", "Public", 0xAA, 1.0)
        store.store_channel_message(m)
        msgs = store.get_channel_messages()
        assert msgs[0]["sender"] == "Ren\u00e9"
        assert "Caf\u00e9" in msgs[0]["text"]

    def test_none_fields_stored(self, store):
        """None sender/text should be storable."""
        m = GroupMessage(1700000000, None, None, "Public", 0xAA, 1.0)
        store.store_channel_message(m)
        msgs = store.get_channel_messages()
        assert len(msgs) == 1
        assert msgs[0]["sender"] is None

    def test_raw_packet_id_stored(self, store):
        """raw_packet_id should be stored when provided."""
        store.store_channel_message(_msg(), raw_packet_id=42)
        msgs = store.get_channel_messages()
        assert msgs[0]["raw_packet_id"] == 42

    def test_nonexistent_channel_filter(self, store):
        """Filtering by a channel that doesn't exist returns empty."""
        store.store_channel_message(_msg(channel="Public"))
        msgs = store.get_channel_messages(channel_name="DoesNotExist")
        assert msgs == []


class TestGetStatsIncludesChannels:
    def test_stats_includes_channel_count(self, store):
        stats = store.get_stats()
        assert "channel_msg_count" in stats
        assert stats["channel_msg_count"] == 0

        store.store_channel_message(_msg())
        stats = store.get_stats()
        assert stats["channel_msg_count"] == 1
