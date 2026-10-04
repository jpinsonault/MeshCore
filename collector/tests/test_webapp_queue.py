"""Tests for the cracker queue, cancellation, and auto-crack routing."""

import time

import pytest

from collector import brute_force, brute_force_gpu
from collector.crypto import Channel, encrypt_then_mac
from collector.webapp.cracker_app import CrackerApp

from .packet_helpers import make_temp_store, store_grp_txt_packet


def _wait(cond, timeout=20.0, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


# --- engine-level cancellation (deterministic, no GPU needed) ---------------

def test_cpu_should_stop_aborts_immediately():
    ch = Channel.from_hashtag("#zzzzzz")  # not findable fast; but we stop at once
    mad = encrypt_then_mac(ch.secret, (0).to_bytes(4, "little") + b"\x00bob: hi\x00")
    t0 = time.monotonic()
    out = brute_force.brute_force_channel(
        ch.hash, mad, charset="abcdefghijklmnopqrstuvwxyz0123456789",
        max_length=6, should_stop=lambda: True,
    )
    assert out is None
    assert time.monotonic() - t0 < 5.0  # aborted, not a full 2.2B sweep


@pytest.mark.skipif(not brute_force_gpu.is_available(), reason="no CUDA GPU")
def test_gpu_should_stop_aborts_immediately():
    ch = Channel.from_hashtag("#zzzzzz")
    mad = encrypt_then_mac(ch.secret, (0).to_bytes(4, "little") + b"\x00bob: hi\x00")
    out = brute_force_gpu.brute_force_channel_gpu(
        ch.hash, mad, charset="abcdefghijklmnopqrstuvwxyz0123456789",
        max_length=7, should_stop=lambda: True,
    )
    assert out is None


# --- queue ------------------------------------------------------------------

def test_queue_processes_multiple_jobs():
    store, _ = make_temp_store()
    try:
        # Two catalog channels -> dictionary cracks, no GPU needed.
        for name in ("#wardriving", "#hamradio"):
            store_grp_txt_packet(store, Channel.from_hashtag(name), "bob: hi there")
        app = CrackerApp(store, use_gpu=False)
        h1 = Channel.from_hashtag("#wardriving").hash
        h2 = Channel.from_hashtag("#hamradio").hash
        assert app.start_crack(h1)["started"] is True
        assert app.start_crack(h2)["started"] is True
        assert _wait(lambda: {c["channel_name"] for c in app.channels()}
                     >= {"#wardriving", "#hamradio"})
        app.shutdown()
    finally:
        store.close()


def test_enqueue_dedupes_pending_hash():
    store, _ = make_temp_store()
    try:
        ch = Channel.from_hashtag("#wardriving")
        store_grp_txt_packet(store, ch, "bob: hi")
        app = CrackerApp(store, use_gpu=False)
        r1 = app.enqueue(ch.hash)
        r2 = app.enqueue(ch.hash)  # same hash again -> deduped (queued or running)
        assert r1["queued"] is True
        assert r2["queued"] is False
        app.shutdown()
    finally:
        store.close()


def test_cancel_unknown_job():
    store, _ = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        assert app.cancel(99999)["ok"] is False
        assert app.cancel_all()["ok"] is True
        app.shutdown()
    finally:
        store.close()


def test_crack_status_exposes_queue():
    store, _ = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        st = app.crack_status()
        for key in ("running", "queued", "recent", "active", "queue_len"):
            assert key in st
        app.shutdown()
    finally:
        store.close()


# --- auto-crack now brute-forces pending misses on GPU ----------------------

@pytest.mark.skipif(not brute_force_gpu.is_available(), reason="no CUDA GPU")
def test_auto_crack_bruteforces_pending_on_gpu():
    store, _ = make_temp_store()
    try:
        # A short name NOT in any wordlist -> dictionary misses, GPU brute-force
        # (3 chars) finds it quickly through the auto loop + queue.
        ch = Channel.from_hashtag("#zq7")
        store_grp_txt_packet(store, ch, "bob: hello")
        app = CrackerApp(store, use_gpu=True)
        assert app.engine() == "gpu"
        app.update_settings({"auto_crack": True})  # no manual press
        assert _wait(lambda: "#zq7" in {c["channel_name"] for c in app.channels()},
                     timeout=30)
        app.update_settings({"auto_crack": False})
        app.shutdown()
    finally:
        store.close()


# --- exhausted-attempt cache -----------------------------------------------

def test_exhausted_cache_records_and_blocks_auto():
    store, _ = make_temp_store()
    try:
        # A short non-dictionary name that a tiny charset can't reach -> the
        # brute-force exhausts and should be recorded, not retried by auto.
        ch = Channel.from_hashtag("#zq7")
        store_grp_txt_packet(store, ch, "bob: hi")
        app = CrackerApp(store, use_gpu=False)
        res = app.crack(ch.hash, charset="ab", max_length=2)  # can't contain z/q/7
        assert res["cracked"] is False
        assert res.get("exhausted") is True
        assert app.is_exhausted(ch.hash, "ab", 2) is True
        # Different params aren't considered exhausted.
        assert app.is_exhausted(ch.hash, "ab", 3) is False
        # retry clears it.
        app.retry_hash(ch.hash)
        assert app.is_exhausted(ch.hash, "ab", 2) is False
        app.shutdown()
    finally:
        store.close()


def test_pasted_packet_does_not_record_exhausted():
    store, _ = make_temp_store()
    try:
        from collector.crypto import encrypt_then_mac
        ch = Channel.from_hashtag("#zq7")
        mad = encrypt_then_mac(ch.secret, (0).to_bytes(4, "little") + b"\x00bob: hi\x00")
        app = CrackerApp(store, use_gpu=False)
        res = app.crack(ch.hash, mac_and_data=mad, charset="ab", max_length=2,
                        record_exhausted=False)
        assert res["cracked"] is False
        assert "exhausted" not in res
        assert app.is_exhausted(ch.hash, "ab", 2) is False
        app.shutdown()
    finally:
        store.close()


# --- rules engine (mangled dictionary) -------------------------------------

def test_crack_via_rules():
    store, _ = make_temp_store()
    try:
        # "weather2024" is not a literal catalog entry, but rules mangle
        # "weather" -> "weather2024"; no brute-force needed.
        ch = Channel.from_hashtag("#weather2024")
        store_grp_txt_packet(store, ch, "alice: hello")
        app = CrackerApp(store, use_gpu=False)
        res = app.crack(ch.hash, charset="ab", max_length=2)  # brute can't reach it
        assert res["cracked"] is True
        assert res["channel_name"] == "#weather2024"
        assert res["method"] == "rules"
        app.shutdown()
    finally:
        store.close()


def test_rules_can_be_disabled():
    store, _ = make_temp_store()
    try:
        ch = Channel.from_hashtag("#weather2024")
        store_grp_txt_packet(store, ch, "alice: hello")
        app = CrackerApp(store, use_gpu=False)
        app.update_settings({"use_rules": False})
        res = app.crack(ch.hash, charset="ab", max_length=2)
        assert res["cracked"] is False  # neither dict nor rules nor tiny brute
        app.shutdown()
    finally:
        store.close()
