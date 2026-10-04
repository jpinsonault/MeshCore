"""Tests for the NodeListActivity."""

import tempfile
import time

import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.node_list import (
    NodeListActivity,
    build_node_list_items,
    _fmt_ago,
)
from collector.events import CollectorFrame
from collector.mesh_service import MeshCollectorService
from collector.protocol import FRAME_TYPE_ADVERTISEMENT
from collector.store import CollectorStore


def _node(name="TestNode", adv_type=1, adv_type_name="CHAT", pk=None,
          lat=None, lon=None, count=5):
    return {
        "pub_key_hex": pk or ("ab" * 32),
        "name": name,
        "adv_type": adv_type,
        "adv_type_name": adv_type_name,
        "lat": lat,
        "lon": lon,
        "first_seen": time.time() - 3600,
        "last_seen": time.time(),
        "advert_count": count,
        "rx_count": 0,
    }


def _adv_frame(name="TestNode", pk="aa" * 32, adv_type=1):
    return {
        "type": FRAME_TYPE_ADVERTISEMENT,
        "received_at": time.time(),
        "parsed": {
            "timestamp": 1700000000,
            "snr": 5.0,
            "pub_key_hex": pk,
            "adv_type": adv_type,
            "adv_type_name": "CHAT",
            "name": name,
            "lat": None,
            "lon": None,
        },
    }


class TestBuildNodeListItems:
    def test_empty_list(self):
        items = build_node_list_items([])
        assert len(items) == 1
        assert "no nodes" in items[0]

    def test_single_node(self):
        nodes = [_node(name="Alpha")]
        items = build_node_list_items(nodes)
        assert len(items) == 1
        assert "Alpha" in items[0]

    def test_shows_type(self):
        nodes = [_node(adv_type_name="REPEATER")]
        items = build_node_list_items(nodes)
        assert "REPEATER" in items[0]

    def test_shows_count(self):
        nodes = [_node(count=42)]
        items = build_node_list_items(nodes)
        assert "x42" in items[0]

    def test_shows_gps(self):
        nodes = [_node(lat=37.77, lon=-122.42)]
        items = build_node_list_items(nodes)
        assert "37.77" in items[0]

    def test_shows_pubkey_prefix(self):
        nodes = [_node(pk="deadbeef" + "00" * 28)]
        items = build_node_list_items(nodes)
        assert "deadbeef" in items[0]

    def test_multiple_nodes(self):
        nodes = [_node(name="A"), _node(name="B"), _node(name="C")]
        items = build_node_list_items(nodes)
        assert len(items) == 3


class TestNodeListActivity:
    def test_shows_title_all_nodes(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("All Nodes")

    def test_shows_title_repeaters(self, app, mock_screen):
        activity = NodeListActivity(adv_type=2)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Repeaters")

    def test_shows_title_rooms(self, app, mock_screen):
        activity = NodeListActivity(adv_type=3)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Room Servers")

    def test_custom_title(self, app, mock_screen):
        activity = NodeListActivity(title="My Custom List")
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("My Custom List")

    def test_esc_pops_activity(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_shows_no_nodes_when_empty(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("no nodes")

    def test_scroll_works(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        # Inject nodes after start and rebuild display
        activity._nodes = [_node(name=f"N{i}") for i in range(20)]
        activity._update_display()
        initial = activity.display_state["content"]["selected_index"]
        app.send_key(Keys.DOWN)
        assert activity.display_state["content"]["selected_index"] == initial + 1

    def test_r_refreshes(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        assert activity._nodes == []
        # r key should call _load_nodes (no store, so still empty, but no crash)
        app.send_key(ord("r"))

    def test_question_opens_help(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        app.send_key(ord("?"))
        assert app.activity_stack_depth() == 2

    def test_bottom_bar_shows_count(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("0 nodes")

    def test_live_update_on_advertisement(self, app, mock_screen):
        activity = NodeListActivity()
        app.start_activity(activity)
        # Simulate an advertisement frame event
        event = CollectorFrame(_adv_frame())
        activity._on_frame(event)
        # Should not crash — no store available, so nodes stays empty


class TestNodeListWithStore:
    def test_loads_all_nodes(self, app, mock_screen, store):
        """NodeListActivity loads nodes from store when available."""
        # Pre-populate store with nodes
        store.store_frame(_adv_frame(name="Alpha", pk="aa" * 32, adv_type=1))
        store.store_frame(_adv_frame(name="Beta", pk="bb" * 32, adv_type=2))
        store.store_frame(_adv_frame(name="Gamma", pk="cc" * 32, adv_type=3))

        # Create activity and manually inject store
        activity = NodeListActivity()
        app.start_activity(activity)
        # Override _get_store to use our test store
        activity._get_store = lambda: store
        activity._load_nodes()
        activity._update_display()

        assert len(activity._nodes) == 3

    def test_filters_by_type(self, app, mock_screen, store):
        store.store_frame(_adv_frame(name="Rep1", pk="aa" * 32, adv_type=2))
        store.store_frame(_adv_frame(name="Chat1", pk="bb" * 32, adv_type=1))
        store.store_frame(_adv_frame(name="Rep2", pk="cc" * 32, adv_type=2))

        activity = NodeListActivity(adv_type=2)
        app.start_activity(activity)
        activity._get_store = lambda: store
        activity._load_nodes()

        assert len(activity._nodes) == 2
        names = {n["name"] for n in activity._nodes}
        assert names == {"Rep1", "Rep2"}
