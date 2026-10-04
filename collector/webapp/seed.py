"""Seed a large demo database for exercising the cracker web app at scale.

Inserts thousands of GRP_TXT packets spread across several hashtag channels so
the paginated/searchable message views can be stress-tested without hardware.
A few channels are pre-cracked (their messages decoded and stored) while the
rest stay pending, so the pending list and the cracked views both have volume.

Reuses collector.tests.packet_helpers for packet construction.

    python -m collector.webapp.seed --db demo.db --total 5000
"""

import argparse
import time

from ..crypto import Channel, try_decode_group_message
from ..store import CollectorStore

# Demo channels. The first few are "cracked" (decoded + stored); the rest are
# left pending so the UI shows unknown hashes too.
DEMO_CHANNELS = [
    "#general", "#mesh", "#seattle", "#emergency", "#hiking",
    "#offgrid", "#weather", "#testbench", "#nodes", "#random",
]
DEMO_CRACKED = 6  # first N channels are decoded; remainder stay pending

SENDERS = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi"]
SNIPPETS = [
    "anyone copy?", "radio check", "good morning mesh", "testing 123",
    "node is up", "heading out on the trail", "storm rolling in",
    "battery at 80 percent", "new repeater online near downtown",
    "who is on tonight", "signal looking strong", "see you at the summit",
    "packet loss is high here", "relocated the antenna", "clear skies today",
]


def seed_demo_db(store: CollectorStore, total=5000, channels=None,
                 cracked=DEMO_CRACKED, ts_base=None):
    """Insert ``total`` GRP_TXT packets across ``channels``.

    Args:
        store: an open CollectorStore.
        total: total number of GRP_TXT packets to insert.
        channels: list of hashtag names (defaults to DEMO_CHANNELS).
        cracked: how many channels (from the front) to decode + persist.
        ts_base: base unix timestamp (defaults to now - total seconds).

    Returns a summary dict {messages, channels, cracked, pending}.
    """
    # Imported lazily so shipped code doesn't hard-depend on the test package.
    from ..tests.packet_helpers import make_grp_txt_raw, store_grp_txt_packet

    names = channels or DEMO_CHANNELS
    chans = [Channel.from_hashtag(n) for n in names]
    if ts_base is None:
        ts_base = int(time.time()) - total
    ts_base = int(ts_base)

    cracked_set = set()
    for i in range(total):
        ch = chans[i % len(chans)]
        sender = SENDERS[i % len(SENDERS)]
        snippet = SNIPPETS[i % len(SNIPPETS)]
        text = f"{sender}: {snippet} #{i}"
        ts = ts_base + i
        pkt_id = store_grp_txt_packet(store, ch, text, timestamp=ts)

        # Decode + persist for the "cracked" channels so they appear decoded.
        if chans.index(ch) < cracked:
            raw = make_grp_txt_raw(ch, text, timestamp=ts)
            msg = try_decode_group_message(raw, [ch], ts)
            if msg:
                store.store_channel_message(msg, raw_packet_id=pkt_id)
                if ch.name not in cracked_set:
                    cracked_set.add(ch.name)

    # Record the decoded channels in the cracked cache.
    for ch in chans[:cracked]:
        decoded = store.count_channel_messages(channel_name=ch.name)
        store.store_cracked_channel(ch.name, ch.hash, decoded)

    return {
        "messages": total,
        "channels": len(chans),
        "cracked": len(cracked_set),
        "pending": len(chans) - cracked,
    }


def main():
    parser = argparse.ArgumentParser(description="Seed a demo collector DB")
    parser.add_argument("--db", required=True, help="SQLite DB path to seed")
    parser.add_argument("--total", type=int, default=5000,
                        help="Total GRP_TXT packets to insert (default 5000)")
    parser.add_argument("--cracked", type=int, default=DEMO_CRACKED,
                        help="How many channels to pre-crack/decode")
    args = parser.parse_args()

    store = CollectorStore(args.db)
    store.open()
    try:
        t0 = time.monotonic()
        summary = seed_demo_db(store, total=args.total, cracked=args.cracked)
        dt = time.monotonic() - t0
    finally:
        store.close()
    print(f"Seeded {summary['messages']} messages across {summary['channels']} "
          f"channels ({summary['cracked']} cracked, {summary['pending']} pending) "
          f"in {dt:.1f}s -> {args.db}")


if __name__ == "__main__":
    main()
