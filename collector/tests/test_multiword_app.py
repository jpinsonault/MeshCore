"""Integration tests for the multiword crack path (estimate + queue + worker)."""

import time

import pytest

from collector import brute_force_gpu as g
from collector.crypto import Channel
from collector.webapp.cracker_app import CrackerApp

from .packet_helpers import make_temp_store, store_grp_txt_packet

gpu_only = pytest.mark.skipif(
    not g.is_available(), reason="no CUDA GPU / CuPy available"
)


@pytest.fixture
def store_with_two_word_channel():
    """Store two distinct packets for '#north-sound' (needs >=2 blobs to crack)."""
    store, _ = make_temp_store()
    ch = Channel.from_hashtag("#north-sound")
    store_grp_txt_packet(store, ch, "alice: first message here")
    store_grp_txt_packet(store, ch, "bob: a different second one")
    yield store, ch
    store.close()


# --- estimate (no GPU needed) --------------------------------------------

def test_estimate_shape_and_cap():
    store, _ = make_temp_store()
    app = CrackerApp(store, use_gpu=False)
    e = app.multiword_estimate(n=2, tier=5000)
    assert e["n"] == 2 and e["tier"] == 5000
    assert e["words"] == 5000
    assert e["patterns"] == 2                 # concat + hyphen
    assert e["raw"] == 5000 ** 2 * 2
    assert 0 < e["under_cap"] <= e["raw"]
    assert e["eta_seconds"] > 0
    assert set(e["tiers"]) == {10000, 50000}
    store.close()


def test_estimate_defaults_from_settings():
    store, _ = make_temp_store()
    app = CrackerApp(store, use_gpu=False)
    app.update_settings({"multiword_words": 3, "multiword_tier": 2000})
    e = app.multiword_estimate()
    assert e["n"] == 3 and e["tier"] == 2000
    store.close()


def test_settings_persist_and_clamp():
    store, _ = make_temp_store()
    app = CrackerApp(store, use_gpu=False)
    app.update_settings({"multiword_words": 99, "multiword_concat": False})
    s = app.get_settings()
    assert s["multiword_words"] == 4          # clamped to 1..4
    assert s["multiword_concat"] is False
    store.close()


# --- full crack path (GPU) -----------------------------------------------

def _wait_idle(app, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = app.crack_status()
        if not st.get("running") and st.get("active") is None and not st.get("queued"):
            return
        time.sleep(0.1)
    raise AssertionError("crack did not finish in time")


@gpu_only
def test_multiword_sweep_cracks_two_word_channel(store_with_two_word_channel):
    store, ch = store_with_two_word_channel
    app = CrackerApp(store, use_gpu=True)
    try:
        # pending before
        assert ch.hash in {p["hash"] for p in app.pending_channels()}
        r = app.enqueue_multiword(n=2, tier=10000)
        assert r["queued"] is True
        _wait_idle(app)
        cracked = {c["channel_name"] for c in store.get_cracked_channels()}
        assert "#north-sound" in cracked
        row = next(c for c in store.get_cracked_channels()
                   if c["channel_name"] == "#north-sound")
        assert row["method"] == "multiword"
    finally:
        app.shutdown()


@gpu_only
def test_multiword_targeted_single_hash(store_with_two_word_channel):
    store, ch = store_with_two_word_channel
    app = CrackerApp(store, use_gpu=True)
    try:
        r = app.enqueue_multiword(target_hash=ch.hash, n=2, tier=10000)
        assert r["queued"] is True
        _wait_idle(app)
        assert "#north-sound" in {c["channel_name"] for c in store.get_cracked_channels()}
    finally:
        app.shutdown()


@gpu_only
def test_multiword_dedup(store_with_two_word_channel):
    store, ch = store_with_two_word_channel
    app = CrackerApp(store, use_gpu=True)
    try:
        r1 = app.enqueue_multiword(n=3, tier=10000)   # slower, stays queued/running
        r2 = app.enqueue_multiword(n=3, tier=10000)
        assert r1["queued"] is True
        assert r2["queued"] is False                  # deduped
        app.cancel_all()
    finally:
        app.shutdown()
