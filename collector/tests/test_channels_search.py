"""Tests for the channel browser search bar."""

import time
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.channels import ChannelBrowserActivity
from collector.events import ChannelMessage
from collector.crypto import GroupMessage


class MockStore:
    """Minimal mock of CollectorStore with search support."""

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

    def search_channel_messages(self, channel_name=None, search_text=None,
                                sender=None, limit=200, offset=0):
        msgs = self._messages
        if channel_name:
            msgs = [m for m in msgs if m["channel_name"] == channel_name]
        if search_text:
            q = search_text.lower()
            msgs = [m for m in msgs if
                    (m.get("text") and q in m["text"].lower()) or
                    (m.get("sender") and q in m["sender"].lower())]
        if sender:
            q = sender.lower()
            msgs = [m for m in msgs if m.get("sender") and q in m["sender"].lower()]
        return msgs[offset:offset + limit]


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


def _make_activity(channels=None, messages=None):
    store = MockStore(channels=channels, messages=messages)
    return ChannelBrowserActivity(store=store)


class TestSearchActivation:
    def test_slash_opens_search(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)
        assert not activity._search_active

        app.send_key(ord("/"))
        assert activity._search_active
        assert activity.focus == "search_input"

    def test_esc_closes_search(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)

        app.send_key(ord("/"))
        assert activity._search_active

        app.send_key(Keys.ESC)
        assert not activity._search_active
        assert activity.focus == "channel_list"

    def test_esc_without_search_pops_activity(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)
        assert app.activity_stack_depth() == 1

        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_search_bar_hidden_initially(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)
        assert activity.display_state["search_input"].get("hidden") is True

    def test_search_bar_visible_when_active(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)
        app.send_key(ord("/"))
        assert activity.display_state["search_input"].get("hidden") is not True


class TestSearchFiltering:
    def test_typing_filters_messages(self, app, mock_screen):
        messages = [
            _message("Alice", "Hello world"),
            _message("Bob", "Goodbye"),
            _message("Alice", "Hello mesh"),
        ]
        channels = [_channel_summary()]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        # Open search and type "Hello"
        app.send_key(ord("/"))
        for ch in "Hello":
            app.send_key(ord(ch))

        # Should filter to 2 messages
        assert len(activity._messages) == 2

    def test_search_clears_on_close(self, app, mock_screen):
        messages = [
            _message("Alice", "Hello"),
            _message("Bob", "World"),
        ]
        channels = [_channel_summary()]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        app.send_key(ord("/"))
        for ch in "Hello":
            app.send_key(ord(ch))
        assert len(activity._messages) == 1

        app.send_key(Keys.ESC)
        assert activity._search_text == ""
        assert len(activity._messages) == 2

    def test_enter_locks_search_and_moves_focus(self, app, mock_screen):
        messages = [_message("Alice", "test")]
        channels = [_channel_summary()]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        app.send_key(ord("/"))
        for ch in "test":
            app.send_key(ord(ch))
        app.send_key(Keys.ENTER)

        assert activity.focus == "messages"
        assert activity._search_text == "test"


class TestSearchStatusText:
    def test_match_count_shown(self, app, mock_screen):
        messages = [
            _message("Alice", "match me"),
            _message("Bob", "no"),
            _message("Eve", "match this"),
        ]
        channels = [_channel_summary()]
        activity = _make_activity(channels=channels, messages=messages)
        app.start_activity(activity)

        app.send_key(ord("/"))
        for ch in "match":
            app.send_key(ord(ch))

        mock_screen.assert_text_on_screen("2 matches")


class TestSearchLiveUpdates:
    def test_matching_live_message_appears(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)

        app.send_key(ord("/"))
        for ch in "mesh":
            app.send_key(ord(ch))

        # Live message that matches search
        msg = _group_msg("Alice", "mesh network", "Public")
        app.dispatch_event(ChannelMessage(msg))
        app.drain()

        assert any("mesh network" in m.get("text", "") for m in activity._messages)

    def test_non_matching_live_message_excluded(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)

        app.send_key(ord("/"))
        for ch in "mesh":
            app.send_key(ord(ch))

        # Live message that does NOT match
        msg = _group_msg("Bob", "hello world", "Public")
        app.dispatch_event(ChannelMessage(msg))
        app.drain()

        assert not any("hello world" in m.get("text", "") for m in activity._messages)


class TestSearchHelpText:
    def test_bottom_bar_shows_search_hint(self, app, mock_screen):
        activity = _make_activity()
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("/:search")
