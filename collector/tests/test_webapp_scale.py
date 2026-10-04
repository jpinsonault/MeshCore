"""Tests for the redesigned cracker web app: pagination, search, settings
persistence, config/wordlist changes, auto-crack, and the new HTTP endpoints.

All offline (no hardware, CPU engine forced). Volume is built with the seed
helper and with direct packet stores for precise counts.
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from collector.crypto import Channel
from collector.store import CollectorStore
from collector.webapp.cracker_app import CrackerApp, SETTINGS_KEY
from collector.webapp.seed import seed_demo_db
from collector.webapp.server import make_handler

from .packet_helpers import make_temp_store, store_grp_txt_packet


# ---- store-level: settings + pagination + counts ------------------------


def test_settings_roundtrip():
    store, path = make_temp_store()
    try:
        assert store.get_setting("missing", "def") == "def"
        store.set_setting("cracker", {"engine": "cpu", "max_length": 5})
        assert store.get_setting("cracker")["engine"] == "cpu"
        store.set_setting("cracker", {"engine": "gpu"})  # overwrite
        assert store.get_setting("cracker")["engine"] == "gpu"
        alls = store.get_all_settings()
        assert "cracker" in alls
    finally:
        store.close()


def test_count_and_page_messages():
    store, path = make_temp_store()
    try:
        ch = Channel.from_hashtag("#test")
        for i in range(25):
            store_grp_txt_packet(store, ch, f"alice: msg{i}", timestamp=1700000000 + i)
        app = CrackerApp(store, use_gpu=False)
        app.crack(ch.hash)  # dictionary → decodes all 25

        assert store.count_channel_messages(channel_name="#test") == 25
        assert store.count_channel_messages(channel_name="#test", search="msg7") == 1

        # Newest-first page.
        page = store.page_channel_messages(channel_name="#test", limit=10)
        assert len(page) == 10
        ids = [m["id"] for m in page]
        assert ids == sorted(ids, reverse=True)  # DESC

        # before_id walks back to older rows.
        older = store.page_channel_messages(
            channel_name="#test", limit=10, before_id=min(ids))
        assert len(older) == 10
        assert max(m["id"] for m in older) < min(ids)

        # after_id returns only newer rows.
        newer = store.page_channel_messages(
            channel_name="#test", limit=50, after_id=max(ids))
        assert newer == []
    finally:
        store.close()


def test_cracked_channel_summaries():
    store, path = make_temp_store()
    try:
        ch = Channel.from_hashtag("#test")
        for i in range(5):
            store_grp_txt_packet(store, ch, f"bob: m{i}", timestamp=1700000000 + i)
        app = CrackerApp(store, use_gpu=False)
        app.crack(ch.hash)
        rows = store.get_cracked_channel_summaries()
        row = next(r for r in rows if r["channel_name"] == "#test")
        assert row["msg_count"] == 5
        assert row["unique_senders"] == 1
    finally:
        store.close()


# ---- CrackerApp: config / settings / wordlist ---------------------------


def test_config_shape():
    store, path = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        cfg = app.config()
        for key in ("settings", "gpu", "engine", "wordlist", "db_path",
                    "search_size", "search_warn", "defaults", "live"):
            assert key in cfg
        assert cfg["engine"] == "cpu"
        assert cfg["gpu"]["forced_cpu"] is True
        assert cfg["live"] is False
        assert cfg["wordlist"]["total"] > 0
    finally:
        store.close()


def test_settings_persist_across_instances():
    store, path = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        app.update_settings({"charset": "abc", "max_length": 4, "engine": "cpu"})
        # A fresh app on the same store reloads the persisted settings.
        app2 = CrackerApp(store, use_gpu=False)
        s = app2.get_settings()
        assert s["charset"] == "abc"
        assert s["max_length"] == 4
        # Persisted under the documented key.
        assert store.get_setting(SETTINGS_KEY)["charset"] == "abc"
    finally:
        store.close()


def test_settings_validation():
    store, path = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        # Out-of-range clamps; bad engine ignored; charset de-duped/whitespace-stripped.
        app.update_settings({"max_length": 999, "engine": "nonsense",
                             "charset": "a a b b c"})
        s = app.get_settings()
        assert s["max_length"] == 12
        assert s["engine"] != "nonsense"
        assert s["charset"] == "abc"
    finally:
        store.close()


def test_estimate_search_size():
    store, path = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        # base 2, lengths 1..3 -> 2 + 4 + 8 = 14
        assert app.estimate_search_size("ab", 3) == 14
        assert app.estimate_search_size("", 5) == 0
    finally:
        store.close()


def test_catalog_toggle_changes_wordlist():
    store, path = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        with_catalog = app.wordlist_info()["total"]
        app.update_settings({"use_catalog": False})
        without = app.wordlist_info()["total"]
        assert without < with_catalog
        app.update_settings({"use_catalog": True})
        assert app.wordlist_info()["total"] == with_catalog
    finally:
        store.close()


def test_add_custom_wordlist_path(tmp_path):
    import secrets
    store, path = make_temp_store()
    try:
        name = "#" + secrets.token_hex(3)  # not in any built-in list
        ch = Channel.from_hashtag(name)
        store_grp_txt_packet(store, ch, "carol: secret")
        app = CrackerApp(store, use_gpu=False)

        # Before: not dictionary-crackable.
        assert app._cracker.match_dictionary(ch.hash, app._find_mac_and_data(ch.hash)) is None

        wl = tmp_path / "words.txt"
        wl.write_text(name + "\n", encoding="utf-8")
        res = app.add_custom_wordlist(path=str(wl))
        assert res["ok"] is True
        assert res["added"] >= 1

        # Now a dictionary match works, and the path persists for a new app.
        assert app._cracker.match_dictionary(ch.hash, app._find_mac_and_data(ch.hash)) is not None
        app2 = CrackerApp(store, use_gpu=False)
        assert str(wl) in app2.get_settings()["custom_wordlists"]
        assert app2._cracker.match_dictionary(ch.hash, app2._find_mac_and_data(ch.hash)) is not None
    finally:
        store.close()


def test_add_custom_wordlist_text(tmp_path, monkeypatch):
    import secrets
    import collector.webapp.cracker_app as mod
    monkeypatch.setattr(mod, "DEFAULT_CONFIG_DIR", tmp_path)
    store, path = make_temp_store()
    try:
        name = "#" + secrets.token_hex(3)
        ch = Channel.from_hashtag(name)
        store_grp_txt_packet(store, ch, "dave: hi")
        app = CrackerApp(store, use_gpu=False)
        res = app.add_custom_wordlist(text=name + "\n")
        assert res["ok"] is True
        assert (tmp_path / "cracker_wordlists").is_dir()
        assert app._cracker.match_dictionary(ch.hash, app._find_mac_and_data(ch.hash)) is not None
    finally:
        store.close()


def test_add_custom_wordlist_errors():
    store, path = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        assert app.add_custom_wordlist()["ok"] is False
        assert app.add_custom_wordlist(path="/no/such/file.txt")["ok"] is False
    finally:
        store.close()


# ---- CrackerApp: paginated results + auto-crack -------------------------


def test_channels_and_messages_at_scale():
    store, path = make_temp_store()
    try:
        summary = seed_demo_db(store, total=400)
        assert summary["messages"] == 400
        app = CrackerApp(store, use_gpu=False)

        chans = app.channels()
        assert len(chans) == summary["cracked"]
        gen = next(c for c in chans if c["channel_name"] == "#general")
        assert gen["msg_count"] == 40  # 400 / 10 channels

        page = app.channel_messages("#general", limit=15)
        assert page["total"] == 40
        assert page["count"] == 15
        assert page["has_more"] is True
        # Walk back.
        page2 = app.channel_messages("#general", limit=15, before_id=page["oldest_id"])
        assert page2["count"] == 15
        assert max(m["id"] for m in page2["messages"]) < page["oldest_id"]
    finally:
        store.close()


def test_channel_messages_search():
    store, path = make_temp_store()
    try:
        ch = Channel.from_hashtag("#test")
        store_grp_txt_packet(store, ch, "alice: needle here", timestamp=1700000001)
        store_grp_txt_packet(store, ch, "bob: nothing", timestamp=1700000002)
        store_grp_txt_packet(store, ch, "needleman: hi", timestamp=1700000003)
        app = CrackerApp(store, use_gpu=False)
        app.crack(ch.hash)
        res = app.channel_messages("#test", search="needle")
        assert res["total"] == 2  # matches text and sender
    finally:
        store.close()


def test_auto_crack_once_dictionary():
    store, path = make_temp_store()
    try:
        seed_demo_db(store, total=100, cracked=0)  # nothing decoded yet
        app = CrackerApp(store, use_gpu=False)
        pending_before = len(app.pending_channels())
        assert pending_before > 0
        cracked = app.auto_crack_once()
        # Demo channel names are all in the catalog, so dictionary cracks them.
        assert len(cracked) >= 1
        assert all(c["method"] == "dictionary" for c in cracked)
        assert len(app.pending_channels()) < pending_before
    finally:
        store.close()


def test_auto_crack_toggle_starts_thread():
    store, path = make_temp_store()
    try:
        app = CrackerApp(store, use_gpu=False)
        assert app.config()["auto_running"] is False
        app.update_settings({"auto_crack": True})
        assert app.config()["auto_running"] is True
        app.update_settings({"auto_crack": False})
        app.shutdown()
        assert app.config()["auto_running"] is False
    finally:
        store.close()


# ---- HTTP layer ---------------------------------------------------------


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode())


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


@pytest.fixture
def http_server():
    store, path = make_temp_store()
    seed_demo_db(store, total=120)
    app = CrackerApp(store, use_gpu=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, app
    httpd.shutdown()
    httpd.server_close()
    app.shutdown()
    store.close()


def test_http_static_assets(http_server):
    base, _ = http_server
    for name, needle in [("/app.js", "msgView"), ("/styles.css", "--accent")]:
        with urllib.request.urlopen(base + name, timeout=10) as r:
            body = r.read().decode()
        assert needle in body


def test_http_config_get_and_post(http_server):
    base, _ = http_server
    cfg = _get(base, "/api/config")
    assert cfg["engine"] == "cpu"
    updated = _post(base, "/api/config", {"max_length": 7, "charset": "xyz"})
    assert updated["settings"]["max_length"] == 7
    assert updated["settings"]["charset"] == "xyz"
    # Reflected on a fresh GET.
    assert _get(base, "/api/config")["settings"]["max_length"] == 7


def test_http_channels_and_messages(http_server):
    base, _ = http_server
    chans = _get(base, "/api/channels")
    assert any(c["channel_name"] == "#general" for c in chans)

    page = _get(base, "/api/channels/messages?channel=%23general&limit=10")
    assert page["total"] == 12  # 120 / 10 channels
    assert page["count"] == 10
    assert page["has_more"] is True

    older = _get(base, f"/api/channels/messages?channel=%23general&limit=10&before_id={page['oldest_id']}")
    assert older["count"] == 2  # remaining 2 of 12

    # Missing channel param -> error payload.
    assert "error" in _get(base, "/api/channels/messages")


def test_http_wordlist_add(http_server, tmp_path):
    import secrets
    base, _ = http_server
    wl = tmp_path / "w.txt"
    wl.write_text("#" + secrets.token_hex(3) + "\n", encoding="utf-8")
    res = _post(base, "/api/wordlist", {"path": str(wl)})
    assert res["ok"] is True
    assert res["added"] >= 1
