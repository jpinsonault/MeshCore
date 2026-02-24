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


def _rx_frame(snr=5.0, rssi=-80, route_type=1, payload_type=5, raw_len=20):
    return {
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


def _tx_frame(route_type=2, payload_type=3, raw_len=15):
    return {
        "type": FRAME_TYPE_TX_RAW,
        "received_at": time.time(),
        "parsed": {
            "route_type": route_type,
            "payload_type": payload_type,
            "raw": b"\x0A" + b"\x00" * (raw_len - 1),
            "raw_len": raw_len,
        },
    }


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
