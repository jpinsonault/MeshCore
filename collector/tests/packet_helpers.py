"""Shared packet/frame builders for collector tests.

Consolidates duplicated helpers from test_crypto, test_cracker, test_chat,
and test_core_channels into one importable module.
"""

import struct
import tempfile
import time

from collector.crypto import (
    Channel,
    GroupMessage,
    encrypt_then_mac,
    PAYLOAD_TYPE_GRP_TXT,
    ROUTE_TYPE_FLOOD,
)
from collector.protocol import FRAME_TYPE_RX_RAW
from collector.store import CollectorStore


def make_grp_txt_raw(channel, text, timestamp=1700000000):
    """Build raw packet bytes for a GRP_TXT message."""
    plaintext = struct.pack("<I", timestamp) + b"\x00" + text.encode("utf-8") + b"\x00"
    mac_and_data = encrypt_then_mac(channel.secret, plaintext)
    payload = bytes([channel.hash]) + mac_and_data
    header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
    return bytes([header, 0x00]) + payload


def store_grp_txt_packet(store, channel, text, timestamp=1700000000):
    """Store a GRP_TXT packet in raw_packets and return its ID."""
    raw = make_grp_txt_raw(channel, text, timestamp)
    frame = {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": 5.0,
            "rssi": -80,
            "route_type": ROUTE_TYPE_FLOOD,
            "payload_type": PAYLOAD_TYPE_GRP_TXT,
            "raw": raw,
            "raw_len": len(raw),
        },
    }
    return store.store_frame(frame)


def make_rx_frame(raw=None, payload_type=PAYLOAD_TYPE_GRP_TXT, snr=5.0, rssi=-80):
    """Build a parsed RX_RAW frame dict."""
    return {
        "type": FRAME_TYPE_RX_RAW,
        "received_at": time.time(),
        "parsed": {
            "snr": snr,
            "rssi": rssi,
            "route_type": ROUTE_TYPE_FLOOD,
            "payload_type": payload_type,
            "raw": raw or b"\x15\x00\xAB",
            "raw_len": len(raw) if raw else 3,
        },
    }


def make_group_msg(sender="alice", text="hello everyone", channel="#meshcore",
                   timestamp=1700000000, channel_hash=0xAA):
    """Build a GroupMessage for TUI events."""
    return GroupMessage(
        timestamp=timestamp,
        sender=sender,
        text=text,
        channel_name=channel,
        channel_hash=channel_hash,
        raw_timestamp=time.time(),
    )


def make_temp_store():
    """Create a fresh SQLite store with a temp file. Caller must close."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    s = CollectorStore(f.name)
    s.open()
    return s, f.name


def make_n_channels(n, prefix="ch"):
    """Bulk-create N hashtag channels. Returns list of Channel."""
    return [Channel.from_hashtag(f"#{prefix}{i}") for i in range(n)]


def store_n_packets(store, channel, n, text_prefix="Msg", ts_base=1700000000):
    """Bulk-store N packets for a channel. Returns list of row IDs."""
    ids = []
    for i in range(n):
        pkt_id = store_grp_txt_packet(
            store, channel, f"User: {text_prefix}{i}", timestamp=ts_base + i
        )
        ids.append(pkt_id)
    return ids
