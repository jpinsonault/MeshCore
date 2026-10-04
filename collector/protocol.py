"""
MeshCore Collector Protocol — Frame constants and parsing.

Extracted from collector_test.py into a reusable module. This is the single
source of truth for the binary frame protocol between the collector repeater
firmware and the host computer.
"""

import struct
import time

# --- Frame constants ---

FRAME_START = 0xC0

FRAME_TYPE_RX_RAW = 0xD0
FRAME_TYPE_TX_RAW = 0xD1
FRAME_TYPE_ADVERTISEMENT = 0xD2
FRAME_TYPE_HEARTBEAT = 0xD3
FRAME_TYPE_DIAGNOSTICS = 0xD4
FRAME_TYPE_HANDSHAKE = 0xDF

# Host -> Device frame types
FRAME_TYPE_HOST_ACK = 0xA0
FRAME_TYPE_HOST_RESUME = 0xA1

FRAME_TYPE_NAMES = {
    FRAME_TYPE_RX_RAW: "RX_RAW",
    FRAME_TYPE_TX_RAW: "TX_RAW",
    FRAME_TYPE_ADVERTISEMENT: "ADVERTISEMENT",
    FRAME_TYPE_HEARTBEAT: "HEARTBEAT",
    FRAME_TYPE_DIAGNOSTICS: "DIAGNOSTICS",
    FRAME_TYPE_HANDSHAKE: "HANDSHAKE",
}

PAYLOAD_TYPES = {
    0x00: "REQ",
    0x01: "RESPONSE",
    0x02: "TXT_MSG",
    0x03: "ACK",
    0x04: "ADVERT",
    0x05: "GRP_TXT",
    0x06: "GRP_DATA",
    0x07: "ANON_REQ",
    0x08: "PATH",
    0x09: "TRACE",
    0x0A: "MULTIPART",
    0x0B: "CONTROL",
    0x0F: "RAW_CUSTOM",
}

PAYLOAD_TYPE_GRP_TXT = 0x05
PAYLOAD_TYPE_GRP_DATA = 0x06

ROUTE_TYPES = {
    0x00: "TRANSPORT_FLOOD",
    0x01: "FLOOD",
    0x02: "DIRECT",
    0x03: "TRANSPORT_DIRECT",
}

ADV_TYPES = {
    0: "NONE",
    1: "CHAT",
    2: "REPEATER",
    3: "ROOM",
    4: "SENSOR",
}


# --- CRC-16-CCITT (poly 0x1021, init 0xFFFF) ---

def crc16_ccitt(data: bytes) -> int:
    """Compute CRC-16-CCITT over data. Returns uint16."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


# --- Host-to-device frame builders ---

def build_host_frame(frame_type: int, payload: bytes = b"") -> bytes:
    """Build a complete v2 wire frame for host-to-device communication.

    Wire format: [0xC0] [len_lo] [len_hi] [type] [seq(4B LE)] [payload] [crc16(2B LE)]
    Host frames always use seq=0.
    """
    seq = struct.pack("<I", 0)
    crc_data = bytes([frame_type]) + seq + payload
    crc = crc16_ccitt(crc_data)
    frame_len = 1 + 4 + len(payload) + 2  # type + seq + payload + crc
    return (
        bytes([FRAME_START])
        + struct.pack("<H", frame_len)
        + crc_data
        + struct.pack("<H", crc)
    )


def build_ack_frame(last_seq: int) -> bytes:
    """Build a HOST_ACK frame: tells firmware 'I have committed through seq N'."""
    return build_host_frame(FRAME_TYPE_HOST_ACK, struct.pack("<I", last_seq))


def build_resume_frame(from_seq: int) -> bytes:
    """Build a HOST_RESUME frame: tells firmware 'replay everything after seq N'."""
    return build_host_frame(FRAME_TYPE_HOST_RESUME, struct.pack("<I", from_seq))


# --- Parsers ---

def parse_packet_header(raw):
    """Parse the first byte of a mesh packet into route/payload/version."""
    header = raw[0]
    return {
        "header": header,
        "route_type": header & 0x03,
        "route_name": ROUTE_TYPES.get(header & 0x03, f"?{header & 0x03}"),
        "payload_type": (header >> 2) & 0x0F,
        "payload_name": PAYLOAD_TYPES.get((header >> 2) & 0x0F, f"?0x{(header >> 2) & 0x0F:X}"),
        "version": (header >> 6) & 0x03,
    }


def parse_rx_raw(payload):
    """Parse an RX_RAW frame payload."""
    if len(payload) < 3:
        return {"error": "too short"}
    snr_x4 = struct.unpack("b", payload[0:1])[0]
    rssi = struct.unpack("b", payload[1:2])[0]
    raw = payload[2:]
    result = {
        "snr": snr_x4 / 4.0,
        "rssi": rssi,
        "raw_len": len(raw),
        "raw": raw,
    }
    result.update(parse_packet_header(raw))
    return result


def parse_tx_raw(payload):
    """Parse a TX_RAW frame payload."""
    if len(payload) < 3:
        return {"error": "too short"}
    result = {"raw_len": len(payload), "raw": payload}
    result.update(parse_packet_header(payload))
    return result


def parse_advertisement(payload):
    """Parse an ADVERTISEMENT frame payload."""
    if len(payload) < 37:
        return {"error": f"too short ({len(payload)} bytes)"}
    timestamp = struct.unpack("<I", payload[0:4])[0]
    snr_x4 = struct.unpack("b", payload[4:5])[0]
    pub_key = payload[5:37]
    app_data = payload[37:]

    result = {
        "timestamp": timestamp,
        "snr": snr_x4 / 4.0,
        "pub_key": pub_key,
        "pub_key_hex": pub_key.hex(),
    }

    if len(app_data) > 0:
        flags = app_data[0]
        adv_type = flags & 0x0F
        result["adv_type"] = adv_type
        result["adv_type_name"] = ADV_TYPES.get(adv_type, f"?{adv_type}")

        i = 1
        if flags & 0x10 and len(app_data) >= i + 8:
            lat = struct.unpack("<i", app_data[i:i + 4])[0] / 1e6
            lon = struct.unpack("<i", app_data[i + 4:i + 8])[0] / 1e6
            result["lat"] = lat
            result["lon"] = lon
            i += 8
        if flags & 0x20 and len(app_data) >= i + 2:
            i += 2
        if flags & 0x40 and len(app_data) >= i + 2:
            i += 2
        if flags & 0x80 and i < len(app_data):
            name_bytes = app_data[i:]
            try:
                result["name"] = name_bytes.decode("utf-8").rstrip("\x00")
            except UnicodeDecodeError:
                result["name"] = name_bytes.hex()

    return result


def parse_heartbeat(payload):
    """Parse a HEARTBEAT frame payload."""
    if len(payload) < 27:
        return {"error": f"too short ({len(payload)} bytes)"}
    timestamp, battery_mv = struct.unpack("<IH", payload[0:6])
    rx_flood, rx_direct = struct.unpack("<II", payload[6:14])
    tx_flood, tx_direct = struct.unpack("<II", payload[14:22])
    free_pkts = payload[22]
    uptime_secs = struct.unpack("<I", payload[23:27])[0]

    return {
        "timestamp": timestamp,
        "battery_mv": battery_mv,
        "rx_flood": rx_flood,
        "rx_direct": rx_direct,
        "tx_flood": tx_flood,
        "tx_direct": tx_direct,
        "free_pkts": free_pkts,
        "uptime_secs": uptime_secs,
    }


def parse_diagnostics(payload):
    """Parse a DIAGNOSTICS frame payload (50 bytes)."""
    if len(payload) < 50:
        return {"error": f"too short ({len(payload)} bytes, need 50)"}
    (
        mcu_temp,
        free_heap, min_free_heap, total_heap,
        noise_floor, last_rssi, last_snr_x4,
        tx_airtime_ms, rx_airtime_ms,
        recv_errors, err_flags, tx_queue_len,
        direct_dups, flood_dups,
        n_recv, n_sent,
    ) = struct.unpack("<f3I3h2I I H H 2H 2I", payload[:50])

    return {
        "mcu_temp": mcu_temp,
        "free_heap": free_heap,
        "min_free_heap": min_free_heap,
        "total_heap": total_heap,
        "noise_floor": noise_floor,
        "last_rssi": last_rssi,
        "last_snr": last_snr_x4 / 4.0,
        "tx_airtime_ms": tx_airtime_ms,
        "rx_airtime_ms": rx_airtime_ms,
        "recv_errors": recv_errors,
        "err_flags": err_flags,
        "tx_queue_len": tx_queue_len,
        "direct_dups": direct_dups,
        "flood_dups": flood_dups,
        "n_recv": n_recv,
        "n_sent": n_sent,
    }


def parse_handshake(payload):
    """Parse a HANDSHAKE frame payload.

    v1: 10 bytes — "COLLECTOR" + version(1)
    v2: 18 bytes — "COLLECTOR" + version(2) + oldest_seq(4) + newest_seq(4)
    """
    if len(payload) < 10:
        return {"error": f"too short ({len(payload)} bytes)"}
    magic = payload[0:9].decode("ascii", errors="replace")
    version = payload[9]
    result = {
        "magic": magic,
        "version": version,
        "valid": magic == "COLLECTOR",
    }
    if version >= 2 and len(payload) >= 18:
        result["oldest_seq"] = struct.unpack("<I", payload[10:14])[0]
        result["newest_seq"] = struct.unpack("<I", payload[14:18])[0]
    return result


FRAME_PARSERS = {
    FRAME_TYPE_RX_RAW: parse_rx_raw,
    FRAME_TYPE_TX_RAW: parse_tx_raw,
    FRAME_TYPE_ADVERTISEMENT: parse_advertisement,
    FRAME_TYPE_HEARTBEAT: parse_heartbeat,
    FRAME_TYPE_DIAGNOSTICS: parse_diagnostics,
    FRAME_TYPE_HANDSHAKE: parse_handshake,
}


def parse_frame(frame_type, payload):
    """Parse a frame payload by type. Returns dict or None."""
    parser = FRAME_PARSERS.get(frame_type)
    if parser:
        try:
            return parser(payload)
        except Exception as e:
            return {"error": str(e)}
    return None


class FrameReader:
    """Reads and separates binary frames from the mixed text/binary serial stream.

    Call ``feed(byte)`` for each byte from the serial port. Complete frames and
    text lines are accumulated into ``frames`` and ``text_lines``.

    Protocol version handling:
    - Starts in v1 mode (no seq, no CRC)
    - Automatically upgrades to v2 after receiving a handshake with version >= 2
    - In v2 mode: extracts seq (4 bytes after type), validates CRC (last 2 bytes),
      strips both before passing payload to parsers
    """

    def __init__(self):
        self.frames = []
        self.text_lines = []
        self.errors = []
        self.protocol_version = 1
        self._text_buf = bytearray()
        self._state = "idle"  # idle | read_len | read_data
        self._len_buf = bytearray()
        self._frame_len = 0
        self._data_buf = bytearray()

    def feed(self, data):
        """Feed raw bytes from the serial port. Can be called with any chunk size."""
        for b in data:
            self._feed_byte(b)

    def _feed_byte(self, b):
        if self._state == "idle":
            if b == FRAME_START:
                if self._text_buf:
                    self._flush_text()
                self._state = "read_len"
                self._len_buf = bytearray()
            elif b == 0x0A:  # \n
                if self._text_buf:
                    self._flush_text()
            elif b >= 0x20 or b == 0x0D:
                self._text_buf.append(b)

        elif self._state == "read_len":
            self._len_buf.append(b)
            if len(self._len_buf) == 2:
                self._frame_len = struct.unpack("<H", self._len_buf)[0]
                if self._frame_len == 0 or self._frame_len > 300:
                    self.errors.append(f"Invalid frame length: {self._frame_len}")
                    self._state = "idle"
                else:
                    self._data_buf = bytearray()
                    self._state = "read_data"

        elif self._state == "read_data":
            self._data_buf.append(b)
            if len(self._data_buf) == self._frame_len:
                self._emit_frame()
                self._state = "idle"

    def _emit_frame(self):
        frame_type = self._data_buf[0]

        # Handshake is always v1 format (even from v2 firmware)
        if frame_type == FRAME_TYPE_HANDSHAKE:
            payload = bytes(self._data_buf[1:])
            frame = {
                "type": frame_type,
                "type_name": FRAME_TYPE_NAMES.get(frame_type, f"UNKNOWN(0x{frame_type:02X})"),
                "payload": payload,
                "received_at": time.time(),
                "parsed": parse_frame(frame_type, payload),
            }
            self.frames.append(frame)
            # Auto-upgrade to v2 if handshake indicates it
            parsed = frame.get("parsed")
            if parsed and parsed.get("version", 1) >= 2:
                self.protocol_version = 2
            return

        if self.protocol_version >= 2:
            # v2 wire: [type] [seq(4B)] [payload...] [crc16(2B)]
            raw_data = bytes(self._data_buf)
            if len(raw_data) < 7:  # type(1) + seq(4) + crc(2) minimum
                self.errors.append(f"v2 frame too short: {len(raw_data)} bytes")
                return
            # Validate CRC (covers type + seq + payload, last 2 bytes are CRC)
            crc_data = raw_data[:-2]
            crc_expected = struct.unpack("<H", raw_data[-2:])[0]
            crc_actual = crc16_ccitt(crc_data)
            if crc_actual != crc_expected:
                self.errors.append(
                    f"CRC mismatch: expected 0x{crc_expected:04X}, got 0x{crc_actual:04X}"
                )
                return
            # Extract seq and payload
            seq = struct.unpack("<I", raw_data[1:5])[0]
            payload = bytes(raw_data[5:-2])
            frame = {
                "type": frame_type,
                "type_name": FRAME_TYPE_NAMES.get(frame_type, f"UNKNOWN(0x{frame_type:02X})"),
                "payload": payload,
                "seq": seq,
                "received_at": time.time(),
                "parsed": parse_frame(frame_type, payload),
            }
            self.frames.append(frame)
        else:
            # v1 wire: [type] [payload...]
            payload = bytes(self._data_buf[1:])
            frame = {
                "type": frame_type,
                "type_name": FRAME_TYPE_NAMES.get(frame_type, f"UNKNOWN(0x{frame_type:02X})"),
                "payload": payload,
                "received_at": time.time(),
                "parsed": parse_frame(frame_type, payload),
            }
            self.frames.append(frame)

    def _flush_text(self):
        line = self._text_buf.decode("ascii", errors="replace").strip()
        if line:
            self.text_lines.append(line)
        self._text_buf = bytearray()

    def take_frames(self):
        """Return and clear accumulated frames."""
        frames = self.frames
        self.frames = []
        return frames

    def take_text(self):
        """Return and clear accumulated text lines."""
        lines = self.text_lines
        self.text_lines = []
        return lines
