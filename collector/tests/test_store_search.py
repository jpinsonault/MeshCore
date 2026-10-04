"""Tests for search_channel_messages() in the storage layer."""

import tempfile
import time
import pytest

from collector.store import CollectorStore
from collector.crypto import GroupMessage


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        s = CollectorStore(f.name)
        s.open()
        yield s
        s.close()


def _msg(channel="Public", sender="Alice", text="Hello", ts=1700000000, hash=0xAA):
    return GroupMessage(
        timestamp=ts,
        sender=sender,
        text=text,
        channel_name=channel,
        channel_hash=hash,
        raw_timestamp=time.time(),
    )


class TestSearchText:
    def test_basic_text_search(self, store):
        store.store_channel_message(_msg(text="Hello world"))
        store.store_channel_message(_msg(text="Goodbye world"))
        store.store_channel_message(_msg(text="Nothing here"))

        results = store.search_channel_messages(search_text="world")
        assert len(results) == 2
        assert all("world" in r["text"] for r in results)

    def test_case_insensitive(self, store):
        store.store_channel_message(_msg(text="HELLO WORLD"))
        store.store_channel_message(_msg(text="hello world"))

        results = store.search_channel_messages(search_text="hello")
        assert len(results) == 2

    def test_text_search_matches_sender_too(self, store):
        """search_text searches both text and sender columns."""
        store.store_channel_message(_msg(sender="Alice", text="Nothing"))
        results = store.search_channel_messages(search_text="Alice")
        assert len(results) == 1

    def test_no_match_returns_empty(self, store):
        store.store_channel_message(_msg(text="Hello"))
        results = store.search_channel_messages(search_text="nonexistent")
        assert results == []


class TestSearchSender:
    def test_sender_filter(self, store):
        store.store_channel_message(_msg(sender="Alice"))
        store.store_channel_message(_msg(sender="Bob"))
        store.store_channel_message(_msg(sender="Alice2"))

        results = store.search_channel_messages(sender="Alice")
        assert len(results) == 2  # "Alice" and "Alice2"

    def test_sender_case_insensitive(self, store):
        store.store_channel_message(_msg(sender="ALICE"))
        results = store.search_channel_messages(sender="alice")
        assert len(results) == 1


class TestSearchCombined:
    def test_channel_plus_text(self, store):
        store.store_channel_message(_msg(channel="Public", text="Hello"))
        store.store_channel_message(_msg(channel="Private", text="Hello"))
        store.store_channel_message(_msg(channel="Public", text="Bye"))

        results = store.search_channel_messages(channel_name="Public", search_text="Hello")
        assert len(results) == 1
        assert results[0]["channel_name"] == "Public"
        assert results[0]["text"] == "Hello"

    def test_channel_plus_sender(self, store):
        store.store_channel_message(_msg(channel="Public", sender="Alice"))
        store.store_channel_message(_msg(channel="Public", sender="Bob"))
        store.store_channel_message(_msg(channel="Private", sender="Alice"))

        results = store.search_channel_messages(channel_name="Public", sender="Alice")
        assert len(results) == 1

    def test_all_filters_combined(self, store):
        store.store_channel_message(_msg(channel="Public", sender="Alice", text="Hello mesh"))
        store.store_channel_message(_msg(channel="Public", sender="Alice", text="Goodbye"))
        store.store_channel_message(_msg(channel="Public", sender="Bob", text="Hello mesh"))
        store.store_channel_message(_msg(channel="Private", sender="Alice", text="Hello mesh"))

        results = store.search_channel_messages(
            channel_name="Public", search_text="mesh", sender="Alice"
        )
        assert len(results) == 1
        assert results[0]["sender"] == "Alice"
        assert results[0]["text"] == "Hello mesh"


class TestSearchEscaping:
    def test_percent_in_search(self, store):
        """Literal % in search should not act as wildcard."""
        store.store_channel_message(_msg(text="100% done"))
        store.store_channel_message(_msg(text="200 items"))

        results = store.search_channel_messages(search_text="100%")
        assert len(results) == 1
        assert results[0]["text"] == "100% done"

    def test_underscore_in_search(self, store):
        """Literal _ in search should not act as single-char wildcard."""
        store.store_channel_message(_msg(text="hello_world"))
        store.store_channel_message(_msg(text="helloXworld"))

        results = store.search_channel_messages(search_text="hello_world")
        assert len(results) == 1
        assert results[0]["text"] == "hello_world"

    def test_backslash_in_search(self, store):
        store.store_channel_message(_msg(text="path\\to\\file"))
        store.store_channel_message(_msg(text="pathXtoXfile"))

        results = store.search_channel_messages(search_text="path\\to")
        assert len(results) == 1


class TestSearchPagination:
    def test_limit(self, store):
        for i in range(10):
            store.store_channel_message(_msg(text=f"msg{i}"))

        results = store.search_channel_messages(limit=3)
        assert len(results) == 3

    def test_offset(self, store):
        for i in range(10):
            store.store_channel_message(_msg(sender=f"User{i}", text="match"))

        results = store.search_channel_messages(search_text="match", limit=3, offset=5)
        assert len(results) == 3
        assert results[0]["sender"] == "User5"

    def test_no_filters_returns_all(self, store):
        for i in range(5):
            store.store_channel_message(_msg(text=f"msg{i}"))

        results = store.search_channel_messages()
        assert len(results) == 5


class TestSearchEdgeCases:
    def test_empty_search_text(self, store):
        """Empty string search_text should be treated as no filter."""
        store.store_channel_message(_msg(text="Hello"))
        results = store.search_channel_messages(search_text="")
        assert len(results) == 1

    def test_none_sender_rows(self, store):
        """Rows with NULL sender should not crash sender search."""
        m = GroupMessage(1700000000, None, "text", "Public", 0xAA, 1.0)
        store.store_channel_message(m)
        results = store.search_channel_messages(sender="Alice")
        assert results == []

    def test_ordering_ascending(self, store):
        now = time.time()
        m1 = GroupMessage(1000, "First", "msg", "Public", 0xAA, now - 10)
        m2 = GroupMessage(2000, "Second", "msg", "Public", 0xAA, now)
        store.store_channel_message(m1)
        store.store_channel_message(m2)

        results = store.search_channel_messages(search_text="msg")
        assert results[0]["sender"] == "First"
        assert results[1]["sender"] == "Second"
