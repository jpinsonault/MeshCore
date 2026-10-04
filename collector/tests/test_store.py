"""Tests for the SQLite storage layer."""

import tempfile
import time
import pytest

from collector.store import CollectorStore
from collector.protocol import (
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_HEARTBEAT,
)


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        s = CollectorStore(f.name)
        s.open()
        yield s
        s.close()


def _rx_frame(snr=5.0, rssi=-80, route_type=1, payload_type=5, raw_len=20, seq=None):
    frame = {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": snr,
            "rssi": rssi,
            "route_type": route_type,
            "payload_type": payload_type,
            "raw": b"\x15" + b"\x00" * (raw_len - 1),
            "raw_len": raw_len,
        },
    }
    if seq is not None:
        frame["seq"] = seq
    return frame


def _tx_frame(route_type=2, payload_type=3, raw_len=15, seq=None):
    frame = {
        "type": FRAME_TYPE_TX_RAW,
        "received_at": time.time(),
        "parsed": {
            "route_type": route_type,
            "payload_type": payload_type,
            "raw": b"\x0A" + b"\x00" * (raw_len - 1),
            "raw_len": raw_len,
        },
    }
    if seq is not None:
        frame["seq"] = seq
    return frame


def _adv_frame(pub_key_hex="ab" * 32, name="TestNode", adv_type=1):
    return {
        "type": FRAME_TYPE_ADVERTISEMENT,
        "received_at": time.time(),
        "parsed": {
            "timestamp": 1700000000,
            "snr": 6.0,
            "pub_key_hex": pub_key_hex,
            "adv_type": adv_type,
            "adv_type_name": "CHAT",
            "name": name,
            "lat": 37.7749,
            "lon": -122.4194,
        },
    }


def _hb_frame(uptime=3600, battery_mv=3700):
    return {
        "type": FRAME_TYPE_HEARTBEAT,
        "received_at": time.time(),
        "parsed": {
            "timestamp": 1700000000,
            "battery_mv": battery_mv,
            "rx_flood": 100,
            "rx_direct": 50,
            "tx_flood": 80,
            "tx_direct": 30,
            "free_pkts": 12,
            "uptime_secs": uptime,
        },
    }


class TestStoreBasics:
    def test_empty_stats(self, store):
        stats = store.get_stats()
        assert stats["total_packets"] == 0
        assert stats["node_count"] == 0

    def test_store_rx_packet(self, store):
        store.store_frame(_rx_frame())
        stats = store.get_stats()
        assert stats["total_packets"] == 1
        assert stats["rx_count"] == 1
        assert stats["tx_count"] == 0

    def test_store_tx_packet(self, store):
        store.store_frame(_tx_frame())
        stats = store.get_stats()
        assert stats["total_packets"] == 1
        assert stats["tx_count"] == 1

    def test_store_advertisement(self, store):
        store.store_frame(_adv_frame())
        stats = store.get_stats()
        assert stats["advert_count"] == 1
        assert stats["node_count"] == 1

    def test_store_heartbeat(self, store):
        store.store_frame(_hb_frame())
        stats = store.get_stats()
        hb = stats["latest_heartbeat"]
        assert hb is not None
        assert hb["uptime_secs"] == 3600


class TestNodeUpsert:
    def test_node_created_on_first_advert(self, store):
        store.store_frame(_adv_frame(name="Node1"))
        nodes = store.get_nodes()
        assert len(nodes) == 1
        assert nodes[0]["name"] == "Node1"
        assert nodes[0]["advert_count"] == 1

    def test_node_updated_on_subsequent_adverts(self, store):
        store.store_frame(_adv_frame(name="Node1"))
        store.store_frame(_adv_frame(name="Node1"))
        nodes = store.get_nodes()
        assert len(nodes) == 1
        assert nodes[0]["advert_count"] == 2

    def test_multiple_nodes(self, store):
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32, name="Alpha"))
        store.store_frame(_adv_frame(pub_key_hex="bb" * 32, name="Beta"))
        nodes = store.get_nodes()
        assert len(nodes) == 2
        names = {n["name"] for n in nodes}
        assert names == {"Alpha", "Beta"}


class TestQueries:
    def test_recent_packets_limit(self, store):
        for _ in range(10):
            store.store_frame(_rx_frame())
        packets = store.get_recent_packets(limit=5)
        assert len(packets) == 5

    def test_recent_packets_filter_direction(self, store):
        store.store_frame(_rx_frame())
        store.store_frame(_tx_frame())
        rx = store.get_recent_packets(direction="rx")
        tx = store.get_recent_packets(direction="tx")
        assert len(rx) == 1
        assert len(tx) == 1

    def test_recent_packets_filter_payload_type(self, store):
        store.store_frame(_rx_frame(payload_type=5))
        store.store_frame(_rx_frame(payload_type=2))
        result = store.get_recent_packets(payload_type=5)
        assert len(result) == 1

    def test_traffic_by_type(self, store):
        for _ in range(3):
            store.store_frame(_rx_frame(payload_type=5))
        store.store_frame(_rx_frame(payload_type=2))
        traffic = store.get_traffic_by_type()
        assert len(traffic) == 2
        # Sorted by count desc
        assert traffic[0]["payload_type"] == 5
        assert traffic[0]["cnt"] == 3

    def test_heartbeat_history(self, store):
        store.store_frame(_hb_frame(uptime=100))
        store.store_frame(_hb_frame(uptime=200))
        hbs = store.get_heartbeats()
        assert len(hbs) == 2

    def test_recent_advertisements(self, store):
        store.store_frame(_adv_frame(name="A"))
        store.store_frame(_adv_frame(name="A"))
        adverts = store.get_recent_advertisements()
        assert len(adverts) == 2


class TestSkipsInvalidFrames:
    def test_frame_with_no_parsed(self, store):
        store.store_frame({"type": FRAME_TYPE_RX_RAW, "received_at": time.time(), "parsed": None})
        assert store.get_stats()["total_packets"] == 0

    def test_frame_with_error(self, store):
        store.store_frame({
            "type": FRAME_TYPE_RX_RAW,
            "received_at": time.time(),
            "parsed": {"error": "too short"},
        })
        assert store.get_stats()["total_packets"] == 0


class TestSchemaV4:
    def test_seq_column_exists(self, store):
        """Schema v4 adds seq column to raw_packets."""
        store.store_frame(_rx_frame(seq=42))
        rows = store.get_recent_packets(limit=1)
        assert len(rows) == 1
        assert rows[0]["seq"] == 42

    def test_seq_null_for_v1_frames(self, store):
        """Frames without seq should have NULL seq."""
        store.store_frame(_rx_frame())
        rows = store.get_recent_packets(limit=1)
        assert rows[0]["seq"] is None

    def test_tx_frame_with_seq(self, store):
        store.store_frame(_tx_frame(seq=7))
        rows = store.get_recent_packets(limit=1)
        assert rows[0]["seq"] == 7


class TestLastCommittedSeq:
    def test_default_is_zero(self, store):
        assert store.get_last_committed_seq() == 0

    def test_roundtrip(self, store):
        store.set_last_committed_seq(42)
        assert store.get_last_committed_seq() == 42

    def test_upsert(self, store):
        store.set_last_committed_seq(10)
        store.set_last_committed_seq(20)
        assert store.get_last_committed_seq() == 20

    def test_persists_across_reopen(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        s = CollectorStore(path)
        s.open()
        s.set_last_committed_seq(99)
        s.close()

        s2 = CollectorStore(path)
        s2.open()
        assert s2.get_last_committed_seq() == 99
        s2.close()


class TestGetNodesByType:
    def test_empty_store(self, store):
        assert store.get_nodes_by_type(1) == []

    def test_filters_by_type(self, store):
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32, name="Chat1", adv_type=1))
        store.store_frame(_adv_frame(pub_key_hex="bb" * 32, name="Rep1", adv_type=2))
        store.store_frame(_adv_frame(pub_key_hex="cc" * 32, name="Room1", adv_type=3))

        repeaters = store.get_nodes_by_type(2)
        assert len(repeaters) == 1
        assert repeaters[0]["name"] == "Rep1"

        rooms = store.get_nodes_by_type(3)
        assert len(rooms) == 1
        assert rooms[0]["name"] == "Room1"

    def test_returns_multiple_of_same_type(self, store):
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32, name="Rep1", adv_type=2))
        store.store_frame(_adv_frame(pub_key_hex="bb" * 32, name="Rep2", adv_type=2))
        result = store.get_nodes_by_type(2)
        assert len(result) == 2

    def test_no_matches(self, store):
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32, name="Chat1", adv_type=1))
        assert store.get_nodes_by_type(3) == []


class TestGetNodeCountByType:
    def test_empty_store(self, store):
        assert store.get_node_count_by_type() == {}

    def test_counts_by_type(self, store):
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32, adv_type=1))
        store.store_frame(_adv_frame(pub_key_hex="bb" * 32, adv_type=1))
        store.store_frame(_adv_frame(pub_key_hex="cc" * 32, adv_type=2))
        store.store_frame(_adv_frame(pub_key_hex="dd" * 32, adv_type=3))

        counts = store.get_node_count_by_type()
        assert counts[1] == 2
        assert counts[2] == 1
        assert counts[3] == 1

    def test_same_node_not_double_counted(self, store):
        """Multiple adverts from the same node should not increase count."""
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32, adv_type=2))
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32, adv_type=2))
        counts = store.get_node_count_by_type()
        assert counts[2] == 1


class TestGetAdvertisementSnrHistory:
    def test_empty_store(self, store):
        assert store.get_advertisement_snr_history("aa" * 32) == []

    def test_returns_snr_tuples(self, store):
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32))
        result = store.get_advertisement_snr_history("aa" * 32)
        assert len(result) == 1
        ts, snr = result[0]
        assert snr == 6.0  # from _adv_frame default

    def test_returns_oldest_first(self, store):
        # Store two adverts with slightly different times
        f1 = _adv_frame(pub_key_hex="aa" * 32)
        f1["received_at"] = 1000.0
        f1["parsed"]["snr"] = 3.0
        store.store_frame(f1)

        f2 = _adv_frame(pub_key_hex="aa" * 32)
        f2["received_at"] = 2000.0
        f2["parsed"]["snr"] = 7.0
        store.store_frame(f2)

        result = store.get_advertisement_snr_history("aa" * 32)
        assert len(result) == 2
        assert result[0][0] < result[1][0]  # oldest first
        assert result[0][1] == 3.0
        assert result[1][1] == 7.0

    def test_limit(self, store):
        for i in range(10):
            f = _adv_frame(pub_key_hex="aa" * 32)
            f["received_at"] = 1000.0 + i
            f["parsed"]["snr"] = float(i)
            store.store_frame(f)

        result = store.get_advertisement_snr_history("aa" * 32, limit=5)
        assert len(result) == 5

    def test_filters_by_pub_key(self, store):
        store.store_frame(_adv_frame(pub_key_hex="aa" * 32))
        store.store_frame(_adv_frame(pub_key_hex="bb" * 32))

        result_a = store.get_advertisement_snr_history("aa" * 32)
        result_b = store.get_advertisement_snr_history("bb" * 32)
        assert len(result_a) == 1
        assert len(result_b) == 1
