"""
MeshCore Collector — Standalone core.

CollectorCore manages the serial connection, reads frames from the collector
repeater, and stores everything in SQLite. It runs on its own thread and can
be used without any TUI — perfect for headless Raspberry Pi deployments.

Usage (standalone):
    core = CollectorCore(port="/dev/ttyUSB0")
    core = CollectorCore(port="socket://heltec-repeater.local:5005", password="...")  # WiFi build
    core.start()       # blocks on its own thread
    ...
    core.stop()

Usage (with pyos Service wrapper):
    The MeshCollectorService in mesh_service.py wraps this class and bridges
    events into the pyos event system.
"""

import os
import threading
import time
from typing import Callable

import serial
from serial.tools import list_ports

from .envfile import load_env

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

# Auto-reconnect backoff bounds (seconds). The firmware's ring buffer replays
# packets missed while the host was away, so reconnecting recovers the gap.
RECONNECT_MIN = 2.0
RECONNECT_MAX = 30.0

# A healthy device sends a HEARTBEAT every 10s even on a silent mesh. If nothing
# at all arrives for this long, the link is dead — on a network (socket://) link
# a peer that vanishes off WiFi leaves read() returning empty forever with no
# error, so without this watchdog the read loop would wait indefinitely on a
# corpse and never reconnect. Must comfortably exceed COLLECTOR_HEARTBEAT_INTERVAL.
LINK_IDLE_TIMEOUT = 45.0


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


def is_network_port(port):
    """True for pyserial URLs that reach the device over the network (WiFi build)."""
    return bool(port) and port.startswith(("socket://", "rfc2217://"))


def open_link(port, baud):
    """Open the device link: a serial port (COM3, /dev/ttyUSB0) or a pyserial URL
    such as socket://heltec-repeater.local:5005 for the WiFi collector build."""
    ser = serial.serial_for_url(port, baudrate=baud, timeout=0.1)
    if is_network_port(port):
        _enable_tcp_keepalive(ser)
    return ser


def _enable_tcp_keepalive(ser):
    """Turn on TCP keepalive for a socket:// link so a peer that silently drops
    off the network is detected by the OS instead of hanging forever. Best-effort:
    the application-level idle watchdog (LINK_IDLE_TIMEOUT) is the real backstop."""
    sock = getattr(ser, "_socket", None)
    if sock is None:
        return
    import socket as _socket
    try:
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
        # Tighten the idle/interval/count where the platform exposes it.
        for opt, val in (("TCP_KEEPIDLE", 20), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            name = getattr(_socket, opt, None)
            if name is not None:
                sock.setsockopt(_socket.IPPROTO_TCP, name, val)
    except (OSError, AttributeError):
        pass


class CollectorCore:
    """Manages serial connection and frame collection.

    Callbacks:
        on_frame(frame)     — called for every parsed frame
        on_text(line)       — called for every text line from CLI
        on_connected()      — called after handshake succeeds
        on_disconnected(reason) — called when connection is lost
        on_error(msg)       — called on non-fatal errors
    """

    def __init__(self, port=None, baud=115200, db_path="collector.db", password=None,
                 reconnect=True):
        self.port = port
        self.baud = baud
        self.db_path = db_path
        # Auto-reconnect with backoff after a dropped/failed connection, until
        # stop() is called. The caller can disable it for one-shot use.
        self.reconnect = reconnect
        # Admin password for network links (the WiFi build wants "auth <password>" first).
        # Falls back to MESHCORE_PASSWORD from the environment or the repo-root .env file.
        if password is None:
            load_env()
            password = os.environ.get("MESHCORE_PASSWORD")
        self.password = password

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
        # Set once a handshake succeeds; used to reset the reconnect backoff after
        # an established session drops (vs. a connection that never came up).
        self._session_established = False
        # Wall-clock time the link last went down, so a reconnect can log the gap.
        self._link_down_since = None

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

        backoff = RECONNECT_MIN
        try:
            while not self._stop_event.is_set():
                self._connected = False
                try:
                    self._connect_and_collect()
                except Exception as e:
                    self._fire_error(f"Collector error: {e}")

                if self._stop_event.is_set() or not self.reconnect:
                    break

                # Connection ended on its own. A session that actually came up
                # (handshake succeeded) resets the backoff so a drop reconnects
                # fast; connections that never established grow the backoff.
                if self._session_established:
                    backoff = RECONNECT_MIN
                self._fire_text(f"[collector] reconnecting in {backoff:.0f}s")
                if self._stop_event.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, RECONNECT_MAX)
        finally:
            self._cleanup()
            self._running = False

    def _log_link_down(self, reason, session_start):
        """Record a durable disconnect event for a session that was established,
        and remember when the link went down so the next connect logs the gap."""
        now = time.time()
        self._link_down_since = now
        if self._store is not None and session_start is not None:
            try:
                self._store.record_connection_event(
                    "disconnected", detail=reason,
                    gap_secs=round(now - session_start, 1), now=now)
            except Exception:
                pass

    def _connect_and_collect(self):
        """Connect to serial port, enable collector mode, and read frames."""
        self._session_established = False
        try:
            self._ser = open_link(self.port, self.baud)
        except (serial.SerialException, OSError, ValueError) as e:
            self._fire_disconnected(f"Cannot open {self.port}: {e}")
            return

        time.sleep(0.5)
        self._ser.reset_input_buffer()

        if is_network_port(self.port):
            error = self._authenticate()
            if error:
                self._fire_disconnected(error)
                return
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

        session_start = time.time()
        self._connected = True
        self._session_established = True
        if self.on_connected:
            self.on_connected()
        # Durable link-up log, written after notifying so a disk write never
        # delays the connected callback. gap_secs is the downtime since the
        # last drop (None on the first connect of this run).
        if self._store is not None:
            gap = None
            if self._link_down_since is not None:
                gap = round(session_start - self._link_down_since, 1)
            try:
                self._store.record_connection_event(
                    "connected", detail=f"protocol v{self._protocol_version}",
                    gap_secs=gap, now=session_start)
            except Exception:
                pass

        # Main read loop. last_data drives the idle watchdog: a live device sends
        # a heartbeat every 10s, so a long silence means the link is dead even
        # when read() keeps returning empty (a vanished network peer never errors).
        last_data = time.monotonic()
        while not self._stop_event.is_set():
            try:
                chunk = self._ser.read(256)
            except serial.SerialException as e:
                reason = f"Serial error: {e}"
                self._log_link_down(reason, session_start)
                self._fire_disconnected(reason)
                return

            if chunk:
                self._reader.feed(chunk)
                last_data = time.monotonic()
            elif time.monotonic() - last_data > LINK_IDLE_TIMEOUT:
                reason = f"link idle: no data for {LINK_IDLE_TIMEOUT:.0f}s"
                self._log_link_down(reason, session_start)
                self._fire_disconnected(reason)
                return

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
        self._log_link_down("Stopped by user", session_start)
        self._fire_disconnected("Stopped by user")

    def _authenticate(self):
        """Log in to a network link. Returns an error message, or None on success.

        The exchange is read raw, not through the FrameReader, so the echoed
        password never reaches the debug log.
        """
        if not self.password:
            return "Network link needs the admin password (--password or MESHCORE_PASSWORD)"
        self._ser.write(f"auth {self.password}\r".encode())
        buf = b""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self._stop_event.is_set():
            buf += self._ser.read(256)
            if b"OK - authenticated" in buf:
                return None
            if b"Err - auth" in buf:
                return "Authentication failed (wrong admin password)"
        return "No reply to auth"

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
