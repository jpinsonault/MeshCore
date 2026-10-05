"""Tests for the benign known-key decode / back-fill helpers (collector.decode)."""

from collector.crypto import Channel
from collector.decode import (
    backfill_channel,
    backfill_known_channels,
    load_known_channels,
)

from .packet_helpers import make_temp_store, store_grp_txt_packet


def test_load_known_channels_from_store():
    store, _ = make_temp_store()
    assert load_known_channels(store) == []
    store.store_cracked_channel("#north-sound", Channel.from_hashtag("#north-sound").hash, 0)
    store.store_cracked_channel("#north-sound", Channel.from_hashtag("#north-sound").hash, 0)  # dup
    chans = load_known_channels(store)
    assert [c.name for c in chans] == ["#north-sound"]
    store.close()


def test_backfill_channel_decodes_and_is_idempotent():
    store, _ = make_temp_store()
    ch = Channel.from_hashtag("#north-sound")
    store_grp_txt_packet(store, ch, "alice: hi one")
    store_grp_txt_packet(store, ch, "bob: hi two")
    assert backfill_channel(store, ch) == 2
    assert backfill_channel(store, ch) == 0           # already decoded -> idempotent
    senders = {m["sender"] for m in store.get_channel_messages(channel_name="#north-sound")}
    assert senders == {"alice", "bob"}
    store.close()


def test_backfill_known_channels_covers_all_known():
    store, _ = make_temp_store()
    a = Channel.from_hashtag("#north-sound")
    b = Channel.from_hashtag("#camp-fire")
    store_grp_txt_packet(store, a, "alice: a1")
    store_grp_txt_packet(store, b, "bob: b1")
    store_grp_txt_packet(store, b, "carol: b2")
    # nothing known yet
    assert backfill_known_channels(store) == 0
    store.store_cracked_channel("#north-sound", a.hash, 0)
    store.store_cracked_channel("#camp-fire", b.hash, 0)
    assert backfill_known_channels(store) == 3        # 1 + 2 across both channels
    assert backfill_known_channels(store) == 0        # idempotent second pass
    store.close()
