"""
MeshCore Collector — Standalone core.

CollectorCore manages the serial connection, reads frames from the collector
repeater, and stores everything in SQLite. It runs on its own thread and can
be used without any TUI — perfect for headless Raspberry Pi deployments.

Usage (standalone):
    core = CollectorCore(port="/dev/ttyUSB0")
    core.start()       # blocks on its own thread
    ...
    core.stop()

Usage (with pyos Service wrapper):
    The MeshCollectorService in mesh_service.py wraps this class and bridges
    events into the pyos event system.
"""

import threading
import time
from typing import Callable

import serial
from serial.tools import list_ports

from .protocol import (
    FRAME_TYPE_HANDSHAKE,
    FRAME_TYPE_HEARTBEAT,
    FrameReader,
)
from .store import CollectorStore


def list_serial_ports():
    """Return list of available serial port info dicts."""
    ports = list_ports.comports()
    return [
        {
            "device": p.device,
            "description": p.description,
            "hwid": p.hwid,
            "name": p.name,
        }
        for p in sorted(ports, key=lambda p: p.device)
    ]


class CollectorCore:
    """Manages serial connection and frame collection.

    Callbacks:
        on_frame(frame)     — called for every parsed frame
        on_text(line)       — called for every text line from CLI
        on_connected()      — called after handshake succeeds
        on_disconnected(reason) — called when connection is lost
        on_error(msg)       — called on non-fatal errors
    """

    def __init__(self, port=None, baud=115200, db_path="collector.db"):
        self.port = port
        self.baud = baud
        self.db_path = db_path

        # Callbacks (set by the caller or Service wrapper)
        self.on_frame: Callable = None
        self.on_text: Callable = None
        self.on_connected: Callable = None
        self.on_disconnected: Callable = None
        self.on_error: Callable = None

        self._ser = None
        self._store = None
        self._reader = None
        self._running = False
        self._stop_event = threading.Event()
        self._thread = None
        self._connected = False

    @property
    def is_running(self):
        return self._running

    @property
    def is_connected(self):
        return self._connected

    @property
    def store(self):
        return self._store

    def start(self):
        """Start the collector on a background thread."""
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the collector to stop and wait for it."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._running = False

    def _run(self):
        """Main loop: connect, enable collector, read frames, store."""
        self._running = True
        self._store = CollectorStore(self.db_path)
        self._store.open()

        try:
            self._connect_and_collect()
        except Exception as e:
            self._fire_error(f"Fatal error: {e}")
        finally:
            self._cleanup()
            self._running = False

    def _connect_and_collect(self):
        """Connect to serial port, enable collector mode, and read frames."""
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except serial.SerialException as e:
            self._fire_disconnected(f"Cannot open {self.port}: {e}")
            return

        time.sleep(0.5)
        self._ser.reset_input_buffer()
        self._reader = FrameReader()

        # Enable collector mode
        self._ser.write(b"collector start\r")

        # Wait for handshake
        deadline = time.monotonic() + 5.0
        handshake_ok = False
        while time.monotonic() < deadline and not self._stop_event.is_set():
            chunk = self._ser.read(256)
            if chunk:
                self._reader.feed(chunk)
            for frame in self._reader.take_frames():
                self._process_frame(frame)
                if frame["type"] == FRAME_TYPE_HANDSHAKE:
                    parsed = frame.get("parsed", {})
                    if parsed.get("valid"):
                        handshake_ok = True
            for line in self._reader.take_text():
                self._fire_text(line)
            if handshake_ok:
                break

        if not handshake_ok:
            self._fire_disconnected("No valid handshake received")
            return

        self._connected = True
        if self.on_connected:
            self.on_connected()

        # Main read loop
        while not self._stop_event.is_set():
            try:
                chunk = self._ser.read(256)
            except serial.SerialException as e:
                self._fire_disconnected(f"Serial error: {e}")
                return

            if chunk:
                self._reader.feed(chunk)

            for frame in self._reader.take_frames():
                self._process_frame(frame)

            for line in self._reader.take_text():
                self._fire_text(line)

        # Disable collector before disconnecting
        try:
            self._ser.write(b"collector stop\r")
            time.sleep(0.3)
        except serial.SerialException:
            pass

        self._connected = False
        self._fire_disconnected("Stopped by user")

    def _process_frame(self, frame):
        """Store frame and fire callback."""
        self._store.store_frame(frame)
        if self.on_frame:
            self.on_frame(frame)

    def _fire_text(self, line):
        if self.on_text:
            self.on_text(line)

    def _fire_disconnected(self, reason):
        self._connected = False
        if self.on_disconnected:
            self.on_disconnected(reason)

    def _fire_error(self, msg):
        if self.on_error:
            self.on_error(msg)

    def _cleanup(self):
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
        if self._store:
            self._store.close()
