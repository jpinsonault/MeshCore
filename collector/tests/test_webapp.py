"""Tests for the cracker web app (collector.webapp).

Offline mode only — a store seeded with a GRP_TXT packet for a known hashtag
channel, cracked with the CPU brute-forcer over a tiny charset so the tests run
fast and need no GPU or hardware.
"""

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from collector.crypto import Channel, extract_group_payload
from collector.webapp.cracker_app import CrackerApp
from collector.webapp.server import make_handler

from .packet_helpers import make_grp_txt_raw, make_temp_store, store_grp_txt_packet

# "#test" -> name "test"; charset covering t/e/s keeps the search tiny.
TARGET_NAME = "#test"
TINY_CHARSET = "est"
TINY_MAXLEN = 4


@pytest.fixture
def seeded_store():
    store, path = make_temp_store()
    channel = Channel.from_hashtag(TARGET_NAME)
    store_grp_txt_packet(store, channel, "alice: hello mesh")
    store_grp_txt_packet(store, channel, "bob: second msg")
    yield store, channel
    store.close()


def test_gpu_status_shape(seeded_store):
    store, _ = seeded_store
    app = CrackerApp(store, use_gpu=False)
    gpu = app.gpu_status()
    assert gpu["available"] is False
    assert gpu["engine"] == "cpu"
    assert app.engine() == "cpu"


def test_pending_lists_unknown_hash(seeded_store):
    store, channel = seeded_store
    app = CrackerApp(store, use_gpu=False)
    pending = app.pending_channels()
    hashes = {p["hash"] for p in pending}
    assert channel.hash in hashes
    entry = next(p for p in pending if p["hash"] == channel.hash)
    assert entry["packet_count"] == 2
    assert entry["undecoded_count"] == 2


def test_crack_offline_decodes_messages(seeded_store):
    store, channel = seeded_store
    app = CrackerApp(store, use_gpu=False)
    result = app.crack(channel.hash, charset=TINY_CHARSET, max_length=TINY_MAXLEN)
    assert result["cracked"] is True
    assert result["channel_name"] == TARGET_NAME
    assert result["decoded_count"] == 2

    # Persisted + retroactively decoded.
    cracked = store.get_cracked_channels()
    assert any(c["channel_name"] == TARGET_NAME for c in cracked)
    msgs = store.get_channel_messages(channel_name=TARGET_NAME)
    senders = {m["sender"] for m in msgs}
    assert senders == {"alice", "bob"}

    # No longer pending.
    assert channel.hash not in {p["hash"] for p in app.pending_channels()}


def test_results_after_crack(seeded_store):
    store, channel = seeded_store
    app = CrackerApp(store, use_gpu=False)
    app.crack(channel.hash, charset=TINY_CHARSET, max_length=TINY_MAXLEN)
    results = app.results()
    assert len(results) == 1
    assert results[0]["channel_name"] == TARGET_NAME
    assert len(results[0]["messages"]) == 2


def test_crack_missing_packet(seeded_store):
    store, _ = seeded_store
    app = CrackerApp(store, use_gpu=False)
    # A hash with no stored packet can't be cracked.
    result = app.crack(0xFF, charset=TINY_CHARSET, max_length=1)
    assert result["cracked"] is False
    assert "error" in result


def test_crack_from_pasted_packet(seeded_store):
    store, channel = seeded_store
    app = CrackerApp(store, use_gpu=False)
    raw = make_grp_txt_raw(channel, "carol: pasted")
    extracted = extract_group_payload(raw)
    result = app.crack(
        extracted["channel_hash"], mac_and_data=extracted["mac_and_data"],
        charset=TINY_CHARSET, max_length=TINY_MAXLEN,
    )
    assert result["cracked"] is True
    assert result["channel_name"] == TARGET_NAME


def test_crack_dictionary_first(seeded_store):
    # "#test" is in the bundled wordlist, so the catalog cracks it instantly —
    # even with a charset that brute-force could never reach the name through.
    store, channel = seeded_store
    app = CrackerApp(store, use_gpu=False)
    result = app.crack(channel.hash, charset="z", max_length=1)
    assert result["cracked"] is True
    assert result["channel_name"] == TARGET_NAME
    assert result["method"] == "dictionary"


def test_crack_bruteforce_fallback():
    # A random name not in any wordlist falls through to brute-force.
    import secrets
    store, _ = make_temp_store()
    name = "#" + secrets.token_hex(2)  # 4 hex chars
    ch = Channel.from_hashtag(name)
    store_grp_txt_packet(store, ch, "carol: hi")
    try:
        app = CrackerApp(store, use_gpu=False)
        result = app.crack(ch.hash, charset="0123456789abcdef", max_length=4)
        assert result["cracked"] is True
        assert result["channel_name"] == name
        assert result["method"] == "bruteforce"
    finally:
        store.close()


# --- HTTP layer ----------------------------------------------------------


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode())


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


@pytest.fixture
def http_server(seeded_store):
    store, channel = seeded_store
    app = CrackerApp(store, use_gpu=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, app, channel
    httpd.shutdown()
    httpd.server_close()


def test_http_index_served(http_server):
    base, _, _ = http_server
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode()
    assert "MeshCore Channel Cracker" in html


def test_http_gpu_and_pending(http_server):
    base, _, channel = http_server
    assert _get(base, "/api/gpu")["engine"] == "cpu"
    pending = _get(base, "/api/pending")
    assert channel.hash in {p["hash"] for p in pending}


def test_http_crack_endpoint(http_server):
    base, _, channel = http_server
    started = _post(base, "/api/crack", {
        "hash": channel.hash, "charset": TINY_CHARSET, "max_length": TINY_MAXLEN,
    })
    assert started["started"] is True

    # Poll status until the background crack finishes.
    deadline = time.monotonic() + 30
    status = {}
    while time.monotonic() < deadline:
        status = _get(base, "/api/crack/status")
        if not status["running"] and status.get("result"):
            break
        time.sleep(0.2)
    assert status["result"]["cracked"] is True
    assert status["result"]["channel_name"] == TARGET_NAME

    results = _get(base, "/api/results")
    assert any(c["channel_name"] == TARGET_NAME for c in results)


def test_http_crack_packet_endpoint(http_server):
    base, _, channel = http_server
    raw = make_grp_txt_raw(channel, "dave: via paste")
    started = _post(base, "/api/crack/packet", {
        "hex": raw.hex(), "charset": TINY_CHARSET, "max_length": TINY_MAXLEN,
    })
    assert started["started"] is True

    deadline = time.monotonic() + 30
    status = {}
    while time.monotonic() < deadline:
        status = _get(base, "/api/crack/status")
        if not status["running"] and status.get("result"):
            break
        time.sleep(0.2)
    assert status["result"]["cracked"] is True
    assert status["result"]["channel_name"] == TARGET_NAME


def test_http_crack_packet_bad_hex(http_server):
    base, _, _ = http_server
    res = _post(base, "/api/crack/packet", {"hex": "zzzz"})
    assert res["started"] is False
    assert "error" in res
