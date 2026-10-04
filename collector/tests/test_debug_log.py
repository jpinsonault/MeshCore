"""Tests for the DebugLogActivity."""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.debug_log import DebugLogActivity
from collector.events import (
    CollectorConnected,
    CollectorDisconnected,
    CollectorError,
    CollectorFrame,
    CollectorText,
)
from collector.protocol import FRAME_TYPE_RX_RAW, FRAME_TYPE_TX_RAW, FRAME_TYPE_HEARTBEAT


def _rx_frame(route="FLOOD", ptype="GRP_TXT", snr=5.0, raw_len=20):
    return {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": snr,
            "rssi": -80,
            "route_type": 1,
            "route_name": route,
            "payload_type": 5,
            "payload_name": ptype,
            "raw_len": raw_len,
        },
    }


def _tx_frame(route="DIRECT", ptype="ACK", raw_len=15):
    return {
        "type": FRAME_TYPE_TX_RAW,
        "received_at": time.time(),
        "parsed": {
            "route_type": 2,
            "route_name": route,
            "payload_type": 3,
            "payload_name": ptype,
            "raw_len": raw_len,
        },
    }


def _hb_frame():
    return {
        "type": FRAME_TYPE_HEARTBEAT,
        "received_at": time.time(),
        "parsed": {
            "battery_mv": 3700,
            "uptime_secs": 3600,
            "free_pkts": 12,
        },
    }


class TestDebugLogRendering:
    def test_shows_title(self, app, mock_screen):
        app.start_activity(DebugLogActivity())
        mock_screen.assert_text_on_screen("Debug Log")

    def test_shows_waiting_messages(self, app, mock_screen):
        app.start_activity(DebugLogActivity())
        mock_screen.assert_text_on_screen("waiting for frames")
        mock_screen.assert_text_on_screen("waiting for serial text")

    def test_shows_help_keys(self, app, mock_screen):
        app.start_activity(DebugLogActivity())
        mock_screen.assert_text_on_screen("TAB")
        mock_screen.assert_text_on_screen("ESC")


class TestDebugLogFrameEvents:
    def test_rx_frame_appears(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()

        mock_screen.assert_text_on_screen("RX_RAW")
        mock_screen.assert_text_on_screen("FLOOD")

    def test_tx_frame_appears(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_tx_frame()))
        app.drain()

        mock_screen.assert_text_on_screen("TX_RAW")

    def test_heartbeat_frame_appears(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_hb_frame()))
        app.drain()

        mock_screen.assert_text_on_screen("HEARTBEAT")

    def test_frame_count_updates(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        for _ in range(3):
            app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()

        assert len(activity._frame_entries) == 3
        mock_screen.assert_text_on_screen("3 frames")

    def test_snr_shown_for_rx(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_rx_frame(snr=7.5)))
        app.drain()

        mock_screen.assert_text_on_screen("+7.5")


class TestDebugLogTextEvents:
    def test_text_appears(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorText("Hello from firmware"))
        app.drain()

        mock_screen.assert_text_on_screen("Hello from firmware")

    def test_text_count_updates(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorText("line 1"))
        app.dispatch_event(CollectorText("line 2"))
        app.drain()

        assert len(activity._text_entries) == 2
        mock_screen.assert_text_on_screen("2 text")


class TestDebugLogConnectionEvents:
    def test_connected_event(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorConnected())
        app.drain()

        mock_screen.assert_text_on_screen("CONNECTED")

    def test_disconnected_event(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorDisconnected("cable unplugged"))
        app.drain()

        mock_screen.assert_text_on_screen("DISCONNECTED")
        mock_screen.assert_text_on_screen("cable unplugged")

    def test_error_event(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorError("timeout"))
        app.drain()

        mock_screen.assert_text_on_screen("ERROR")
        mock_screen.assert_text_on_screen("timeout")


class TestDebugLogKeyboard:
    def test_esc_pops_activity(self, app, mock_screen):
        app.start_activity(DebugLogActivity())
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_tab_cycles_focus(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)
        assert activity.focus == "frames"
        app.send_key(Keys.TAB)
        assert activity.focus == "text"
        app.send_key(Keys.TAB)
        assert activity.focus == "frames"

    def test_x_clears_entries(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)

        app.dispatch_event(CollectorFrame(_rx_frame()))
        app.dispatch_event(CollectorText("test line"))
        app.drain()

        assert len(activity._frame_entries) == 1
        assert len(activity._text_entries) == 1

        app.send_key(ord("x"))

        assert len(activity._frame_entries) == 0
        assert len(activity._text_entries) == 0
        mock_screen.assert_text_on_screen("0 frames")

    def test_question_mark_opens_help(self, app, mock_screen):
        activity = DebugLogActivity()
        app.start_activity(activity)
        assert app.activity_stack_depth() == 1

        app.send_key(ord("?"))

        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Help")
        mock_screen.assert_text_on_screen("Debug Log")

    def test_scroll_works(self, app, mock_screen):
        import curses
        activity = DebugLogActivity()
        app.start_activity(activity)

        for i in range(5):
            app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()

        initial = activity.display_state["frames"]["selected_index"]
        app.send_key(curses.KEY_DOWN)
        assert activity.display_state["frames"]["selected_index"] == initial + 1


class TestDebugLogMaxEntries:
    def test_entries_capped_at_max(self, app, mock_screen):
        activity = DebugLogActivity(max_entries=5)
        app.start_activity(activity)

        for i in range(10):
            app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()

        assert len(activity._frame_entries) == 5
