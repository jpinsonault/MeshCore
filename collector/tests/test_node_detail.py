"""Tests for the NodeDetailActivity."""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.node_detail import (
    NodeDetailActivity,
    snr_sparkline,
    snr_stats,
    build_node_detail_lines,
    _fmt_ago,
    _fmt_active_duration,
    SPARK_CHARS,
)


def _node_info(name="TestNode", atype="CHAT", snr=5.0, count=10, lat=None, lon=None, adv_type=1):
    """Create a node info dict for testing."""
    return {
        "name": name,
        "adv_type": adv_type,
        "adv_type_name": atype,
        "snr": snr,
        "last_seen": time.time(),
        "first_seen": time.time() - 3600,
        "count": count,
        "lat": lat,
        "lon": lon,
        "pub_key_hex": "ab" * 32,
    }


def _snr_history(values=None):
    """Create SNR history tuples."""
    if values is None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
    now = time.time()
    return [(now - len(values) + i, v) for i, v in enumerate(values)]


class TestSnrSparkline:
    def test_empty_history(self):
        result = snr_sparkline([])
        assert result == "(no data)"

    def test_single_value(self):
        history = [(time.time(), 5.0)]
        result = snr_sparkline(history)
        assert len(result) == 1
        assert result[0] in SPARK_CHARS

    def test_rising_values(self):
        history = _snr_history([-20.0, -10.0, 0.0, 5.0, 10.0])
        result = snr_sparkline(history)
        assert len(result) == 5
        # Values should be non-decreasing in sparkline char index
        indices = [SPARK_CHARS.index(c) for c in result]
        assert indices == sorted(indices)

    def test_max_width(self):
        history = _snr_history([float(i) for i in range(50)])
        result = snr_sparkline(history, width=10)
        assert len(result) == 10

    def test_negative_snr(self):
        history = _snr_history([-15.0, -10.0, -5.0])
        result = snr_sparkline(history)
        # All should be valid sparkline chars
        for c in result:
            assert c in SPARK_CHARS

    def test_all_same_value(self):
        history = _snr_history([3.0, 3.0, 3.0])
        result = snr_sparkline(history)
        # All chars should be the same
        assert len(set(result)) == 1


class TestSnrStats:
    def test_empty_history(self):
        assert snr_stats([]) is None

    def test_basic_stats(self):
        history = _snr_history([1.0, 2.0, 3.0, 4.0, 5.0])
        stats = snr_stats(history)
        assert stats["min"] == 1.0
        assert stats["max"] == 5.0
        assert stats["avg"] == 3.0
        assert stats["current"] == 5.0
        assert stats["samples"] == 5

    def test_single_value(self):
        history = [(time.time(), 7.5)]
        stats = snr_stats(history)
        assert stats["min"] == 7.5
        assert stats["max"] == 7.5
        assert stats["avg"] == 7.5
        assert stats["current"] == 7.5

    def test_negative_values(self):
        history = _snr_history([-10.0, -5.0, -2.0])
        stats = snr_stats(history)
        assert stats["min"] == -10.0
        assert stats["max"] == -2.0


class TestFmtAgo:
    def test_seconds(self):
        result = _fmt_ago(time.time() - 30)
        assert "30s ago" == result

    def test_minutes(self):
        result = _fmt_ago(time.time() - 120)
        assert "2m ago" == result

    def test_hours(self):
        result = _fmt_ago(time.time() - 7200)
        assert "2h ago" == result

    def test_days(self):
        result = _fmt_ago(time.time() - 172800)
        assert "2d ago" == result

    def test_none(self):
        assert _fmt_ago(None) == "---"


class TestBuildNodeDetailLines:
    def test_shows_name(self):
        lines = build_node_detail_lines("aa" * 32, _node_info(name="Alice"), [])
        text = "\n".join(lines)
        assert "Alice" in text

    def test_shows_type(self):
        lines = build_node_detail_lines("aa" * 32, _node_info(atype="REPEATER"), [])
        text = "\n".join(lines)
        assert "REPEATER" in text

    def test_shows_public_key(self):
        pk = "ab" * 32
        lines = build_node_detail_lines(pk, _node_info(), [])
        text = "\n".join(lines)
        assert "ab ab ab ab" in text

    def test_shows_gps(self):
        info = _node_info(lat=37.7749, lon=-122.4194)
        lines = build_node_detail_lines("aa" * 32, info, [])
        text = "\n".join(lines)
        assert "37.774900" in text
        assert "-122.419400" in text

    def test_no_gps(self):
        info = _node_info()
        lines = build_node_detail_lines("aa" * 32, info, [])
        text = "\n".join(lines)
        assert "no GPS" in text

    def test_shows_advert_count(self):
        info = _node_info(count=42)
        lines = build_node_detail_lines("aa" * 32, info, [])
        text = "\n".join(lines)
        assert "42" in text

    def test_shows_snr_stats(self):
        history = _snr_history([1.0, 3.0, 5.0])
        lines = build_node_detail_lines("aa" * 32, _node_info(), history)
        text = "\n".join(lines)
        assert "Signal Quality" in text
        assert "+5.0" in text
        assert "3 " in text or "Samples" in text

    def test_no_snr_history(self):
        lines = build_node_detail_lines("aa" * 32, _node_info(), [])
        text = "\n".join(lines)
        assert "no signal data" in text

    def test_shows_snr_sparkline(self):
        history = _snr_history([-10.0, 0.0, 5.0, 10.0])
        lines = build_node_detail_lines("aa" * 32, _node_info(), history)
        text = "\n".join(lines)
        assert "SNR History" in text


class TestNodeDetailActivity:
    def test_shows_title(self, app, mock_screen):
        activity = NodeDetailActivity("aa" * 32, _node_info())
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Node Detail")

    def test_shows_node_name_in_subtitle(self, app, mock_screen):
        info = _node_info(name="MyRepeater")
        activity = NodeDetailActivity("aa" * 32, info)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("MyRepeater")

    def test_shows_node_type_in_subtitle(self, app, mock_screen):
        info = _node_info(atype="SENSOR")
        activity = NodeDetailActivity("aa" * 32, info)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("SENSOR")

    def test_shows_public_key(self, app, mock_screen):
        pk = "ab" * 32
        activity = NodeDetailActivity(pk, _node_info())
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("ab ab ab ab")

    def test_esc_pops_activity(self, app, mock_screen):
        activity = NodeDetailActivity("aa" * 32, _node_info())
        app.start_activity(activity)
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_scroll_works(self, app, mock_screen):
        history = _snr_history(list(range(20)))
        activity = NodeDetailActivity("aa" * 32, _node_info(), history)
        app.start_activity(activity)
        initial = activity.display_state["content"]["selected_index"]
        app.send_key(Keys.DOWN)
        assert activity.display_state["content"]["selected_index"] == initial + 1

    def test_shows_bottom_bar_with_key(self, app, mock_screen):
        pk = "ab" * 32
        activity = NodeDetailActivity(pk, _node_info())
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("abababababababab...")

    def test_with_snr_history(self, app, mock_screen):
        history = _snr_history([3.0, 5.0, 7.0])
        activity = NodeDetailActivity("aa" * 32, _node_info(), history)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Signal Quality")

    def test_with_gps_data(self, app, mock_screen):
        info = _node_info(lat=37.7749, lon=-122.4194)
        activity = NodeDetailActivity("aa" * 32, info)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("37.774900")


class TestFmtActiveDuration:
    def test_none(self):
        assert _fmt_active_duration(None) == "---"

    def test_minutes(self):
        result = _fmt_active_duration(time.time() - 300)
        assert "5m" == result

    def test_hours_and_minutes(self):
        result = _fmt_active_duration(time.time() - 5400)
        assert "1h 30m" == result

    def test_days_and_hours(self):
        result = _fmt_active_duration(time.time() - 90000)
        assert "1d 1h" == result


class TestTypeSpecificSections:
    def test_repeater_section(self):
        info = _node_info(atype="REPEATER", adv_type=2, count=42)
        lines = build_node_detail_lines("aa" * 32, info, [])
        text = "\n".join(lines)
        assert "Repeater" in text
        assert "Active for:" in text
        assert "42" in text

    def test_room_server_section(self):
        info = _node_info(atype="ROOM_SERVER", adv_type=3, count=15)
        lines = build_node_detail_lines("aa" * 32, info, [])
        text = "\n".join(lines)
        assert "Room Server" in text
        assert "Active for:" in text
        assert "15" in text

    def test_chat_node_no_extra_section(self):
        info = _node_info(atype="CHAT", adv_type=1, count=10)
        lines = build_node_detail_lines("aa" * 32, info, [])
        text = "\n".join(lines)
        assert "Repeater" not in text
        assert "Room Server" not in text

    def test_repeater_in_activity(self, app, mock_screen):
        info = _node_info(atype="REPEATER", adv_type=2)
        activity = NodeDetailActivity("aa" * 32, info)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Repeater")

    def test_room_in_activity(self, app, mock_screen):
        info = _node_info(atype="ROOM_SERVER", adv_type=3)
        activity = NodeDetailActivity("aa" * 32, info)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Room Server")
