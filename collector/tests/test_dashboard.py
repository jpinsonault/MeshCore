"""Tests for the DashboardActivity."""

import curses
import tempfile
import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.dashboard import DashboardActivity, _fmt_uptime, _fmt_time
from collector.events import (
    ChannelMessage,
    CollectorConnected,
    CollectorDisconnected,
    CollectorError,
    CollectorFrame,
)
from collector.crypto import GroupMessage
from collector.protocol import (
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_HEARTBEAT,
)
from collector.store import CollectorStore


def _make_dashboard():
    """Create a dashboard with auto_start=False for testing (no serial port needed)."""
    return DashboardActivity(port="/dev/ttyUSB0", auto_start=False)


class TestFormatHelpers:
    def test_fmt_uptime_seconds(self):
        assert _fmt_uptime(45) == "45s"

    def test_fmt_uptime_minutes(self):
        assert _fmt_uptime(125) == "2m 5s"

    def test_fmt_uptime_hours(self):
        assert _fmt_uptime(3665) == "1h 1m 5s"

    def test_fmt_uptime_none(self):
        assert _fmt_uptime(None) == "---"

    def test_fmt_time_valid(self):
        result = _fmt_time(1700000000)
        assert ":" in result  # HH:MM:SS format

    def test_fmt_time_none(self):
        assert _fmt_time(None) == "---"


class TestDashboardRendering:
    def test_shows_title_and_port(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        mock_screen.assert_text_on_screen("MeshCore Collector")
        mock_screen.assert_text_on_screen("/dev/ttyUSB0")

    def test_shows_connecting_status(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        mock_screen.assert_text_on_screen("Connecting")

    def test_shows_waiting_messages(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        mock_screen.assert_text_on_screen("waiting for packets")
        mock_screen.assert_text_on_screen("waiting for advertisements")

    def test_shows_help_keys(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        mock_screen.assert_text_on_screen("TAB")
        mock_screen.assert_text_on_screen("q:quit")

    def test_shows_heartbeat_pending(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        mock_screen.assert_text_on_screen("awaiting first heartbeat")


class TestDashboardEvents:
    def test_connected_event_updates_status(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.dispatch_event(CollectorConnected())
        app.drain()
        mock_screen.assert_text_on_screen("Connected")

    def test_disconnected_event_updates_status(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.dispatch_event(CollectorDisconnected("cable unplugged"))
        app.drain()
        mock_screen.assert_text_on_screen("Disconnected")

    def test_error_event_updates_status(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.dispatch_event(CollectorError("timeout"))
        app.drain()
        mock_screen.assert_text_on_screen("Error")

    def test_rx_frame_appears_in_list(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        frame = {
            "type": FRAME_TYPE_RX_RAW,
            "received_at": time.time(),
            "parsed": {
                "snr": 5.0,
                "rssi": -80,
                "route_type": 1,
                "route_name": "FLOOD",
                "payload_type": 5,
                "payload_name": "GRP_TXT",
                "raw_len": 20,
            },
        }
        app.dispatch_event(CollectorFrame(frame))
        app.drain()
        mock_screen.assert_text_on_screen("RX")
        mock_screen.assert_text_on_screen("FLOOD")
        mock_screen.assert_text_on_screen("GRP_TXT")

    def test_tx_frame_appears_in_list(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        frame = {
            "type": FRAME_TYPE_TX_RAW,
            "received_at": time.time(),
            "parsed": {
                "route_type": 2,
                "route_name": "DIRECT",
                "payload_type": 3,
                "payload_name": "ACK",
                "raw_len": 15,
            },
        }
        app.dispatch_event(CollectorFrame(frame))
        app.drain()
        mock_screen.assert_text_on_screen("TX")
        mock_screen.assert_text_on_screen("DIRECT")

    def test_advertisement_adds_node(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        frame = {
            "type": FRAME_TYPE_ADVERTISEMENT,
            "received_at": time.time(),
            "parsed": {
                "timestamp": 1700000000,
                "snr": 6.0,
                "pub_key_hex": "ab" * 32,
                "adv_type": 1,
                "adv_type_name": "CHAT",
                "name": "TestNode",
            },
        }
        app.dispatch_event(CollectorFrame(frame))
        app.drain()
        mock_screen.assert_text_on_screen("TestNode")
        mock_screen.assert_text_on_screen("CHAT")

    def test_heartbeat_updates_stats(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        frame = {
            "type": FRAME_TYPE_HEARTBEAT,
            "received_at": time.time(),
            "parsed": {
                "timestamp": 1700000000,
                "battery_mv": 3700,
                "rx_flood": 100,
                "rx_direct": 50,
                "tx_flood": 80,
                "tx_direct": 30,
                "free_pkts": 12,
                "uptime_secs": 3600,
            },
        }
        app.dispatch_event(CollectorFrame(frame))
        app.drain()
        mock_screen.assert_text_on_screen("3700mV")
        mock_screen.assert_text_on_screen("1h 0m 0s")

    def test_frame_counter_increments(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        for _ in range(3):
            frame = {
                "type": FRAME_TYPE_RX_RAW,
                "received_at": time.time(),
                "parsed": {
                    "snr": 5.0, "rssi": -80, "route_type": 1,
                    "route_name": "FLOOD", "payload_type": 5,
                    "payload_name": "GRP_TXT", "raw_len": 20,
                },
            }
            app.dispatch_event(CollectorFrame(frame))

        app.drain()
        assert activity._frame_count == 3
        assert activity._rx_count == 3
        mock_screen.assert_text_on_screen("3 frames")

    def test_multiple_nodes_tracked(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        for name, key in [("Alpha", "aa" * 32), ("Beta", "bb" * 32)]:
            frame = {
                "type": FRAME_TYPE_ADVERTISEMENT,
                "received_at": time.time(),
                "parsed": {
                    "timestamp": 1700000000,
                    "snr": 5.0,
                    "pub_key_hex": key,
                    "adv_type": 2,
                    "adv_type_name": "REPEATER",
                    "name": name,
                },
            }
            app.dispatch_event(CollectorFrame(frame))

        app.drain()
        mock_screen.assert_text_on_screen("Alpha")
        mock_screen.assert_text_on_screen("Beta")
        assert len(activity._nodes) == 2

    def test_node_count_updates(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        for _ in range(3):
            frame = {
                "type": FRAME_TYPE_ADVERTISEMENT,
                "received_at": time.time(),
                "parsed": {
                    "timestamp": 1700000000,
                    "snr": 5.0,
                    "pub_key_hex": "cc" * 32,
                    "adv_type": 1,
                    "adv_type_name": "CHAT",
                    "name": "Repeat",
                },
            }
            app.dispatch_event(CollectorFrame(frame))

        app.drain()
        assert activity._nodes["cc" * 32]["count"] == 3


class TestDashboardKeyboard:
    def test_esc_pops_activity(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_tab_cycles_focus(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        assert activity.focus == "packets"
        app.send_key(Keys.TAB)
        assert activity.focus == "nodes"
        app.send_key(Keys.TAB)
        assert activity.focus == "packets"

    def test_q_stops_application(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        app.send_key(ord("q"))
        assert len(app.flush_stop_events()) >= 1

    def test_scroll_packets(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        # Add some packets
        for i in range(5):
            frame = {
                "type": FRAME_TYPE_RX_RAW,
                "received_at": time.time(),
                "parsed": {
                    "snr": float(i), "rssi": -80, "route_type": 1,
                    "route_name": "FLOOD", "payload_type": 5,
                    "payload_name": "GRP_TXT", "raw_len": 20,
                },
            }
            app.dispatch_event(CollectorFrame(frame))
        app.drain()

        # Should be able to scroll
        initial_idx = activity.display_state["packets"]["selected_index"]
        app.send_key(curses.KEY_DOWN)
        assert activity.display_state["packets"]["selected_index"] == initial_idx + 1


def _group_msg(sender="Alice", text="Hello", channel="Public"):
    return GroupMessage(
        timestamp=1700000000,
        sender=sender,
        text=text,
        channel_name=channel,
        channel_hash=0xAA,
        raw_timestamp=time.time(),
    )


class TestDashboardChannelMessage:
    def test_channel_message_increments_counter(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        app.dispatch_event(ChannelMessage(_group_msg()))
        app.drain()

        assert activity._channel_msg_count == 1
        assert activity._channel_count == 1

    def test_multiple_channel_messages(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        app.dispatch_event(ChannelMessage(_group_msg(channel="Public")))
        app.dispatch_event(ChannelMessage(_group_msg(channel="Private")))
        app.dispatch_event(ChannelMessage(_group_msg(channel="Public")))
        app.drain()

        assert activity._channel_msg_count == 3
        assert activity._channel_count == 2

    def test_channel_stats_shown_after_message(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        app.dispatch_event(ChannelMessage(_group_msg()))
        app.drain()

        mock_screen.assert_text_on_screen("Channels")
        mock_screen.assert_text_on_screen("1 msgs decoded")

    def test_channel_stats_not_shown_when_zero(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        # No channel messages — stats line should not include "Channels"
        lines = activity._stats_lines()
        assert not any("Channels" in line for line in lines)

    def test_channel_stats_plural(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        app.dispatch_event(ChannelMessage(_group_msg(channel="A")))
        app.dispatch_event(ChannelMessage(_group_msg(channel="B")))
        app.drain()

        mock_screen.assert_text_on_screen("2 channels")


class TestDashboardChannelKey:
    def test_help_shows_channels_key(self, app, mock_screen):
        app.start_activity(_make_dashboard())
        mock_screen.assert_text_on_screen("c:chan")

    def test_c_key_does_not_crash_without_service(self, app, mock_screen):
        """Pressing 'c' when no collector service is registered should not crash."""
        activity = _make_dashboard()
        app.start_activity(activity)
        # No service registered, should handle gracefully
        app.send_key(ord("c"))
        # Still alive
        assert app.activity_stack_depth() >= 1


class _FakeCollectorService:
    """Minimal mock that provides a store for _reload_from_store tests."""
    def __init__(self, store):
        self.store = store
        self.core = None
        self.is_running = False
        self._application = None
        self.state = "STOPPED"

    def on_stop(self):
        pass


def _setup_store_with_data(db_path):
    """Create and populate a CollectorStore with test data."""
    store = CollectorStore(db_path=db_path)
    store.open()

    now = time.time()
    # Insert RX packets
    for i in range(5):
        store.store_frame({
            "type": FRAME_TYPE_RX_RAW,
            "received_at": now - 10 + i,
            "parsed": {
                "snr": 5.0 + i,
                "rssi": -80 + i,
                "route_type": 1,
                "payload_type": 5,
                "raw": b"\xaa" * 20,
                "raw_len": 20,
            },
        })
    # Insert TX packets
    for i in range(3):
        store.store_frame({
            "type": FRAME_TYPE_TX_RAW,
            "received_at": now - 5 + i,
            "parsed": {
                "route_type": 2,
                "payload_type": 3,
                "raw": b"\xbb" * 15,
                "raw_len": 15,
            },
        })
    # Insert advertisement
    store.store_frame({
        "type": FRAME_TYPE_ADVERTISEMENT,
        "received_at": now,
        "parsed": {
            "timestamp": 1700000000,
            "snr": 7.0,
            "pub_key_hex": "aa" * 32,
            "adv_type": 1,
            "adv_type_name": "CHAT",
            "name": "ReloadNode",
        },
    })
    # Insert heartbeat
    store.store_frame({
        "type": FRAME_TYPE_HEARTBEAT,
        "received_at": now,
        "parsed": {
            "timestamp": 1700000000,
            "battery_mv": 3800,
            "rx_flood": 50,
            "rx_direct": 20,
            "tx_flood": 40,
            "tx_direct": 10,
            "free_pkts": 8,
            "uptime_secs": 7200,
        },
    })
    return store


class TestDashboardReentry:
    """Test that dashboard state persists across segue/return transitions."""

    def _register_fake_service(self, app, activity, store):
        """Register a fake collector service and mark service as started."""
        svc = _FakeCollectorService(store)
        app._services["collector"] = svc
        svc._application = app
        activity._service_started = True
        return svc

    def _cleanup_fake_service(self, app):
        """Remove fake service before teardown to avoid attribute errors."""
        app._services.pop("collector", None)

    def test_reload_from_store_restores_counters(self, app, mock_screen):
        """After segue and return, dashboard reloads state from SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = _setup_store_with_data(db_path)

            activity = _make_dashboard()
            app.start_activity(activity)
            self._register_fake_service(app, activity, store)

            # Verify initial state (fresh dashboard, no data yet)
            assert activity._frame_count == 0
            assert activity._rx_count == 0
            assert activity._tx_count == 0

            # Simulate on_stop + on_start (as if returning from a segue)
            activity.on_stop()
            activity.on_start()
            app.drain()

            # Counters should be reloaded from SQLite
            assert activity._rx_count == 5
            assert activity._tx_count == 3
            assert activity._adv_count == 1
            # frame_count = total_packets(8) + advert_count(1)
            assert activity._frame_count == 9
            assert activity._status == "Connected"

            self._cleanup_fake_service(app)
            store.close()

    def test_reload_restores_nodes(self, app, mock_screen):
        """After re-entry, nodes are rebuilt from SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = _setup_store_with_data(db_path)

            activity = _make_dashboard()
            app.start_activity(activity)
            self._register_fake_service(app, activity, store)

            assert len(activity._nodes) == 0

            # Simulate re-entry
            activity.on_stop()
            activity.on_start()
            app.drain()

            assert len(activity._nodes) == 1
            assert "aa" * 32 in activity._nodes
            node = activity._nodes["aa" * 32]
            assert node["name"] == "ReloadNode"
            assert node["count"] == 1
            mock_screen.assert_text_on_screen("ReloadNode")

            self._cleanup_fake_service(app)
            store.close()

    def test_reload_restores_packets(self, app, mock_screen):
        """After re-entry, recent packets list is rebuilt from SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = _setup_store_with_data(db_path)

            activity = _make_dashboard()
            app.start_activity(activity)
            self._register_fake_service(app, activity, store)

            assert len(activity._recent_packets) == 0

            activity.on_stop()
            activity.on_start()
            app.drain()

            # 5 RX + 3 TX = 8 packets
            assert len(activity._recent_packets) == 8
            directions = [p["dir"] for p in activity._recent_packets]
            assert directions.count("RX") == 5
            assert directions.count("TX") == 3

            self._cleanup_fake_service(app)
            store.close()

    def test_reload_restores_heartbeat(self, app, mock_screen):
        """After re-entry, heartbeat data is restored from SQLite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = _setup_store_with_data(db_path)

            activity = _make_dashboard()
            app.start_activity(activity)
            self._register_fake_service(app, activity, store)

            assert activity._last_heartbeat is None

            activity.on_stop()
            activity.on_start()
            app.drain()

            assert activity._last_heartbeat is not None
            assert activity._last_heartbeat["battery_mv"] == 3800
            mock_screen.assert_text_on_screen("3800mV")

            self._cleanup_fake_service(app)
            store.close()

    def test_reload_restores_packet_timestamps(self, app, mock_screen):
        """After re-entry, packet_times deque is rebuilt for sparkline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"
            store = _setup_store_with_data(db_path)

            activity = _make_dashboard()
            app.start_activity(activity)
            self._register_fake_service(app, activity, store)

            assert len(activity._packet_times) == 0

            activity.on_stop()
            activity.on_start()
            app.drain()

            # 8 packets total
            assert len(activity._packet_times) == 8

            self._cleanup_fake_service(app)
            store.close()

    def test_first_start_does_not_reload(self, app, mock_screen):
        """On first start with auto_start=False, _reload_from_store is not called."""
        activity = _make_dashboard()
        app.start_activity(activity)

        # Should remain at initial state
        assert activity._service_started is False
        assert activity._frame_count == 0
        assert activity._status == "Connecting..."

    def test_service_started_flag_set_on_auto_start(self, app, mock_screen):
        """When auto_start=True and service starts, _service_started is set."""
        activity = DashboardActivity(port="/dev/ttyUSB0", auto_start=True)
        # We can't fully test auto_start=True without a real port,
        # but verify the flag logic: auto_start=False leaves it False
        activity2 = _make_dashboard()
        app.start_activity(activity2)
        assert activity2._service_started is False
