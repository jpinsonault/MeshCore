"""Tests for the ChatActivity — IRC-style main screen."""

import tempfile
import time
from unittest.mock import MagicMock
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.chat import ChatActivity, _fmt_time, _fmt_uptime_short
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


def _make_chat():
    """Create a ChatActivity for testing (no serial port needed)."""
    return ChatActivity(port="/dev/ttyUSB0", auto_start=False)


def _group_msg(sender="alice", text="hello everyone", channel="#meshcore"):
    return GroupMessage(
        timestamp=1700000000,
        sender=sender,
        text=text,
        channel_name=channel,
        channel_hash=0xAA,
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


def _adv_frame(name="TestNode", pk="aa" * 32, snr=5.0):
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
        },
    }


def _hb_frame(uptime=3600, battery=3700):
    return {
        "type": FRAME_TYPE_HEARTBEAT,
        "received_at": time.time(),
        "parsed": {
            "timestamp": 1700000000,
            "battery_mv": battery,
            "rx_flood": 50,
            "rx_direct": 20,
            "tx_flood": 40,
            "tx_direct": 10,
            "free_pkts": 8,
            "uptime_secs": uptime,
        },
    }


class TestFormatHelpers:
    def test_fmt_time_valid(self):
        result = _fmt_time(1700000000)
        assert ":" in result  # HH:MM format

    def test_fmt_time_none(self):
        assert _fmt_time(None) == "---"

    def test_fmt_uptime_short_seconds(self):
        assert _fmt_uptime_short(45) == "45s"

    def test_fmt_uptime_short_minutes(self):
        assert _fmt_uptime_short(125) == "2m5s"

    def test_fmt_uptime_short_hours(self):
        assert _fmt_uptime_short(3665) == "1h1m"


class TestChatRendering:
    def test_shows_title_and_port(self, app, mock_screen):
        app.start_activity(_make_chat())
        mock_screen.assert_text_on_screen("MeshCore Collector")
        mock_screen.assert_text_on_screen("/dev/ttyUSB0")

    def test_shows_rooms_panel(self, app, mock_screen):
        app.start_activity(_make_chat())
        mock_screen.assert_text_on_screen("Rooms")

    def test_shows_command_input(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        assert "command_input" in activity.display_state
        assert activity.display_state["command_input"]["label"] == "> "

    def test_shows_system_messages(self, app, mock_screen):
        app.start_activity(_make_chat())
        mock_screen.assert_text_on_screen("Connected to")
        mock_screen.assert_text_on_screen("/help")

    def test_shows_no_channels_hint(self, app, mock_screen):
        """When no channels are configured, shows hint text."""
        activity = _make_chat()
        app.start_activity(activity)
        # Clear channels after start (config may have loaded some)
        activity._channels = []
        items = activity._sidebar_items()
        assert any("no channels" in item for item in items)

    def test_shows_status_block(self, app, mock_screen):
        app.start_activity(_make_chat())
        mock_screen.assert_text_on_screen("nodes")
        mock_screen.assert_text_on_screen("p/s")

    def test_shows_bottom_bar(self, app, mock_screen):
        app.start_activity(_make_chat())
        mock_screen.assert_text_on_screen("TAB:rooms")


class TestChatEvents:
    def test_connected_event(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(CollectorConnected())
        app.drain()
        assert activity._connected is True

    def test_disconnected_event(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(CollectorDisconnected("cable unplugged"))
        app.drain()
        assert activity._connected is False
        mock_screen.assert_text_on_screen("Disconnected")

    def test_error_event(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(CollectorError("timeout"))
        app.drain()
        mock_screen.assert_text_on_screen("Error")

    def test_rx_frame_increments_counter(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(CollectorFrame(_rx_frame()))
        app.drain()
        assert activity._rx_count == 1

    def test_heartbeat_updates_stats(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(CollectorFrame(_hb_frame(uptime=3600, battery=3700)))
        app.drain()
        assert activity._last_heartbeat is not None
        mock_screen.assert_text_on_screen("3700mV")

    def test_channel_message_updates_sidebar(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        initial_count = len(activity._channels)
        app.dispatch_event(ChannelMessage(_group_msg(channel="#livechan")))
        app.drain()
        # Channel should appear in sidebar
        assert any(ch["name"] == "#livechan" for ch in activity._channels)
        live = [ch for ch in activity._channels if ch["name"] == "#livechan"][0]
        assert live["msg_count"] == 1

    def test_channel_message_appears_in_messages(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelMessage(_group_msg(sender="alice", text="hello everyone")))
        app.drain()
        mock_screen.assert_text_on_screen("alice")
        mock_screen.assert_text_on_screen("hello everyone")

    def test_multiple_channel_messages(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        initial_count = len(activity._channels)
        app.dispatch_event(ChannelMessage(_group_msg(channel="#alpha")))
        app.dispatch_event(ChannelMessage(_group_msg(channel="#beta")))
        app.dispatch_event(ChannelMessage(_group_msg(channel="#alpha")))
        app.drain()
        alpha = [ch for ch in activity._channels if ch["name"] == "#alpha"]
        beta = [ch for ch in activity._channels if ch["name"] == "#beta"]
        assert len(alpha) == 1
        assert alpha[0]["msg_count"] == 2
        assert len(beta) == 1
        assert beta[0]["msg_count"] == 1


class TestChatKeyboard:
    def test_esc_from_split_pops_activity(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        # First switch to split focus
        activity._set_focus("split")
        activity.display_state["split"]["focused_panel"] = "left"
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_esc_from_command_input_goes_to_split(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        assert activity.focus == "command_input"
        app.send_key(Keys.ESC)
        assert activity.focus == "split"

    def test_tab_cycles_focus(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        # Start at command_input
        assert activity.focus == "command_input"
        # TAB -> split (left)
        app.send_key(Keys.TAB)
        assert activity.focus == "split"
        assert activity.display_state["split"]["focused_panel"] == "left"
        # TAB -> split (right)
        app.send_key(Keys.TAB)
        assert activity.focus == "split"
        assert activity.display_state["split"]["focused_panel"] == "right"
        # TAB -> command_input
        app.send_key(Keys.TAB)
        assert activity.focus == "command_input"

    def test_q_quits_when_not_typing(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        # Switch to split so q quits
        activity._set_focus("split")
        app.send_key(ord("q"))
        assert len(app.flush_stop_events()) >= 1

    def test_q_types_in_command_input(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        assert activity.focus == "command_input"
        # q should be typed, not quit
        app.send_key(ord("q"))
        # Application should still be running
        assert app.activity_stack_depth() >= 1

    def test_question_mark_opens_help(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._set_focus("split")
        app.send_key(ord("?"))
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Help")

    def test_enter_selects_channel(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []  # clear config channels
        # Add a channel via message
        app.dispatch_event(ChannelMessage(_group_msg(channel="#seltest")))
        app.drain()
        # Focus split left panel
        activity._set_focus("split")
        activity.display_state["split"]["focused_panel"] = "left"
        activity.display_state["split"]["left_selected"] = 0
        # ENTER to select
        app.send_key(Keys.ENTER)
        assert activity._selected_channel == "#seltest"

    def test_enter_deselects_channel(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        app.dispatch_event(ChannelMessage(_group_msg(channel="#seltest2")))
        app.drain()
        activity._set_focus("split")
        activity.display_state["split"]["focused_panel"] = "left"
        activity.display_state["split"]["left_selected"] = 0
        app.send_key(Keys.ENTER)
        assert activity._selected_channel == "#seltest2"
        app.send_key(Keys.ENTER)
        assert activity._selected_channel is None


class TestChatCommands:
    def test_help_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/help"
        activity._on_text_submit(None)
        app.drain()
        mock_screen.assert_text_on_screen("Available commands")
        mock_screen.assert_text_on_screen("/join")

    def test_status_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/status"
        activity._on_text_submit(None)
        app.drain()
        mock_screen.assert_text_on_screen("Port:")
        mock_screen.assert_text_on_screen("/dev/ttyUSB0")

    def test_join_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        # Use a unique channel name to avoid collisions with user config
        activity._channels = []  # clear any config-loaded channels
        activity.display_state["command_input"]["text"] = "/join #testjoin1"
        activity._on_text_submit(None)
        app.drain()
        assert any(ch["name"] == "#testjoin1" for ch in activity._channels)
        # Check system messages contain the join notice
        assert any("Joined #testjoin1" in m["text"] for m in activity._system_messages)

    def test_join_without_hash(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        activity.display_state["command_input"]["text"] = "/join testjoin2"
        activity._on_text_submit(None)
        app.drain()
        assert any(ch["name"] == "#testjoin2" for ch in activity._channels)

    def test_join_duplicate(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        activity.display_state["command_input"]["text"] = "/join #testdup"
        activity._on_text_submit(None)
        activity.display_state["command_input"]["text"] = "/join #testdup"
        activity._on_text_submit(None)
        app.drain()
        count = sum(1 for ch in activity._channels if ch["name"] == "#testdup")
        assert count == 1
        assert any("Already joined" in m["text"] for m in activity._system_messages)

    def test_join_no_arg(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/join"
        activity._on_text_submit(None)
        app.drain()
        mock_screen.assert_text_on_screen("Usage:")

    def test_part_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        # Join first
        activity.display_state["command_input"]["text"] = "/join #parttest"
        activity._on_text_submit(None)
        assert any(ch["name"] == "#parttest" for ch in activity._channels)
        # Then part
        activity.display_state["command_input"]["text"] = "/part #parttest"
        activity._on_text_submit(None)
        app.drain()
        assert not any(ch["name"] == "#parttest" for ch in activity._channels)
        assert any("Left #parttest" in m["text"] for m in activity._system_messages)

    def test_part_selected_channel(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        activity.display_state["command_input"]["text"] = "/join #partsel"
        activity._on_text_submit(None)
        activity._selected_channel = "#partsel"
        activity.display_state["command_input"]["text"] = "/part"
        activity._on_text_submit(None)
        app.drain()
        assert not any(ch["name"] == "#partsel" for ch in activity._channels)
        assert activity._selected_channel is None

    def test_part_no_selection(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/part"
        activity._on_text_submit(None)
        app.drain()
        mock_screen.assert_text_on_screen("Usage:")

    def test_search_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/search hello"
        activity._on_text_submit(None)
        app.drain()
        assert activity._search_text == "hello"
        mock_screen.assert_text_on_screen("Searching for: hello")

    def test_search_clear(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._search_text = "old"
        activity.display_state["command_input"]["text"] = "/search"
        activity._on_text_submit(None)
        app.drain()
        assert activity._search_text == ""
        mock_screen.assert_text_on_screen("Search cleared")

    def test_diag_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/diag"
        activity._on_text_submit(None)
        app.drain()
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("System Diagnostics")

    def test_packets_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/packets"
        activity._on_text_submit(None)
        app.drain()
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Packets")

    def test_nodes_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/nodes"
        activity._on_text_submit(None)
        app.drain()
        assert app.activity_stack_depth() == 2
        mock_screen.assert_text_on_screen("Nodes")

    def test_unknown_command(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/badcmd"
        activity._on_text_submit(None)
        app.drain()
        mock_screen.assert_text_on_screen("Unknown command")

    def test_command_input_clears_after_submit(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/help"
        activity._on_text_submit(None)
        assert activity.display_state["command_input"]["text"] == ""


class TestChatSystemMessages:
    def test_system_messages_have_star_prefix(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._add_system_message("Test message")
        activity._update_display()
        app.drain()
        # System messages should show with * prefix
        items = activity._message_items()
        assert any("* Test message" in item for item in items)

    def test_initial_system_messages(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        assert len(activity._system_messages) >= 2  # connected + help hint

    def test_system_messages_inline_in_chat(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        # System messages appear in the same message list
        items = activity._message_items()
        system_items = [i for i in items if "* " in i]
        assert len(system_items) >= 2


class TestChatSidebarClamping:
    def test_left_selected_clamped_to_channels(self, app, mock_screen):
        """UP/DOWN should not select status lines below channels."""
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": "#only", "msg_count": 0}]
        activity._update_display()
        # Focus split left
        activity._set_focus("split")
        activity.display_state["split"]["focused_panel"] = "left"
        activity.display_state["split"]["left_selected"] = 0
        # Try to scroll down past the channel
        for _ in range(10):
            app.send_key(0x102)  # KEY_DOWN
        # Should be clamped to last channel index (only 1 channel, index 0)
        assert activity.display_state["split"]["left_selected"] == 0


class TestChatReentry:
    def test_service_persists_across_segue(self, app, mock_screen):
        """Service stays running when segue to diag and back."""
        activity = _make_chat()
        app.start_activity(activity)
        assert activity._service_started is True
        # Simulate segue
        activity.on_stop()
        activity.on_start()
        app.drain()
        # Service should still be marked started
        assert activity._service_started is True


def _setup_with_mock_service(app):
    """Start a ChatActivity and register a mock collector service."""
    activity = _make_chat()
    app.start_activity(activity)
    mock_svc = MagicMock()
    mock_svc.send_message = MagicMock(return_value=True)
    app._services["collector"] = mock_svc
    mock_svc._application = app
    return activity, mock_svc


class TestChatSend:
    def test_bare_text_sends_message(self, app, mock_screen):
        """Bare text in command input sends to selected channel."""
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity._sender_name = "collector"
        activity._selected_channel = "#test"
        activity.display_state["command_input"]["text"] = "hello world"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_called_once_with("#test", "collector", "hello world")

    def test_bare_text_no_channel_shows_error(self, app, mock_screen):
        """Bare text without a selected channel shows error."""
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity._selected_channel = None
        activity.display_state["command_input"]["text"] = "hello"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_not_called()
        assert any("Select a channel" in m["text"] for m in activity._system_messages)

    def test_bare_text_not_connected_shows_error(self, app, mock_screen):
        """Bare text when disconnected shows error."""
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = False
        activity._selected_channel = "#test"
        activity.display_state["command_input"]["text"] = "hello"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_not_called()
        assert any("Not connected" in m["text"] for m in activity._system_messages)

    def test_bare_text_send_failure(self, app, mock_screen):
        """When send returns False, shows failure message."""
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity._selected_channel = "#test"
        mock_svc.send_message.return_value = False
        activity.display_state["command_input"]["text"] = "hello"
        activity._on_text_submit(None)
        app.drain()
        assert any("Failed to send" in m["text"] for m in activity._system_messages)

    def test_bare_text_uses_sender_name(self, app, mock_screen):
        """Send uses the configured sender name."""
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity._selected_channel = "#test"
        activity._sender_name = "alice"
        activity.display_state["command_input"]["text"] = "hi"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_called_once_with("#test", "alice", "hi")


class TestChatNick:
    def test_nick_sets_name(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/nick alice"
        activity._on_text_submit(None)
        app.drain()
        assert activity._sender_name == "alice"
        assert any("Nick set to: alice" in m["text"] for m in activity._system_messages)

    def test_nick_no_arg_shows_current(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._sender_name = "bob"
        activity.display_state["command_input"]["text"] = "/nick"
        activity._on_text_submit(None)
        app.drain()
        assert any("Current nick: bob" in m["text"] for m in activity._system_messages)

    def test_nick_takes_first_word(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/nick alice wonderland"
        activity._on_text_submit(None)
        app.drain()
        assert activity._sender_name == "alice"


class TestChatSendCommand:
    def test_send_to_specified_channel(self, app, mock_screen):
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity._sender_name = "collector"
        activity.display_state["command_input"]["text"] = "/send #other hello world"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_called_once_with("#other", "collector", "hello world")

    def test_send_to_selected_channel(self, app, mock_screen):
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity._sender_name = "collector"
        activity._selected_channel = "#test"
        activity.display_state["command_input"]["text"] = "/send just some text"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_called_once_with("#test", "collector", "just some text")

    def test_send_no_channel_shows_error(self, app, mock_screen):
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity._selected_channel = None
        activity.display_state["command_input"]["text"] = "/send hello"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_not_called()
        assert any("No channel" in m["text"] for m in activity._system_messages)

    def test_send_no_arg_shows_usage(self, app, mock_screen):
        activity, mock_svc = _setup_with_mock_service(app)
        activity._connected = True
        activity.display_state["command_input"]["text"] = "/send"
        activity._on_text_submit(None)
        app.drain()
        mock_svc.send_message.assert_not_called()
        assert any("Usage:" in m["text"] for m in activity._system_messages)
