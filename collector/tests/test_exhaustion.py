"""Tests for depth-aware exhaustion: charset max-length subsumption + incremental
runs, and the multiword (word-count/tier) exhaustion cache."""

import struct

import pytest

from collector import brute_force
from collector import brute_force_gpu as g
from collector.crypto import Channel, encrypt_then_mac
from collector.webapp.cracker_app import CrackerApp

from .packet_helpers import make_temp_store, store_grp_txt_packet

gpu_only = pytest.mark.skipif(
    not g.is_available(), reason="no CUDA GPU / CuPy available"
)


def _mad(name, text="alice: hi there friend"):
    ch = Channel.from_hashtag(name)
    pt = struct.pack("<I", 1700000000) + b"\x00" + text.encode() + b"\x00"
    return ch, encrypt_then_mac(ch.secret, pt)


# --- store: charset max-length subsumption ---------------------------------

def test_max_exhausted_length_picks_deepest():
    store, _ = make_temp_store()
    cs = "abc"
    store.record_crack_attempt(42, cs, 5, "exhausted", 3)
    store.record_crack_attempt(42, cs, 7, "exhausted", 4)
    store.record_crack_attempt(42, cs, 6, "exhausted", 2)
    assert store.max_exhausted_length(42, cs) == (7, 4)
    assert store.max_exhausted_length(99, cs) is None
    store.close()


def test_max_exhausted_length_charset_subset():
    store, _ = make_temp_store()
    # A sweep over "abc-" covers a request over "abc" (subset) but not "abcd".
    store.record_crack_attempt(42, "abc-", 6, "exhausted", 0)
    assert store.max_exhausted_length(42, "abc") == (6, 0)      # subset -> covered
    assert store.max_exhausted_length(42, "abc-") == (6, 0)     # equal -> covered
    assert store.max_exhausted_length(42, "abcd") is None       # superset req -> not covered
    # Enlarging the charset (adding '-') is NOT covered by a no-hyphen sweep.
    store.record_crack_attempt(43, "abc", 6, "exhausted", 0)
    assert store.max_exhausted_length(43, "abc-") is None
    store.close()


# --- store: multiword covering (tier + pattern subsumption) ----------------

def test_multiword_covering():
    store, _ = make_temp_store()
    # swept 2 words, tier 10000, both separators
    store.record_multiword_attempt(10, 2, 10000, True, True, "exhausted", 5)
    # same/smaller tier, subset patterns -> covered
    assert store.multiword_attempt_covering(10, 2, 10000, True, True) == (10000, 5)
    assert store.multiword_attempt_covering(10, 2, 5000, True, True) is not None
    assert store.multiword_attempt_covering(10, 2, 10000, True, False) is not None
    assert store.multiword_attempt_covering(10, 2, 10000, False, True) is not None
    # bigger tier -> NOT covered (superset not yet searched)
    assert store.multiword_attempt_covering(10, 2, 50000, True, True) is None
    # different word count -> NOT covered
    assert store.multiword_attempt_covering(10, 3, 10000, True, True) is None
    store.close()


def test_clear_crack_attempt_clears_both():
    store, _ = make_temp_store()
    store.record_crack_attempt(7, "abc", 6, "exhausted", 1)
    store.record_multiword_attempt(7, 2, 10000, True, True, "exhausted", 1)
    store.clear_crack_attempt(7)
    assert store.max_exhausted_length(7, "abc") is None
    assert store.multiword_attempt_covering(7, 2, 10000, True, True) is None
    store.close()


# --- app: is_exhausted / _swept_length subsumption -------------------------

def test_is_exhausted_subsumes_shallower():
    store, _ = make_temp_store()
    app = CrackerApp(store, use_gpu=False)
    cs = app._settings["charset"]
    store.record_crack_attempt(55, cs, 7, "exhausted", 0)
    # swept to 7 -> 6 and 7 are covered, 8 is not
    assert app.is_exhausted(55, cs, 6, current_packets=0) is True
    assert app.is_exhausted(55, cs, 7, current_packets=0) is True
    assert app.is_exhausted(55, cs, 8, current_packets=0) is False
    # a charset with a character the sweep never covered is NOT subsumed
    assert app.is_exhausted(55, cs + "!", 6, current_packets=0) is False
    store.close()


def test_swept_length_and_packet_growth_reset():
    store, _ = make_temp_store()
    app = CrackerApp(store, use_gpu=False)
    cs = app._settings["charset"]
    store.record_crack_attempt(55, cs, 6, "exhausted", 10)
    assert app._swept_length(55, cs, current_packets=12) == 6     # stable -> floor 6
    # packets grew past the retry delta -> re-grind from scratch (floor 0)
    assert app._swept_length(55, cs, current_packets=1000) == 0
    assert app.is_exhausted(55, cs, 6, current_packets=1000) is False
    store.close()


# --- incremental CPU brute-force (min_length skips shallower lengths) -------

def test_cpu_min_length_skips_shorter():
    ch, mad = _mad("#ab")  # name "ab", length 2
    # min_length=3 skips lengths 1-2, so the len-2 name is NOT found.
    assert brute_force.brute_force_channel(
        ch.hash, mad, charset="ab", max_length=3, min_length=3) is None
    # min_length=1 (default) finds it.
    assert brute_force.brute_force_channel(
        ch.hash, mad, charset="ab", max_length=2) == "#ab"


# --- incremental GPU batch (min_length_by_hash) ----------------------------

@gpu_only
def test_gpu_batch_min_length_skips_shorter():
    ch, mad = _mad("#ab")
    _, mad2 = _mad("#ab", text="bob: another line here")
    targets = {ch.hash: {"mac_and_data": mad, "extras": [mad2]}}
    # floor above the real length -> skipped, not found
    skipped = g.brute_force_batch_gpu(
        targets, charset="ab", max_length=3, min_length_by_hash={ch.hash: 3})
    assert ch.hash not in skipped
    # no floor -> found
    found = g.brute_force_batch_gpu(targets, charset="ab", max_length=2)
    assert found.get(ch.hash) == "#ab"


# --- app: multiword skip of covered targets --------------------------------

@gpu_only
def test_multiword_skips_exhausted():
    store, _ = make_temp_store()
    ch = Channel.from_hashtag("#north-sound")
    store_grp_txt_packet(store, ch, "alice: one two three")
    store_grp_txt_packet(store, ch, "bob: four five six seven")
    # Pre-mark this hash as already 2-word-swept at tier 10000 (both separators).
    store.record_multiword_attempt(ch.hash, 2, 10000, True, True, "exhausted", 0)
    app = CrackerApp(store, use_gpu=True)
    try:
        res = app._run_multiword({
            "cancel": __import__("threading").Event(),
            "target_hash": None, "mw_n": 2, "mw_tier": 10000,
            "mw_concat": True, "mw_hyphen": True,
        })
        assert res["swept"] == 0            # nothing actually ground
        assert res["skipped"] >= 1          # the pre-marked hash was skipped
        # and it was NOT cracked (skip beats the otherwise-crackable name)
        assert "#north-sound" not in {c["channel_name"]
                                      for c in store.get_cracked_channels()}
    finally:
        app.shutdown()
