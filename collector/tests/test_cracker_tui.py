"""Tests for TUI integration of the ChannelCracker — ChannelDiscovered events, /crack command."""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.chat import ChatActivity
from collector.events import ChannelDiscovered, ChannelMessage
from collector.crypto import GroupMessage


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


class TestChannelDiscoveredEvent:
    def test_discovered_adds_channel_to_sidebar(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        initial = len(activity._channels)
        app.dispatch_event(ChannelDiscovered("#cracked", 5))
        app.drain()
        assert any(ch["name"] == "#cracked" for ch in activity._channels)
        cracked = [ch for ch in activity._channels if ch["name"] == "#cracked"][0]
        assert cracked["msg_count"] == 5

    def test_discovered_shows_system_message(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelDiscovered("#hiking", 14))
        app.drain()
        assert any(
            "Cracked channel #hiking" in m["text"] and "14 messages" in m["text"]
            for m in activity._system_messages
        )

    def test_discovered_updates_existing_channel(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = [{"name": "#hiking", "msg_count": 2}]
        app.dispatch_event(ChannelDiscovered("#hiking", 10))
        app.drain()
        ch = [c for c in activity._channels if c["name"] == "#hiking"][0]
        assert ch["msg_count"] == 12  # 2 + 10

    def test_discovered_does_not_duplicate_channel(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        app.dispatch_event(ChannelDiscovered("#newchan", 3))
        app.dispatch_event(ChannelDiscovered("#newchan", 2))
        app.drain()
        count = sum(1 for ch in activity._channels if ch["name"] == "#newchan")
        assert count == 1


class TestCrackCommand:
    def test_crack_status_no_cracker(self, app, mock_screen):
        """When no collector service is running, /crack shows an error."""
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/crack"
        activity._on_text_submit(None)
        app.drain()
        assert any("not available" in m["text"] for m in activity._system_messages)

    def test_crack_unknown_subcommand_no_service(self, app, mock_screen):
        """Without a collector service, /crack badarg shows 'not available'."""
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/crack badarg"
        activity._on_text_submit(None)
        app.drain()
        assert any("not available" in m["text"] for m in activity._system_messages)


class TestEnhancedJoin:
    def test_join_hashtag(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        activity.display_state["command_input"]["text"] = "/join #newtest"
        activity._on_text_submit(None)
        app.drain()
        assert any(ch["name"] == "#newtest" for ch in activity._channels)
        assert any("Joined #newtest" in m["text"] for m in activity._system_messages)

    def test_join_without_hash_prefix(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        activity.display_state["command_input"]["text"] = "/join newtest2"
        activity._on_text_submit(None)
        app.drain()
        assert any(ch["name"] == "#newtest2" for ch in activity._channels)

    def test_join_no_args_shows_usage(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/join"
        activity._on_text_submit(None)
        app.drain()
        assert any("Usage:" in m["text"] for m in activity._system_messages)

    def test_join_duplicate_rejected(self, app, mock_screen):
        activity = _make_chat()
        app.start_activity(activity)
        activity._channels = []
        activity.display_state["command_input"]["text"] = "/join #duptest"
        activity._on_text_submit(None)
        activity.display_state["command_input"]["text"] = "/join #duptest"
        activity._on_text_submit(None)
        app.drain()
        count = sum(1 for ch in activity._channels if ch["name"] == "#duptest")
        assert count == 1
        assert any("Already joined" in m["text"] for m in activity._system_messages)

    def test_help_shows_crack_and_psk(self, app, mock_screen):
        """Updated /help should mention /crack and PSK join."""
        activity = _make_chat()
        app.start_activity(activity)
        activity.display_state["command_input"]["text"] = "/help"
        activity._on_text_submit(None)
        app.drain()
        help_texts = [m["text"] for m in activity._system_messages]
        all_text = " ".join(help_texts)
        assert "/crack" in all_text
        assert "b64psk" in all_text or "PSK" in all_text
