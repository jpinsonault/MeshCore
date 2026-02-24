"""Tests for the ChannelBrowserActivity."""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.channels import ChannelBrowserActivity
from collector.events import ChannelMessage
from collector.crypto import GroupMessage


class MockStore:
    """Minimal mock of CollectorStore for testing."""

    def __init__(self, channels=None, messages=None):
        self._channels = channels or []
        self._messages = messages or []
        self._count = len(self._messages)

    def get_channel_summary(self):
        return list(self._channels)

    def get_channel_messages(self, channel_name=None, limit=200):
        msgs = self._messages
        if channel_name:
            msgs = [m for m in msgs if m["channel_name"] == channel_name]
        return msgs[:limit]

    def get_channel_message_count(self):
        return self._count


def _make_activity(channels=None, messages=None):
    """Create a ChannelBrowserActivity with mock store."""
    store = MockStore(channels=channels, messages=messages)
    return ChannelBrowserActivity(store=store)


def _channel_summary(name="Public", count=5, senders=2):
    return {
        "channel_name": name,
        "channel_hash": 0xAA,
        "msg_count": count,
        "last_activity": time.time(),
        "unique_senders": senders,
    }


def _message(sender="Alice", text="Hello", channel="Public"):
    return {
        "timestamp": time.time(),
        "msg_timestamp": 1700000000,
        "sender": sender,
        "text": text,
        "channel_name": channel,
    }


def _group_msg(sender="Alice", text="Hello", channel="Public"):
    return GroupMessage(
        timestamp=1700000000,
        sender=sender,
        text=text,
        channel_name=channel,
        channel_hash=0xAA,
        raw_timestamp=time.time(),
    )


class TestRendering:
    def test_shows_title(self, app, mock_screen):
        app.start_activity(_make_activity())
        mock_screen.assert_text_on_screen("Channel Browser")

    def test_empty_state(self, app, mock_screen):
        app.start_activity(_make_activity())
        mock_screen.assert_text_on_screen("no channels decoded")

    def test_shows_channel_list(self, app, mock_screen):
        channels = [_channel_summary("Public", 42, 3)]
        app.start_activity(_make_activity(channels=channels))
        mock_screen.assert_text_on_screen("Public")
        mock_screen.assert_text_on_screen("42 msgs")

    def test_shows_messages(self, app, mock_screen):
        channels = [_channel_summary("Public")]
        messages = [_message("Alice", "Hello everyone!")]
        app.start_activity(_make_activity(channels=channels, messages=messages))
        mock_screen.assert_text_on_screen("Alice")
        mock_screen.assert_text_on_screen("Hello everyone!")

    def test_shows_decoded_count(self, app, mock_screen):
        messages = [_message()] * 3
        channels = [_channel_summary()]
        store = MockStore(channels=channels, messages=messages)
        store._count = 42
        activity = ChannelBrowserActivity(store=store)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("42 msgs decoded")

    def test_shows_help_keys(self, app, mock_screen):
        app.start_activity(_make_activity())
        mock_screen.assert_text_on_screen("TAB")
        mock_screen.assert_text_on_screen("ENTER")
        mock_screen.assert_text_on_screen("ESC")


class TestNavigation:
    def test_esc_pops_activity(self, app, mock_screen):
        app.start_activity(_make_activity())
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_tab_cycles_focus(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)
        assert activity.focus == "channel_list"
        app.send_key(Keys.TAB)
        assert activity.focus == "messages"
        app.send_key(Keys.TAB)
        assert activity.focus == "channel_list"

    def test_enter_selects_channel(self, app, mock_screen):
        channels = [
            _channel_summary("Public", 10, 2),
            _channel_summary("Private", 5, 1),
        ]
        messages = [
            _message("Alice", "pub msg", "Public"),
            _message("Bob", "priv msg", "Private"),
        ]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        # Select first channel (Public)
        app.send_key(Keys.ENTER)
        assert activity._selected_channel == "Public"

    def test_enter_deselects_channel(self, app, mock_screen):
        channels = [_channel_summary("Public")]
        activity = _make_activity(channels=channels)
        app.start_activity(activity)

        # Select
        app.send_key(Keys.ENTER)
        assert activity._selected_channel == "Public"
        # Deselect
        app.send_key(Keys.ENTER)
        assert activity._selected_channel is None

    def test_r_refreshes(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)
        # Shouldn't crash
        app.send_key(ord("r"))


class TestLiveUpdates:
    def test_channel_message_event_updates_display(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)

        msg = _group_msg("Eve", "Live message!")
        app.dispatch_event(ChannelMessage(msg))
        app.drain()

        mock_screen.assert_text_on_screen("Eve")
        mock_screen.assert_text_on_screen("Live message!")
        assert activity._total_count == 1

    def test_channel_message_adds_new_channel(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)

        msg = _group_msg(channel="NewChannel")
        app.dispatch_event(ChannelMessage(msg))
        app.drain()

        assert len(activity._channels) == 1
        assert activity._channels[0]["channel_name"] == "NewChannel"

    def test_channel_message_updates_existing_channel(self, app, mock_screen):
        channels = [_channel_summary("Public", 5, 2)]
        activity = _make_activity(channels=channels)
        app.start_activity(activity)

        msg = _group_msg(channel="Public")
        app.dispatch_event(ChannelMessage(msg))
        app.drain()

        assert activity._channels[0]["msg_count"] == 6


class TestNullStore:
    def test_none_store_does_not_crash(self, app, mock_screen):
        """Activity with store=None should render empty state without crashing."""
        activity = ChannelBrowserActivity(store=None)
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Channel Browser")
        mock_screen.assert_text_on_screen("no channels decoded")


class TestFilteredLiveUpdates:
    def test_filtered_msg_from_other_channel_excluded(self, app, mock_screen):
        """When channel A is selected, live messages for channel B should not appear in messages."""
        channels = [
            _channel_summary("ChanA", 3, 1),
            _channel_summary("ChanB", 2, 1),
        ]
        messages = [
            _message("Alice", "msg1", "ChanA"),
            _message("Bob", "msg2", "ChanB"),
        ]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        # Select ChanA
        app.send_key(Keys.ENTER)
        assert activity._selected_channel == "ChanA"

        # Live message for ChanB
        msg = _group_msg(sender="Eve", text="filtered out", channel="ChanB")
        app.dispatch_event(ChannelMessage(msg))
        app.drain()

        # Eve's message should NOT appear (filtered to ChanA)
        assert not any("filtered out" in m.get("text", "") for m in activity._messages)
        # But total count should still increment
        assert activity._total_count == 3  # initial 2 + 1 live

    def test_filtered_msg_from_same_channel_included(self, app, mock_screen):
        """When channel A is selected, live messages for channel A appear."""
        channels = [_channel_summary("ChanA", 1, 1)]
        messages = [_message("Alice", "existing", "ChanA")]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        # Select ChanA
        app.send_key(Keys.ENTER)

        msg = _group_msg(sender="Bob", text="included", channel="ChanA")
        app.dispatch_event(ChannelMessage(msg))
        app.drain()

        assert any("included" in m.get("text", "") for m in activity._messages)


class TestScrollBehavior:
    def test_enter_on_empty_channel_list(self, app, mock_screen):
        """ENTER on empty channel list should not crash."""
        activity = _make_activity()
        app.start_activity(activity)
        # No channels — ENTER should be harmless
        app.send_key(Keys.ENTER)
        assert activity._selected_channel is None

    def test_scroll_down_in_messages(self, app, mock_screen):
        """Scroll down in the messages list."""
        import curses
        messages = [_message(f"User{i}", f"msg{i}") for i in range(10)]
        channels = [_channel_summary()]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        # Switch focus to messages
        app.send_key(Keys.TAB)
        assert activity.focus == "messages"

        # Scroll down
        initial = activity.display_state["messages"]["selected_index"]
        app.send_key(curses.KEY_UP)
        assert activity.display_state["messages"]["selected_index"] == initial - 1
