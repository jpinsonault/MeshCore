"""
Remote operations on a collector repeater WiFi build (Heltec_v3_collector_wifi).

    python -m collector.remote cmd ver "wifi status" neighbors   # run CLI commands, print replies
    python -m collector.remote flash                              # network firmware update, then report version
    python -m collector.remote flash path/to/firmware.bin

Settings come from the environment or the repo-root .env file:
    MESHCORE_HOST      node hostname or IP (e.g. my-node.local)
    MESHCORE_PASSWORD  node admin password (never printed)

CLI commands go over the WebSocket link (ws://<host>/ws), so they don't kick a collector off TCP 5005.
"""

import argparse
import importlib.util
import logging
import random
import socket
import sys
import time
from pathlib import Path

from websockets.sync.client import connect as ws_connect

from .envfile import REPO_ROOT, load_env

DEFAULT_FIRMWARE = REPO_ROOT / ".pio" / "build" / "Heltec_v3_collector_wifi" / "firmware.bin"
ESPOTA = Path.home() / ".platformio" / "packages" / "framework-arduinoespressif32" / "tools" / "espota.py"
FRAME_START = 0xC0


class RemoteError(Exception):
    pass


def settings():
    import os
    load_env()
    host = os.environ.get("MESHCORE_HOST")
    password = os.environ.get("MESHCORE_PASSWORD")
    if not host or not password:
        raise RemoteError("Set MESHCORE_HOST and MESHCORE_PASSWORD in the repo-root .env file")
    return host, password


def resolve_ipv4(host):
    """IPv4 address for host. Windows can stall trying IPv6 first for .local names."""
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except socket.gaierror as e:
        raise RemoteError(f"Cannot resolve {host}: {e}")


class RemoteCLI:
    """CLI session over the node's WebSocket link. Skips collector frames if any arrive."""

    def __init__(self, host, password, timeout=5.0):
        self.ip = resolve_ipv4(host)
        try:
            self.ws = ws_connect(f"ws://{self.ip}/ws", open_timeout=timeout, max_size=None)
        except Exception as e:
            raise RemoteError(f"Cannot connect to ws://{self.ip}/ws: {e}")
        self._buf = b""
        self.ws.send(f"auth {password}\r".encode())
        text = self._read_until([b"OK - authenticated", b"Err - auth"], timeout)
        if b"OK - authenticated" not in text:
            self.close()
            raise RemoteError("Login refused (check MESHCORE_PASSWORD)")
        self._read_idle(0.3)   # rest of the login reply line

    def _recv(self, timeout):
        try:
            data = self.ws.recv(timeout=timeout)
        except TimeoutError:
            return False
        self._buf += data if isinstance(data, bytes) else data.encode()
        return True

    def _take_text(self):
        """Pop text from the buffer, dropping complete collector frames ([0xC0][len16][body])."""
        out = bytearray()
        buf = self._buf
        i = 0
        while i < len(buf):
            if buf[i] == FRAME_START:
                if i + 3 > len(buf):
                    break
                end = i + 3 + (buf[i + 1] | (buf[i + 2] << 8))
                if end > len(buf):
                    break
                i = end
                continue
            out.append(buf[i])
            i += 1
        self._buf = buf[i:]
        return bytes(out)

    def _read_until(self, markers, timeout):
        text = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._recv(max(0.05, deadline - time.monotonic()))
            text += self._take_text()
            if any(m in text for m in markers):
                return text
        return text

    def _read_idle(self, idle):
        text = b""
        while self._recv(idle):
            text += self._take_text()
        return text + self._take_text()

    def run(self, command, timeout=5.0):
        """Send one CLI command; returns its reply ('' if the command doesn't reply)."""
        self.ws.send(f"{command}\r".encode())
        text = self._read_until([b"  -> "], timeout)
        if b"  -> " not in text:
            return ""
        while b"\n" not in text.split(b"  -> ", 1)[1]:
            more = self._read_until([b"\n"], 1.0)
            if not more:
                break
            text += more
        return text.split(b"  -> ", 1)[1].split(b"\n", 1)[0].decode(errors="replace").strip()

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def cmd_main(commands):
    host, password = settings()
    cli = RemoteCLI(host, password)
    try:
        for c in commands:
            print(f"> {c}\n{cli.run(c)}", flush=True)
    finally:
        cli.close()


def load_espota():
    if not ESPOTA.is_file():
        raise RemoteError(f"espota.py not found at {ESPOTA} (install the ESP32 platform with pio first)")
    spec = importlib.util.spec_from_file_location("espota", ESPOTA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def flash_main(firmware):
    firmware = Path(firmware)
    if not firmware.is_file():
        raise RemoteError(f"No firmware at {firmware} (build it first: pio run -e Heltec_v3_collector_wifi)")
    host, password = settings()
    ip = resolve_ipv4(host)

    try:
        cli = RemoteCLI(host, password)
        print(f"before: {cli.run('ver')}", flush=True)
        cli.close()
    except RemoteError as e:
        print(f"before: (could not read version: {e})", flush=True)

    print(f"flashing {firmware} ({firmware.stat().st_size} bytes) to {host} ({ip})", flush=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    espota = load_espota()
    espota.PROGRESS = True
    rc = espota.serve(ip, "0.0.0.0", 3232, random.randint(10000, 60000), password, str(firmware), espota.FLASH)
    if rc != 0:
        raise RemoteError("Network update failed (is the firewall allowing Python?)")

    print("waiting for reboot...", flush=True)
    deadline = time.monotonic() + 60
    time.sleep(8)
    while time.monotonic() < deadline:
        try:
            cli = RemoteCLI(host, password, timeout=3)
            print(f"after: {cli.run('ver')}", flush=True)
            cli.close()
            return
        except RemoteError:
            time.sleep(3)
    raise RemoteError("Node did not come back within 60s")


def main():
    parser = argparse.ArgumentParser(description="Remote CLI and network flashing for the collector WiFi build")
    sub = parser.add_subparsers(dest="action", required=True)
    p_cmd = sub.add_parser("cmd", help="run CLI commands over WiFi")
    p_cmd.add_argument("commands", nargs="+")
    p_flash = sub.add_parser("flash", help="network firmware update (espota)")
    p_flash.add_argument("firmware", nargs="?", default=str(DEFAULT_FIRMWARE))
    args = parser.parse_args()
    try:
        if args.action == "cmd":
            cmd_main(args.commands)
        else:
            flash_main(args.firmware)
    except RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
