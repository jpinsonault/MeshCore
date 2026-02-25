"""Adversarial and edge-case tests for ChatActivity.

Covers: message edge cases, rapid-fire events, room scaling, command edge
cases, multi-activity navigation, and ChannelDiscovered events.
"""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.chat import ChatActivity
from collector.events import (
    ChannelDiscovered,
    ChannelMessage,
    CollectorConnected,
    CollectorFrame,
)
from collector.crypto import GroupMessage
from collector.protocol import FRAME_TYPE_RX_RAW


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chat():
    return ChatActivity(port="/dev/ttyUSB0", auto_start=False)


def _group_msg(sender="alice", text="hello everyone", channel="#meshcore",
               channel_hash=0xAA):
    return GroupMessage(
        timestamp=1700000000,
        sender=sender,
        text=text,
        channel_name=channel,
        channel_hash=channel_hash,
        raw_timestamp=time.time(),
    )


def _rx_frame(snr=5.0, rssi=-80):
    return {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": snr,
            "rssi": rssi,
            "route_type": 1,
            "route_name": "FLOOD",
            "payload_type": 5,
            "payload_name": "GRP_TXT",
            "raw_len": 20,
        },
    }


# ---------------------------------------------------------------------------
# TestMessageEdgeCases
# ---------------------------------------------------------------------------

class TestMessageEdgeCases:
    """Messages with unusual content should not crash."""

    def test_empty_text(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(text="")))
        app.drain()
        items = activity._message_items()
        assert len(items) >= 1  # at least system messages + the empty msg

    def test_long_text_200(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(text="A" * 200)))
        app.drain()
        assert any("A" * 20 in item for item in activity._message_items())

    def test_long_text_500(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(text="B" * 500)))
        app.drain()
        assert any("B" * 20 in item for item in activity._message_items())

    def test_unicode_emoji(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        text = "\U0001F525\U0001F680 mesh beacon"
        app.dispatch_event(ChannelMessage(_group_msg(text=text)))
        app.drain()
        assert any("mesh beacon" in item for item in activity._message_items())

    def test_null_bytes(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(text="hello\x00world")))
        app.drain()
        # Should not crash — content may be truncated at null
        assert len(activity._messages) >= 1

    def test_question_mark_sender(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(sender="?", text="orphan")))
        app.drain()
        assert any("orphan" in item for item in activity._message_items())

    def test_special_chars(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(text="line1\nline2\ttab\\slash")))
        app.drain()
        assert len(activity._messages) >= 1

    def test_empty_sender(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(sender="", text="no sender")))
        app.drain()
        assert any("no sender" in item for item in activity._message_items())


# ---------------------------------------------------------------------------
# TestRapidFireEvents
# ---------------------------------------------------------------------------

class TestRapidFireEvents:
    """Burst of events should all be processed."""

    def test_50_messages_burst(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        for i in range(50):
            app.dispatch_event(ChannelMessage(
                _group_msg(sender=f"user{i}", text=f"msg{i}", channel="#burst")
            ))
        app.drain()
        assert len(activity._messages) >= 50
        burst = [ch for ch in activity._channels if ch["name"] == "#burst"]
        assert len(burst) == 1
        assert burst[0]["msg_count"] == 50

    def test_100_messages_across_5_channels(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        for i in range(100):
            ch = f"#multi{i % 5}"
            app.dispatch_event(ChannelMessage(
                _group_msg(sender=f"u{i}", text=f"m{i}", channel=ch)
            ))
        app.drain()
        for j in range(5):
            ch_entry = [c for c in activity._channels if c["name"] == f"#multi{j}"]
            assert len(ch_entry) == 1
            assert ch_entry[0]["msg_count"] == 20
        assert len(activity._messages) == 100

    def test_interleaved_frames_and_messages(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        for i in range(25):
            app.dispatch_event(CollectorFrame(_rx_frame()))
            app.dispatch_event(ChannelMessage(
                _group_msg(sender=f"u{i}", text=f"m{i}", channel="#interleave")
            ))
        app.drain()
        assert activity._rx_count == 25
        interleave = [ch for ch in activity._channels if ch["name"] == "#interleave"]
        assert interleave[0]["msg_count"] == 25


# ---------------------------------------------------------------------------
# TestRoomManagement
# ---------------------------------------------------------------------------

class TestRoomManagement:
    """Sidebar with many channels."""

    def test_50_channels_in_sidebar(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": f"#room{i}", "msg_count": i} for i in range(50)]
        items = activity._sidebar_items()
        # Should have 50 channel lines + separator + status lines
        channel_items = [it for it in items if it.startswith("  ") and "#room" in it]
        assert len(channel_items) == 50

    def test_100_channels_no_crash(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": f"#big{i}", "msg_count": 0} for i in range(100)]
        items = activity._sidebar_items()
        assert len(items) > 100  # channels + separator + status

    def test_scroll_through_50_channels(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": f"#scroll{i}", "msg_count": 0} for i in range(50)]
        activity._update_display()
        activity._set_focus("split")
        activity.display_state["split"]["focused_panel"] = "left"
        activity.display_state["split"]["left_selected"] = 0

        # Scroll down 49 times
        for _ in range(49):
            app.send_key(0x102)  # KEY_DOWN
        assert activity.display_state["split"]["left_selected"] == 49

        # 10 more should stay clamped at 49
        for _ in range(10):
            app.send_key(0x102)
        assert activity.display_state["split"]["left_selected"] == 49

    def test_select_last_channel(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": f"#pick{i}", "msg_count": 0} for i in range(10)]
        activity._update_display()
        activity._set_focus("split")
        activity.display_state["split"]["focused_panel"] = "left"
        activity.display_state["split"]["left_selected"] = 9
        app.send_key(Keys.ENTER)
        assert activity._selected_channel == "#pick9"

    def test_deselect_restores_all(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": "#desel", "msg_count": 0}]
        activity._update_display()
        activity._set_focus("split")
        activity.display_state["split"]["focused_panel"] = "left"
        activity.display_state["split"]["left_selected"] = 0
        app.send_key(Keys.ENTER)
        assert activity._selected_channel == "#desel"
        app.send_key(Keys.ENTER)
        assert activity._selected_channel is None


# ---------------------------------------------------------------------------
# TestCommandEdgeCases
# ---------------------------------------------------------------------------

class TestCommandEdgeCases:
    """Edge cases in command dispatch."""

    def test_empty_submit(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        initial_sys = len(activity._system_messages)
        activity.display_state["command_input"]["text"] = ""
        activity._on_text_submit(None)
        app.drain()
        # No new system messages (empty text is a no-op)
        assert len(activity._system_messages) == initial_sys

    def test_spaces_only(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        initial_sys = len(activity._system_messages)
        activity.display_state["command_input"]["text"] = "   "
        activity._on_text_submit(None)
        app.drain()
        assert len(activity._system_messages) == initial_sys

    def test_very_long_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/" + "x" * 499
        activity._on_text_submit(None)
        app.drain()
        assert any("Unknown command" in m["text"] for m in activity._system_messages)

    def test_leading_spaces(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "  /help  "
        activity._on_text_submit(None)
        app.drain()
        assert any("Available commands" in m["text"] for m in activity._system_messages)

    def test_trailing_spaces_on_join(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        activity.display_state["command_input"]["text"] = "/join #trimtest   "
        activity._on_text_submit(None)
        app.drain()
        # Channel name should be trimmed
        assert any(ch["name"] == "#trimtest" for ch in activity._channels)

    def test_crack_without_service(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/crack status"
        activity._on_text_submit(None)
        app.drain()
        assert any("not available" in m["text"].lower() for m in activity._system_messages)

    def test_crack_unknown_subcommand(self, app, mock_screen):
        # We need a mock cracker — easiest is to test the dispatch path
        activity = _make_chat()
        app.start_activity(activity)
        # Without a collector service, crack is "not available"
        activity.display_state["command_input"]["text"] = "/crack banana"
        activity._on_text_submit(None)
        app.drain()
        # Should say not available (no service) or usage
        has_msg = any(
            "not available" in m["text"].lower() or "usage" in m["text"].lower()
            for m in activity._system_messages
        )
        assert has_msg


# ---------------------------------------------------------------------------
# TestMultiActivityNavigation
# ---------------------------------------------------------------------------

class TestMultiActivityNavigation:
    """Segue to another activity and back — chat state preserved."""

    def test_diag_and_back(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        # Send a channel message first
        app.dispatch_event(ChannelMessage(_group_msg(channel="#nav1")))
        app.drain()

        # Segue to diag
        activity.display_state["command_input"]["text"] = "/diag"
        activity._on_text_submit(None)
        app.drain()
        assert app.activity_stack_depth() == 2

        # Pop back
        app.send_key(Keys.ESC)
        app.drain()
        # Chat state should be preserved
        assert activity._service_started is True
        assert any(ch["name"] == "#nav1" for ch in activity._channels)

    def test_help_overlay_and_back(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._set_focus("split")
        app.send_key(ord("?"))
        app.drain()
        assert app.activity_stack_depth() == 2

        app.send_key(Keys.ESC)
        app.drain()
        assert activity._service_started is True

    def test_multiple_segues(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(channel="#persist")))
        app.drain()

        # Go to nodes
        activity.display_state["command_input"]["text"] = "/nodes"
        activity._on_text_submit(None)
        app.drain()
        assert app.activity_stack_depth() == 2
        app.send_key(Keys.ESC)
        app.drain()

        # Go to packets
        activity.display_state["command_input"]["text"] = "/packets"
        activity._on_text_submit(None)
        app.drain()
        assert app.activity_stack_depth() == 2
        app.send_key(Keys.ESC)
        app.drain()

        # Chat state still intact
        assert activity._service_started is True
        assert any(ch["name"] == "#persist" for ch in activity._channels)


# ---------------------------------------------------------------------------
# TestChannelDiscoveredEvent
# ---------------------------------------------------------------------------

class TestChannelDiscoveredEvent:
    """ChannelDiscovered events from the cracker."""

    def test_rapid_discoveries(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        for i in range(5):
            app.dispatch_event(ChannelDiscovered(f"#disc{i}", decoded_count=i + 1))
        app.drain()

        for i in range(5):
            matches = [ch for ch in activity._channels if ch["name"] == f"#disc{i}"]
            assert len(matches) == 1
            assert matches[0]["msg_count"] == i + 1

    def test_discovered_while_viewing_all(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        assert activity._selected_channel is None  # viewing "All"
        app.dispatch_event(ChannelDiscovered("#found_all", decoded_count=3))
        app.drain()
        assert any("Cracked channel #found_all" in m["text"] for m in activity._system_messages)

    def test_discovered_for_selected_channel(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": "#existing", "msg_count": 2}]
        activity._selected_channel = "#existing"
        app.dispatch_event(ChannelDiscovered("#existing", decoded_count=5))
        app.drain()
        existing = [ch for ch in activity._channels if ch["name"] == "#existing"]
        assert existing[0]["msg_count"] == 7  # 2 + 5

    def test_duplicate_discovery_no_dup_sidebar(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        app.dispatch_event(ChannelDiscovered("#dup_disc", decoded_count=1))
        app.dispatch_event(ChannelDiscovered("#dup_disc", decoded_count=2))
        app.drain()
        matches = [ch for ch in activity._channels if ch["name"] == "#dup_disc"]
        assert len(matches) == 1
        assert matches[0]["msg_count"] == 3  # 1 + 2
