"""Tests for the network link to the WiFi collector build (socket:// ports, auth)."""

import socket
import struct
import threading
import time

import pytest

from collector.core import CollectorCore, is_network_port
from collector.protocol import (
    FRAME_START, FRAME_TYPE_HANDSHAKE, FRAME_TYPE_HEARTBEAT, FRAME_TYPE_BOOT_INFO, crc16_ccitt,
)
from collector.tests.packet_helpers import temp_file

PASSWORD = "s3cret"


def _v1_frame(frame_type, payload):
    return bytes([FRAME_START]) + struct.pack("<H", 1 + len(payload)) + bytes([frame_type]) + payload


def _v2_frame(frame_type, seq, payload):
    crc_data = bytes([frame_type]) + struct.pack("<I", seq) + payload
    return (bytes([FRAME_START]) + struct.pack("<H", len(crc_data) + 2) + crc_data
            + struct.pack("<H", crc16_ccitt(crc_data)))


def _hb_payload():
    return struct.pack("<IHIIIIBI", 1700000000, 3700, 0, 0, 0, 0, 10, 100)


def _boot_payload(reset_reason=5, flags=0x01, boot_count=2, prev_uptime=1800,
                  prev_heap_min=38000, prev_rssi=-72, prev_err=0):
    return struct.pack("<BBHIIhH", reset_reason, flags, boot_count, prev_uptime,
                       prev_heap_min, prev_rssi, prev_err)


class FakeWifiDevice:
    """Speaks the firmware's TCP side: banner, echo, auth gate, collector start -> handshake."""

    def __init__(self, password=PASSWORD):
        self.password = password
        self.lines = []
        self.authed = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _reply(self, conn, text):
        conn.sendall(f"  -> {text}\r\n".encode())

    def _serve(self):
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            conn.sendall(b"MeshCore collector - send: auth <admin password>\r\n")
            buf = b""
            while True:
                try:
                    data = conn.recv(1024)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while True:
                    if buf[:1] == bytes([FRAME_START]):   # host ACK/RESUME frames
                        if len(buf) < 3:
                            break
                        flen = buf[1] | (buf[2] << 8)
                        if len(buf) < 3 + flen:
                            break
                        buf = buf[3 + flen:]
                        continue
                    if b"\r" not in buf:
                        break
                    line, buf = buf.split(b"\r", 1)
                    line = line.decode(errors="replace")
                    self.lines.append(line)
                    conn.sendall(line.encode() + b"\r\n")   # firmware echoes input
                    if not self.authed:
                        if line == f"auth {self.password}":
                            self.authed = True
                            self._reply(conn, "OK - authenticated")
                        else:
                            self._reply(conn, "Err - auth required: auth <admin password>")
                    elif line == "collector start":
                        payload = b"COLLECTOR" + bytes([2]) + struct.pack("<II", 0, 0)
                        conn.sendall(_v1_frame(FRAME_TYPE_HANDSHAKE, payload))
                        self._reply(conn, "OK")
                        conn.sendall(_v2_frame(FRAME_TYPE_BOOT_INFO, 1, _boot_payload()))
                        conn.sendall(_v2_frame(FRAME_TYPE_HEARTBEAT, 2, _hb_payload()))

    def close(self):
        self._srv.close()


def _run_core(port, password, wait_for, reconnect=True):
    """Start a core against the fake device; returns (core, events dict) after wait_for fires."""
    events = {"connected": threading.Event(), "disconnected": threading.Event(),
              "heartbeat": threading.Event(), "reason": None, "text": []}

    def on_frame(frame):
        if frame["type"] == FRAME_TYPE_HEARTBEAT:
            events["heartbeat"].set()

    def on_disconnected(reason):
        if events["reason"] is None:   # keep the first reason (later ones are reconnect noise)
            events["reason"] = reason
        events["disconnected"].set()

    tmp = temp_file(".db")
    db = tmp.__enter__()
    core = CollectorCore(port=port, db_path=db.name, password=password, reconnect=reconnect)
    core.on_frame = on_frame
    core.on_text = lambda line: events["text"].append(line)
    core.on_connected = events["connected"].set
    core.on_disconnected = on_disconnected
    core.start()
    events[wait_for].wait(timeout=10)
    return core, events, tmp


class TestIsNetworkPort:
    def test_socket_url(self):
        assert is_network_port("socket://heltec-repeater.local:5005")

    def test_serial_ports(self):
        assert not is_network_port("COM3")
        assert not is_network_port("/dev/ttyUSB0")
        assert not is_network_port(None)


class TestNetworkLink:
    def test_auth_then_collect(self):
        dev = FakeWifiDevice()
        core, ev, tmp = _run_core(f"socket://127.0.0.1:{dev.port}", PASSWORD, "heartbeat")
        try:
            assert ev["connected"].is_set(), ev["reason"]
            assert ev["heartbeat"].is_set()
            assert dev.lines[0] == f"auth {PASSWORD}"
            assert "collector start" in dev.lines
            # the echoed password must not leak into the debug log text
            assert not any(PASSWORD in line for line in ev["text"])
        finally:
            core.stop()
            tmp.__exit__(None, None, None)
            dev.close()

    def test_wrong_password(self):
        dev = FakeWifiDevice()
        core, ev, tmp = _run_core(f"socket://127.0.0.1:{dev.port}", "nope", "disconnected")
        try:
            assert "Authentication failed" in ev["reason"]
            assert not ev["connected"].is_set()
            assert "collector start" not in dev.lines
        finally:
            core.stop()
            tmp.__exit__(None, None, None)
            dev.close()

    def test_missing_password(self, monkeypatch):
        monkeypatch.delenv("MESHCORE_PASSWORD", raising=False)
        monkeypatch.setattr("collector.core.load_env", lambda: None)   # ignore a real .env
        dev = FakeWifiDevice()
        core, ev, tmp = _run_core(f"socket://127.0.0.1:{dev.port}", None, "disconnected")
        try:
            assert "admin password" in ev["reason"]
            assert dev.lines == []
        finally:
            core.stop()
            tmp.__exit__(None, None, None)
            dev.close()

    def test_password_from_env(self, monkeypatch):
        monkeypatch.setenv("MESHCORE_PASSWORD", "from-env")
        assert CollectorCore(port="socket://x:1").password == "from-env"
        assert CollectorCore(port="socket://x:1", password="explicit").password == "explicit"

    def test_unreachable_host(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()   # nothing listening there now
        core, ev, tmp = _run_core(f"socket://127.0.0.1:{port}", PASSWORD, "disconnected")
        try:
            assert "Cannot open" in ev["reason"]
        finally:
            core.stop()
            tmp.__exit__(None, None, None)


def _wait_for_event(store, event, timeout=3.0):
    """Poll the durable connection log until an event of the given kind appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = [e for e in store.get_connection_events() if e["event"] == event]
        if rows:
            return rows
        time.sleep(0.05)
    return []


def _wait_for_boot(store, timeout=3.0):
    """Poll the durable device-boot log until a row appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = store.get_device_boots()
        if rows:
            return rows
        time.sleep(0.05)
    return []


class TestConnectionLog:
    """The durable link up/down log and the idle watchdog (core.py)."""

    def test_connected_event_recorded(self):
        dev = FakeWifiDevice()
        core, ev, tmp = _run_core(f"socket://127.0.0.1:{dev.port}", PASSWORD, "connected")
        try:
            assert ev["connected"].is_set(), ev["reason"]
            assert _wait_for_event(core.store, "connected")
        finally:
            core.stop()
            core.store.close()   # close this thread's connection before temp cleanup (Windows)
            tmp.__exit__(None, None, None)
            dev.close()

    def test_boot_info_recorded(self):
        dev = FakeWifiDevice()
        core, ev, tmp = _run_core(f"socket://127.0.0.1:{dev.port}", PASSWORD, "heartbeat")
        try:
            boots = _wait_for_boot(core.store)
            assert boots, "BOOT_INFO was not recorded"
            assert boots[0]["reset_name"] == "task watchdog"
            assert boots[0]["boot_count"] == 2
            assert boots[0]["prev_rssi"] == -72
            # a human-readable boot line is surfaced to the log
            assert any("device boot" in t for t in ev["text"])
        finally:
            core.stop()
            core.store.close()
            tmp.__exit__(None, None, None)
            dev.close()

    def test_idle_watchdog_disconnects(self, monkeypatch):
        # A device that connects then goes silent must be detected: read() keeps
        # returning empty with no error, so only the idle watchdog breaks the loop.
        monkeypatch.setattr("collector.core.LINK_IDLE_TIMEOUT", 0.5)
        dev = FakeWifiDevice()   # sends one heartbeat, then nothing
        core, ev, tmp = _run_core(f"socket://127.0.0.1:{dev.port}", PASSWORD,
                                  "disconnected", reconnect=False)
        try:
            assert ev["disconnected"].is_set()
            assert "idle" in ev["reason"], ev["reason"]
            downs = _wait_for_event(core.store, "disconnected")
            assert downs and "idle" in (downs[0]["detail"] or "")
            assert downs[0]["gap_secs"] is not None   # session length recorded
        finally:
            core.stop()
            core.store.close()   # close this thread's connection before temp cleanup (Windows)
            tmp.__exit__(None, None, None)
            dev.close()
