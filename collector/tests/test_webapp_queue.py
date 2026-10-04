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
        store_grp_txt_packet(store, ch, "carol: sibling")  # >=2 packets to brute-force
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
        store_grp_txt_packet(store, ch, "carol: sibling")  # >=2 packets to brute-force
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


# --- batched sweep ---------------------------------------------------------

@pytest.mark.skipif(not brute_force_gpu.is_available(), reason="no CUDA GPU")
def test_batch_sweep_cracks_many_and_marks_exhausted():
    store, _ = make_temp_store()
    try:
        # short non-dictionary names (brute-forceable) + one unsolvable long name
        solvable = ["#zq7", "#xj4", "#kk9"]
        for nm in solvable:
            store_grp_txt_packet(store, Channel.from_hashtag(nm), "bob: one")
            store_grp_txt_packet(store, Channel.from_hashtag(nm), "carol: two")
        unsolv = Channel.from_hashtag("#unsolvablelongname")
        store_grp_txt_packet(store, unsolv, "dave: hi")
        store_grp_txt_packet(store, unsolv, "erin: sibling")  # >=2 so it's swept (then exhausted)

        app = CrackerApp(store, use_gpu=True)
        assert app.engine() == "gpu"
        app.update_settings({"use_rules": False})  # force brute-force path
        r = app.enqueue_sweep()
        assert r["queued"] is True
        assert _wait(lambda: {c["channel_name"] for c in app.channels()} >= set(solvable),
                     timeout=60)
        # the unsolvable hash is now marked exhausted (won't be re-swept)
        cs = app.get_settings()["charset"]
        ml = app.get_settings()["max_length"]
        assert _wait(lambda: app.is_exhausted(unsolv.hash, cs, ml), timeout=10)
        app.shutdown()
    finally:
        store.close()


@pytest.mark.skipif(not brute_force_gpu.is_available(), reason="no CUDA GPU")
def test_batch_host_fn_no_false_positive():
    from collector.crypto import encrypt_then_mac
    def blob(ch):
        return encrypt_then_mac(ch.secret, (0).to_bytes(4, "little") + b"\x00bob: hi there\x00")
    good = Channel.from_hashtag("#zq7")
    bad = Channel.from_hashtag("#wardriving")  # 10 chars, outside 6-char sweep
    targets = {good.hash: {"mac_and_data": blob(good)},
               bad.hash: {"mac_and_data": blob(bad)}}
    solved = brute_force_gpu.brute_force_batch_gpu(
        targets, charset="abcdefghijklmnopqrstuvwxyz0123456789", max_length=6)
    assert solved.get(good.hash) == "#zq7"
    assert bad.hash not in solved


# --- channel model: method + state + packets -------------------------------

def test_channels_carry_method_state_packets():
    store, _ = make_temp_store()
    try:
        ch = Channel.from_hashtag("#wardriving")  # in the catalog -> dictionary
        for t in ("bob: a", "carol: b", "bob: c"):
            store_grp_txt_packet(store, ch, t)
        app = CrackerApp(store, use_gpu=False)
        res = app.crack(ch.hash)
        assert res["cracked"] and res["method"] == "dictionary"
        chans = {c["channel_name"]: c for c in app.channels()}
        assert "#wardriving" in chans
        c = chans["#wardriving"]
        assert c["method"] == "dictionary"
        assert c["state"] == "named"
        assert c["packets"] == 3
        assert c["messages"] == 3
        app.shutdown()
    finally:
        store.close()


# --- hash collision: cracking must progress to the second channel ----------

@pytest.mark.skipif(not brute_force_gpu.is_available(), reason="no CUDA GPU")
def test_collision_hash_cracks_both_channels():
    import itertools
    store, _ = make_temp_store()
    try:
        cs = "abcdefghijklmnopqrstuvwxyz0123456789"
        dictname = "#weather"                    # in catalog -> fast-match
        hw = Channel.from_hashtag(dictname).hash
        # find a short a-z0-9 name on the SAME hash byte (a real collision)
        collide = None
        for a, b, c in itertools.product(cs, cs, cs):
            nm = "#" + a + b + c
            if Channel.from_hashtag(nm).hash == hw and nm != dictname:
                collide = nm
                break
        assert collide, "no colliding name found"

        chA, chB = Channel.from_hashtag(dictname), Channel.from_hashtag(collide)
        for i in range(3):
            store_grp_txt_packet(store, chA, f"alice: a{i}")
            store_grp_txt_packet(store, chB, f"bob: b{i}")

        app = CrackerApp(store, use_gpu=True)
        assert any(p["hash"] == hw for p in app.pending_channels())

        # Crack #1: fast-match recovers #weather; the colliding channel stays.
        app.crack(hw, charset=cs, max_length=3)
        names = {c["channel_name"] for c in app.channels()}
        assert dictname in names and collide not in names
        assert any(p["hash"] == hw for p in app.pending_channels()), "collision still pending"

        # Crack #2: now targets the undecoded collider -> brute-force finds it.
        app.crack(hw, charset=cs, max_length=3)
        names = {c["channel_name"] for c in app.channels()}
        assert collide in names, "second channel on the hash not recovered"
        assert not any(p["hash"] == hw for p in app.pending_channels()), "hash fully decoded now"
        app.shutdown()
    finally:
        store.close()
