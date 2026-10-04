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
    FRAME_TYPE_RX_RAW,
    PAYLOAD_TYPE_GRP_TXT,
    FrameReader,
    build_ack_frame,
    build_resume_frame,
)
from .crypto import try_decode_group_message
from .store import CollectorStore

ACK_INTERVAL = 1.0  # seconds between ACK frames


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
        self.on_channel_message: Callable = None
        self.on_channel_discovered: Callable = None
        self.on_undecryptable: Callable = None

        self._channels = []
        self._undecryptable_count = 0
        self._cracker = None
        self._ser = None
        self._store = None
        self._reader = None
        self._running = False
        self._stop_event = threading.Event()
        self._thread = None
        self._connected = False

        # Group message dedup — tracks recent payload hashes to suppress
        # relay echoes (same message arriving via different paths).
        self._msg_dedup = set()
        self._msg_dedup_max = 256

        # Reliable delivery state (v2)
        self._protocol_version = 1
        self._highest_seq_seen = 0
        self._last_ack_time = 0.0
        self._replay_above_seq = 0  # skip storage for frames with seq <= this

    @property
    def is_running(self):
        return self._running

    @property
    def is_connected(self):
        return self._connected

    @property
    def store(self):
        return self._store

    def set_channels(self, channels):
        """Set the list of Channel objects for group message decoding."""
        self._channels = list(channels)

    def add_channel(self, channel):
        """Add a channel to the live decode list."""
        self._channels.append(channel)

    def remove_channel(self, name):
        """Remove a channel by name from the live decode list."""
        self._channels = [ch for ch in self._channels if ch.name != name]

    def set_cracker(self, cracker):
        """Attach a ChannelCracker instance for passive channel discovery."""
        self._cracker = cracker

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

        # Load channels from config if none were set explicitly
        if not self._channels:
            try:
                from .config import load_config, load_channels
                config = load_config()
                self._channels = load_channels(config)
            except Exception:
                pass

        # Ensure the default public channel is always present
        from .crypto import default_public_channel, DEFAULT_PUBLIC_CHANNEL_NAME
        if not any(ch.name == DEFAULT_PUBLIC_CHANNEL_NAME for ch in self._channels):
            self._channels.insert(0, default_public_channel())

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

        # Reset reliable delivery state
        self._protocol_version = 1
        self._highest_seq_seen = 0
        self._last_ack_time = 0.0
        self._replay_above_seq = 0

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
                        self._handle_handshake(parsed)
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

            # Periodic ACK (v2 only)
            if self._protocol_version >= 2 and self._highest_seq_seen > 0:
                now = time.monotonic()
                if now - self._last_ack_time >= ACK_INTERVAL:
                    self._send_ack(self._highest_seq_seen)
                    self._last_ack_time = now

        # Disable collector before disconnecting
        try:
            # Final ACK before disconnect
            if self._protocol_version >= 2 and self._highest_seq_seen > 0:
                self._send_ack(self._highest_seq_seen)
            self._ser.write(b"collector stop\r")
            time.sleep(0.3)
        except serial.SerialException:
            pass

        self._connected = False
        self._fire_disconnected("Stopped by user")

    def _handle_handshake(self, parsed):
        """Process handshake result — set up v2 reliable delivery if supported."""
        version = parsed.get("version", 1)
        self._protocol_version = version
        self._reader.protocol_version = version

        if version >= 2:
            last_committed = self._store.get_last_committed_seq()
            self._replay_above_seq = last_committed
            self._fire_text(
                f"[collector] v2 handshake: oldest={parsed.get('oldest_seq', '?')}, "
                f"newest={parsed.get('newest_seq', '?')}, resuming from seq {last_committed}"
            )
            try:
                self._ser.write(build_resume_frame(last_committed))
            except serial.SerialException:
                pass
            self._last_ack_time = time.monotonic()

    def _process_frame(self, frame):
        """Store frame, attempt channel decode, and fire callbacks."""
        seq = frame.get("seq")

        # v2 seq tracking
        if seq is not None:
            # Seq reset detection: if incoming seq is much lower than last seen
            if self._highest_seq_seen > 0 and seq < self._highest_seq_seen and seq < 100:
                self._fire_text(
                    f"[collector] seq reset detected: got {seq}, expected > {self._highest_seq_seen}"
                )
                self._replay_above_seq = 0  # clear replay watermark on reset

            if seq > self._highest_seq_seen:
                self._highest_seq_seen = seq

            # Replay dedup: skip storage for frames already committed
            if self._replay_above_seq > 0 and seq <= self._replay_above_seq:
                # Still fire callback (for live display) but don't store
                if self.on_frame:
                    self.on_frame(frame)
                return

            # Clear replay watermark once we see a frame above it
            if self._replay_above_seq > 0 and seq > self._replay_above_seq:
                self._replay_above_seq = 0

        raw_packet_id = self._store.store_frame(frame)
        if self.on_frame:
            self.on_frame(frame)

        # Try to decode group channel messages
        if (
            frame["type"] == FRAME_TYPE_RX_RAW
            and frame.get("parsed")
            and frame["parsed"].get("payload_type") == PAYLOAD_TYPE_GRP_TXT
        ):
            raw = frame["parsed"].get("raw")
            if raw and isinstance(raw, bytes):
                # Dedup: extract the payload portion (after path) which is
                # identical across relay paths.  Use its hash to suppress
                # duplicates from echo + relay.
                from .crypto import extract_group_payload
                extracted = extract_group_payload(raw)
                if extracted:
                    dedup_key = (extracted["channel_hash"], hash(extracted["mac_and_data"]))
                    if dedup_key in self._msg_dedup:
                        return  # already decoded this message
                    self._msg_dedup.add(dedup_key)
                    if len(self._msg_dedup) > self._msg_dedup_max:
                        # Evict oldest ~half to avoid unbounded growth
                        to_remove = list(self._msg_dedup)[:self._msg_dedup_max // 2]
                        self._msg_dedup -= set(to_remove)

                msg = None
                if self._channels:
                    msg = try_decode_group_message(
                        raw, self._channels, frame.get("received_at", time.time())
                    )
                if msg:
                    self._store.store_channel_message(msg, raw_packet_id=raw_packet_id)
                    if self.on_channel_message:
                        self.on_channel_message(msg)
                else:
                    self._undecryptable_count += 1
                    if self.on_undecryptable:
                        self.on_undecryptable(self._undecryptable_count)
                    if self._cracker and extracted:
                        self._cracker.notify_unknown_hash(extracted["channel_hash"])

    def _send_ack(self, seq):
        """Send HOST_ACK frame and persist last committed seq."""
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(build_ack_frame(seq))
                self._store.set_last_committed_seq(seq)
            except serial.SerialException:
                pass

    def send_command(self, cmd):
        """Send a CLI command to the device. Returns True if sent, False otherwise."""
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(cmd.encode() if isinstance(cmd, str) else cmd)
                return True
            except serial.SerialException:
                return False
        return False

    def send_screen_text(self, text=None):
        """Show text on the device's OLED display, or clear it if text is None."""
        if text:
            return self.send_command(f"collector screen {text}\r")
        return self.send_command("collector screen\r")

    def send_channel_message(self, channel_name, sender_name, text):
        """Send a group message on a channel via the firmware.

        Uses the firmware's ``collector send`` CLI command which encrypts,
        transmits over LoRa, and echoes the packet back into the collector
        pipeline.  The firmware handles channel lookup: registered PSK
        channels (e.g. "Public") use their pre-configured key, while
        hashtag channels (``#name``) derive the key via SHA-256.

        Returns True if the command was sent to the device, False otherwise.
        """
        cmd = f"collector send {channel_name} {sender_name} {text}\r"
        return self.send_command(cmd)

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
