"""Decode-with-known-key helpers for the collector.

Benign counterpart to the (now separate) channel-name discovery tooling: these only apply
*already-known* channel keys to stored packets — no key/name search. Pulled out of the former
`cracker.py` (`retroactive_decrypt` / the benign half of `load_cache`) so the collector can
back-fill history and surface previously-discovered channels without depending on that tooling.
"""

from .crypto import Channel, try_decode_group_message


def load_known_channels(store):
    """Channel objects for channel names already recorded in the store (by this run or the
    external discovery tooling). Pure DB read + known-name key derivation; no search."""
    channels = []
    try:
        rows = store.get_cracked_channels()
    except Exception:
        return channels
    seen = set()
    for row in rows:
        name = row["channel_name"]
        if name in seen:
            continue
        try:
            ch = Channel.from_hashtag(name)
        except Exception:
            continue
        seen.add(name)
        channels.append(ch)
    return channels


def backfill_channel(store, channel):
    """Decode stored GRP_TXT packets with an already-known channel key, store the results, and
    return the count of newly decoded messages. (Formerly ChannelCracker.retroactive_decrypt.)"""
    if not store:
        return 0
    decoded = 0
    for pkt in store.get_grp_txt_packets():
        raw_packet_id = pkt["id"]
        if store.has_channel_message_for_packet(raw_packet_id):
            continue
        raw_hex = pkt.get("raw_hex", "")
        if not raw_hex:
            continue
        try:
            raw_bytes = bytes.fromhex(raw_hex)
        except ValueError:
            continue
        msg = try_decode_group_message(raw_bytes, [channel], pkt["timestamp"])
        if msg:
            store.store_channel_message(msg, raw_packet_id=raw_packet_id)
            decoded += 1
    return decoded


def backfill_known_channels(store):
    """Re-decode stored packets under EVERY already-known channel key and store the
    results (idempotent via backfill_channel). Returns the total count of newly
    decoded messages. Run this once after a decode change — e.g. the packed
    path_len parse fix, which had mis-sliced packets with >1-byte-per-hop paths so
    they never decoded even on channels we already hold keys for."""
    total = 0
    for channel in load_known_channels(store):
        try:
            total += backfill_channel(store, channel)
        except Exception:
            continue
    return total
