"""Tests for the enhanced dashboard features: sparkline, packet rate, detail drill-down."""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.dashboard import (
    DashboardActivity,
    make_sparkline,
    calc_packet_rate,
    make_type_distribution,
    SPARK_CHARS,
    SPARKLINE_BUCKETS,
)
from collector.events import CollectorFrame
from collector.protocol import FRAME_TYPE_RX_RAW, FRAME_TYPE_TX_RAW, FRAME_TYPE_ADVERTISEMENT


def _make_dashboard():
    return DashboardActivity(port="/dev/ttyUSB0", auto_start=False)


def _rx_frame(snr=5.0, rssi=-80, route="FLOOD", ptype="GRP_TXT", raw=None):
    if raw is None:
        raw = bytes([0x15]) + b"\x00" * 19
    return {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": snr,
            "rssi": rssi,
            "route_type": 1,
            "route_name": route,
            "payload_type": 5,
            "payload_name": ptype,
            "raw_len": len(raw),
            "raw": raw,
            "header": raw[0],
        },
    }


def _tx_frame():
    raw = bytes([0x09]) + b"\x00" * 14
    return {
        "type": FRAME_TYPE_TX_RAW,
        "received_at": time.time(),
        "parsed": {
            "route_type": 2,
            "route_name": "DIRECT",
            "payload_type": 3,
            "payload_name": "ACK",
            "raw_len": len(raw),
            "raw": raw,
            "header": raw[0],
        },
    }


def _adv_frame(name="TestNode", pk="aa" * 32, snr=5.0, lat=None, lon=None):
    return {
        "type": FRAME_TYPE_ADVERTISEMENT,
        "received_at": time.time(),
        "parsed": {
            "timestamp": 1700000000,
            "snr": snr,
            "pub_key_hex": pk,
            "adv_type": 1,
            "adv_type_name": "CHAT",
            "name": name,
            "lat": lat,
            "lon": lon,
        },
    }


class TestMakeSparkline:
    def test_empty_timestamps(self):
        result = make_sparkline([], now=1000, window=60, buckets=10)
        assert len(result) == 10
        assert result == SPARK_CHARS[0] * 10

    def test_single_timestamp(self):
        now = 1000.0
        result = make_sparkline([999.0], now=now, window=60, buckets=10)
        assert len(result) == 10
        # The timestamp at 999 is in the last bucket
        assert SPARK_CHARS[-1] in result

    def test_uniform_traffic(self):
        now = 1000.0
        # One packet per bucket
        timestamps = [now - 60 + i * 2 + 1 for i in range(30)]
        result = make_sparkline(timestamps, now=now)
        assert len(result) == SPARKLINE_BUCKETS
        # All buckets should have roughly equal activity
        for c in result:
            assert c in SPARK_CHARS

    def test_burst_traffic(self):
        now = 1000.0
        # All packets in the last 2 seconds
        timestamps = [now - 1] * 10
        result = make_sparkline(timestamps, now=now, window=60, buckets=30)
        # Most buckets should be empty, one should be full
        assert SPARK_CHARS[-1] in result
        assert SPARK_CHARS[0] in result

    def test_old_timestamps_ignored(self):
        now = 1000.0
        # All timestamps outside the window
        timestamps = [500.0, 600.0, 700.0]
        result = make_sparkline(timestamps, now=now, window=60, buckets=10)
        assert result == SPARK_CHARS[0] * 10


class TestCalcPacketRate:
    def test_no_packets(self):
        rate = calc_packet_rate([], now=1000, window=10)
        assert rate == 0.0

    def test_known_rate(self):
        now = 1000.0
        # 10 packets in 10 second window = 1.0 pkt/s
        timestamps = [now - i for i in range(10)]
        rate = calc_packet_rate(timestamps, now=now, window=10)
        assert rate == 1.0

    def test_old_packets_excluded(self):
        now = 1000.0
        timestamps = [500.0, 600.0, 999.0, 999.5]
        rate = calc_packet_rate(timestamps, now=now, window=10)
        assert rate == 0.2  # 2 packets in 10 seconds


class TestMakeTypeDistribution:
    def test_empty(self):
        assert make_type_distribution({}) == ""

    def test_single_type(self):
        result = make_type_distribution({"GRP_TXT": 10})
        assert "GRP_TXT" in result

    def test_multiple_types(self):
        result = make_type_distribution({"GRP_TXT": 10, "ACK": 5, "REQ": 2})
        assert "GRP_TXT" in result
        assert "ACK" in result

    def test_max_four_types(self):
        counts = {f"TYPE{i}": i for i in range(10)}
        result = make_type_distribution(counts)
        # Should show at most 4 types
        type_count = sum(1 for name in counts if name in result)
        assert type_count <= 4


class TestDashboardEnhancedStats:
    def test_packet_rate_shown_after_frames(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        for _ in range(5):
            app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()

        mock_screen.assert_text_on_screen("p/s")

    def test_sparkline_shown_after_frames(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        for _ in range(3):
            app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()

        # Sparkline chars appear in the stats line
        lines = activity._stats_lines()
        has_spark = any(c in "".join(lines) for c in SPARK_CHARS[1:])
        assert has_spark

    def test_payload_types_tracked(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_rx_frame(ptype="GRP_TXT")))
        app.dispatch_event(CollectorFrame(_rx_frame(ptype="ACK")))
        app.dispatch_event(CollectorFrame(_rx_frame(ptype="GRP_TXT")))
        app.drain()

        assert activity._payload_type_counts["GRP_TXT"] == 2
        assert activity._payload_type_counts["ACK"] == 1


class TestDashboardNodeSnrHistory:
    def test_snr_history_tracked(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        pk = "aa" * 32
        for snr in [3.0, 5.0, 7.0]:
            frame = _adv_frame(pk=pk, snr=snr)
            app.dispatch_event(CollectorFrame(frame))
        app.drain()

        assert len(activity._node_snr_history[pk]) == 3
        values = [s for _, s in activity._node_snr_history[pk]]
        assert values == [3.0, 5.0, 7.0]

    def test_snr_sparkline_in_node_line(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        pk = "aa" * 32
        for snr in [1.0, 3.0, 5.0]:
            frame = _adv_frame(pk=pk, snr=snr)
            app.dispatch_event(CollectorFrame(frame))
        app.drain()

        # Node line should contain sparkline chars
        node_line = activity._node_line(pk, activity._nodes[pk])
        has_spark_char = any(c in node_line for c in SPARK_CHARS[1:])
        assert has_spark_char


class TestDashboardEnterDrillDown:
    def test_enter_on_packet_opens_detail(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()

        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ENTER)
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Packet Detail")

    def test_enter_on_node_opens_detail(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_adv_frame()))
        app.drain()

        # Switch focus to nodes (right panel)
        app.send_key(Keys.TAB)
        assert activity.display_state["split"]["focused_panel"] == "right"

        app.send_key(Keys.ENTER)
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Node Detail")

    def test_enter_on_empty_packets_does_nothing(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.send_key(Keys.ENTER)
        assert app.activity_stack_depth() == 1

    def test_enter_on_empty_nodes_does_nothing(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.send_key(Keys.TAB)  # focus nodes
        app.send_key(Keys.ENTER)
        assert app.activity_stack_depth() == 1


class TestDashboardNewKeybindings:
    def test_d_opens_debug_log(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.send_key(ord("d"))
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Debug Log")

    def test_question_mark_opens_help(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.send_key(ord("?"))
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Help")

    def test_help_shows_dashboard_keys(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        app.send_key(ord("?"))
        mock_screen.assert_text_on_screen("Dashboard")

    def test_updated_bottom_bar(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("d:log")
        mock_screen.assert_text_on_screen("?:help")
        mock_screen.assert_text_on_screen("resize")


class TestDashboardFrameStorage:
    def test_packets_carry_frame_data(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        frame = _rx_frame()
        app.dispatch_event(CollectorFrame(frame))
        app.drain()

        assert len(activity._recent_packets) == 1
        pkt = activity._recent_packets[0]
        assert "frame" in pkt
        assert pkt["frame"]["type"] == FRAME_TYPE_RX_RAW

    def test_node_lat_lon_tracked(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        pk = "bb" * 32
        frame = _adv_frame(pk=pk, lat=37.77, lon=-122.42)
        app.dispatch_event(CollectorFrame(frame))
        app.drain()

        node = activity._nodes[pk]
        assert node["lat"] == 37.77
        assert node["lon"] == -122.42

    def test_sorted_node_keys_maintained(self, app, mock_screen):
        activity = _make_dashboard()
        app.start_activity(activity)

        for name, pk in [("Alpha", "aa" * 32), ("Beta", "bb" * 32)]:
            frame = _adv_frame(name=name, pk=pk)
            app.dispatch_event(CollectorFrame(frame))
        app.drain()

        assert len(activity._sorted_node_keys) == 2
        # Most recent should be first
        assert activity._sorted_node_keys[0] == "bb" * 32
