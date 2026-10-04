#!/usr/bin/env python3
"""
MeshCore Collector Repeater — API Validation Test

Connects to a collector-enabled repeater over serial, enables collector mode,
and validates the binary frame protocol.

Usage:
    python collector_test.py [--port /dev/cu.usbserial-0001] [--duration 30]
"""

import argparse
import struct
import sys
import time
from datetime import datetime, timezone

import serial

# --- Protocol constants ---

FRAME_START = 0xC0

FRAME_TYPE_RX_RAW = 0xD0
FRAME_TYPE_TX_RAW = 0xD1
FRAME_TYPE_ADVERTISEMENT = 0xD2
FRAME_TYPE_HEARTBEAT = 0xD3
FRAME_TYPE_HANDSHAKE = 0xDF

FRAME_TYPE_NAMES = {
    FRAME_TYPE_RX_RAW: "RX_RAW",
    FRAME_TYPE_TX_RAW: "TX_RAW",
    FRAME_TYPE_ADVERTISEMENT: "ADVERTISEMENT",
    FRAME_TYPE_HEARTBEAT: "HEARTBEAT",
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


class CollectorReader:
    """Reads and parses the mixed text/binary stream from a collector repeater."""

    def __init__(self, ser):
        self.ser = ser
        self.text_lines = []
        self.frames = []
        self.errors = []

    def send_command(self, cmd):
        """Send a text command to the repeater CLI."""
        self.ser.write(f"{cmd}\r".encode())

    def read_until_frame_or_timeout(self, timeout=5.0, frame_type=None):
        """Read from serial until a specific frame type is received or timeout."""
        deadline = time.monotonic() + timeout
        text_buf = bytearray()

        while time.monotonic() < deadline:
            if not self.ser.in_waiting:
                time.sleep(0.01)
                continue

            b = self.ser.read(1)
            if not b:
                continue

            byte = b[0]

            if byte == FRAME_START:
                if text_buf:
                    self._process_text(text_buf)
                    text_buf = bytearray()
                frame = self._read_frame(deadline)
                if frame:
                    self.frames.append(frame)
                    if frame_type is None or frame["type"] == frame_type:
                        return frame
            elif byte == 0x0A:  # \n
                if text_buf:
                    self._process_text(text_buf)
                    text_buf = bytearray()
            elif byte >= 0x20 or byte == 0x0D:  # printable or \r
                text_buf.append(byte)

        if text_buf:
            self._process_text(text_buf)
        return None

    def read_all(self, duration=10.0):
        """Read all frames and text for a given duration."""
        deadline = time.monotonic() + duration
        text_buf = bytearray()

        while time.monotonic() < deadline:
            if not self.ser.in_waiting:
                time.sleep(0.01)
                continue

            b = self.ser.read(1)
            if not b:
                continue

            byte = b[0]

            if byte == FRAME_START:
                if text_buf:
                    self._process_text(text_buf)
                    text_buf = bytearray()
                frame = self._read_frame(deadline)
                if frame:
                    self.frames.append(frame)
            elif byte == 0x0A:
                if text_buf:
                    self._process_text(text_buf)
                    text_buf = bytearray()
            elif byte >= 0x20 or byte == 0x0D:
                text_buf.append(byte)

        if text_buf:
            self._process_text(text_buf)

    def _read_frame(self, deadline):
        """Read a binary frame after the start byte (0xC0) has been consumed."""
        try:
            remaining = max(0.1, deadline - time.monotonic())
            self.ser.timeout = min(remaining, 2.0)
            len_bytes = self.ser.read(2)
            if len(len_bytes) < 2:
                self.errors.append("Incomplete frame length")
                return None

            frame_len = struct.unpack("<H", len_bytes)[0]
            if frame_len == 0 or frame_len > 300:
                self.errors.append(f"Invalid frame length: {frame_len}")
                return None

            remaining = max(0.1, deadline - time.monotonic())
            self.ser.timeout = min(remaining, 2.0)
            data = self.ser.read(frame_len)
            if len(data) < frame_len:
                self.errors.append(
                    f"Incomplete frame data: got {len(data)}, expected {frame_len}"
                )
                return None

            frame_type = data[0]
            payload = bytes(data[1:])

            return {
                "type": frame_type,
                "type_name": FRAME_TYPE_NAMES.get(
                    frame_type, f"UNKNOWN(0x{frame_type:02X})"
                ),
                "payload": payload,
                "received_at": time.time(),
                "parsed": self._parse_frame(frame_type, payload),
            }
        except Exception as e:
            self.errors.append(f"Frame read error: {e}")
            return None

    def _parse_frame(self, frame_type, payload):
        """Parse a frame payload into structured data."""
        try:
            if frame_type == FRAME_TYPE_RX_RAW:
                return self._parse_rx_raw(payload)
            elif frame_type == FRAME_TYPE_TX_RAW:
                return self._parse_tx_raw(payload)
            elif frame_type == FRAME_TYPE_ADVERTISEMENT:
                return self._parse_advertisement(payload)
            elif frame_type == FRAME_TYPE_HEARTBEAT:
                return self._parse_heartbeat(payload)
            elif frame_type == FRAME_TYPE_HANDSHAKE:
                return self._parse_handshake(payload)
        except Exception as e:
            return {"error": str(e)}
        return None

    def _parse_rx_raw(self, payload):
        if len(payload) < 3:
            return {"error": "too short"}
        snr_x4 = struct.unpack("b", payload[0:1])[0]
        rssi = struct.unpack("b", payload[1:2])[0]
        raw = payload[2:]

        header = raw[0]
        route_type = header & 0x03
        payload_type = (header >> 2) & 0x0F
        version = (header >> 6) & 0x03

        return {
            "snr": snr_x4 / 4.0,
            "rssi": rssi,
            "raw_len": len(raw),
            "header": header,
            "route_type": route_type,
            "route_name": ROUTE_TYPES.get(route_type, f"?{route_type}"),
            "payload_type": payload_type,
            "payload_name": PAYLOAD_TYPES.get(payload_type, f"?0x{payload_type:X}"),
            "version": version,
        }

    def _parse_tx_raw(self, payload):
        if len(payload) < 3:
            return {"error": "too short"}
        header = payload[0]
        route_type = header & 0x03
        payload_type = (header >> 2) & 0x0F

        return {
            "raw_len": len(payload),
            "header": header,
            "route_type": route_type,
            "route_name": ROUTE_TYPES.get(route_type, f"?{route_type}"),
            "payload_type": payload_type,
            "payload_name": PAYLOAD_TYPES.get(payload_type, f"?0x{payload_type:X}"),
        }

    def _parse_advertisement(self, payload):
        if len(payload) < 37:
            return {"error": f"too short ({len(payload)} bytes)"}
        timestamp = struct.unpack("<I", payload[0:4])[0]
        snr_x4 = struct.unpack("b", payload[4:5])[0]
        pub_key = payload[5:37]
        app_data = payload[37:]

        result = {
            "timestamp": timestamp,
            "snr": snr_x4 / 4.0,
            "pub_key_hex": pub_key.hex(),
        }

        if len(app_data) > 0:
            flags = app_data[0]
            adv_type = flags & 0x0F
            result["adv_type"] = adv_type
            result["adv_type_name"] = ADV_TYPES.get(adv_type, f"?{adv_type}")

            i = 1
            if flags & 0x10 and len(app_data) >= i + 8:  # has lat/lon
                lat = struct.unpack("<i", app_data[i : i + 4])[0] / 1e6
                lon = struct.unpack("<i", app_data[i + 4 : i + 8])[0] / 1e6
                result["lat"] = lat
                result["lon"] = lon
                i += 8
            if flags & 0x20 and len(app_data) >= i + 2:  # feat1
                i += 2
            if flags & 0x40 and len(app_data) >= i + 2:  # feat2
                i += 2
            if flags & 0x80 and i < len(app_data):  # has name
                name_bytes = app_data[i:]
                try:
                    result["name"] = name_bytes.decode("utf-8").rstrip("\x00")
                except UnicodeDecodeError:
                    result["name"] = name_bytes.hex()

        return result

    def _parse_heartbeat(self, payload):
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

    def _parse_handshake(self, payload):
        if len(payload) < 10:
            return {"error": f"too short ({len(payload)} bytes)"}
        magic = payload[0:9].decode("ascii", errors="replace")
        version = payload[9]
        return {
            "magic": magic,
            "version": version,
            "valid": magic == "COLLECTOR",
        }

    def _process_text(self, buf):
        line = buf.decode("ascii", errors="replace").strip()
        if line:
            self.text_lines.append(line)


def validate_frame(frame):
    """Validate a single frame. Returns (is_valid, issues)."""
    issues = []
    parsed = frame.get("parsed")

    if not parsed:
        issues.append("no parsed data")
        return False, issues

    if "error" in parsed:
        issues.append(f"parse error: {parsed['error']}")
        return False, issues

    ft = frame["type"]

    if ft == FRAME_TYPE_HANDSHAKE:
        if not parsed.get("valid"):
            issues.append(f"invalid magic: {parsed.get('magic')}")
        if parsed.get("version", 0) < 1:
            issues.append(f"unexpected version: {parsed.get('version')}")

    elif ft == FRAME_TYPE_HEARTBEAT:
        if parsed["battery_mv"] == 0:
            issues.append("battery_mv is 0 (may be normal on USB power)")
        if parsed["timestamp"] < 1_000_000_000:
            issues.append(f"timestamp looks unset: {parsed['timestamp']}")

    elif ft == FRAME_TYPE_RX_RAW:
        snr = parsed["snr"]
        if snr < -30 or snr > 30:
            issues.append(f"SNR out of expected range: {snr}")
        if parsed["raw_len"] < 3:
            issues.append(f"raw packet too short: {parsed['raw_len']}")
        if parsed["payload_type"] > 0x0F:
            issues.append(f"invalid payload type: {parsed['payload_type']}")
        if parsed["route_type"] > 3:
            issues.append(f"invalid route type: {parsed['route_type']}")

    elif ft == FRAME_TYPE_TX_RAW:
        if parsed["raw_len"] < 3:
            issues.append(f"raw packet too short: {parsed['raw_len']}")

    elif ft == FRAME_TYPE_ADVERTISEMENT:
        if parsed["timestamp"] < 1_000_000_000:
            issues.append(f"timestamp looks unset: {parsed['timestamp']}")
        if len(parsed.get("pub_key_hex", "")) != 64:
            issues.append(f"pub_key wrong length")

    return len(issues) == 0, issues


def run_test(port, baud, duration):
    print(f"Connecting to {port} at {baud} baud...")
    ser = serial.Serial(port, baud, timeout=1.0)
    time.sleep(0.5)
    ser.reset_input_buffer()

    reader = CollectorReader(ser)

    # --- Step 1: Enable collector mode ---
    print("\n[1] Sending 'collector start'...")
    reader.send_command("collector start")

    handshake = reader.read_until_frame_or_timeout(
        timeout=5.0, frame_type=FRAME_TYPE_HANDSHAKE
    )
    if handshake:
        parsed = handshake["parsed"]
        valid, issues = validate_frame(handshake)
        status = "PASS" if valid else f"FAIL ({', '.join(issues)})"
        print(
            f"    Handshake: magic={parsed.get('magic')}, "
            f"version={parsed.get('version')} [{status}]"
        )
    else:
        print("    WARNING: No handshake frame received")
        print("    (firmware may not have collector support)")

    ok_found = any("OK" in line for line in reader.text_lines)
    print(f"    CLI response: {'OK' if ok_found else 'no OK seen'}")

    # --- Step 2: Request heartbeat ---
    print("\n[2] Requesting heartbeat via 'collector status'...")
    reader.send_command("collector status")

    heartbeat = reader.read_until_frame_or_timeout(
        timeout=5.0, frame_type=FRAME_TYPE_HEARTBEAT
    )
    if heartbeat:
        p = heartbeat["parsed"]
        valid, issues = validate_frame(heartbeat)
        status = "PASS" if valid else f"WARN ({', '.join(issues)})"
        if p["timestamp"] > 1_000_000_000:
            ts = datetime.fromtimestamp(p["timestamp"], tz=timezone.utc).isoformat()
        else:
            ts = f"{p['timestamp']} (clock not set)"
        print(f"    Heartbeat [{status}]:")
        print(f"      timestamp:  {ts}")
        print(f"      battery:    {p['battery_mv']} mV")
        print(f"      rx (F/D):   {p['rx_flood']}/{p['rx_direct']}")
        print(f"      tx (F/D):   {p['tx_flood']}/{p['tx_direct']}")
        print(f"      free pkts:  {p['free_pkts']}")
        print(f"      uptime:     {p['uptime_secs']}s")
    else:
        print("    WARNING: No heartbeat frame received")

    # --- Step 3: Collect live traffic ---
    print(f"\n[3] Collecting mesh traffic for {duration}s (Ctrl+C to stop early)...")
    try:
        reader.read_all(duration=duration)
    except KeyboardInterrupt:
        print("\n    (interrupted)")

    # --- Step 4: Stop collector ---
    print("\n[4] Sending 'collector stop'...")
    reader.send_command("collector stop")
    reader.read_until_frame_or_timeout(timeout=2.0)

    ser.close()

    # --- Summary ---
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    type_counts = {}
    total_valid = 0
    total_invalid = 0
    invalid_details = []

    for frame in reader.frames:
        name = frame["type_name"]
        type_counts[name] = type_counts.get(name, 0) + 1
        valid, issues = validate_frame(frame)
        if valid:
            total_valid += 1
        else:
            total_invalid += 1
            if issues:
                invalid_details.append(f"  {name}: {', '.join(issues)}")

    print(f"\nFrames received: {len(reader.frames)}")
    for name, count in sorted(type_counts.items()):
        print(f"  {name:20s}: {count}")

    print(f"\nValidation: {total_valid} passed, {total_invalid} failed")
    for detail in invalid_details[:10]:
        print(detail)

    if reader.errors:
        print(f"\nProtocol errors: {len(reader.errors)}")
        for err in reader.errors[:5]:
            print(f"  - {err}")

    # Sample RX packets
    rx_frames = [f for f in reader.frames if f["type"] == FRAME_TYPE_RX_RAW]
    if rx_frames:
        print(f"\nSample RX packets (first 5 of {len(rx_frames)}):")
        for frame in rx_frames[:5]:
            p = frame["parsed"]
            print(
                f"  SNR={p['snr']:+5.1f}  RSSI={p['rssi']:4d}  "
                f"route={p['route_name']:18s}  type={p['payload_name']}"
            )

    # TX packets
    tx_frames = [f for f in reader.frames if f["type"] == FRAME_TYPE_TX_RAW]
    if tx_frames:
        print(f"\nSample TX packets (first 5 of {len(tx_frames)}):")
        for frame in tx_frames[:5]:
            p = frame["parsed"]
            print(
                f"  route={p['route_name']:18s}  type={p['payload_name']}"
            )

    # Advertisements
    adv_frames = [f for f in reader.frames if f["type"] == FRAME_TYPE_ADVERTISEMENT]
    if adv_frames:
        print(f"\nAdvertisements ({len(adv_frames)}):")
        for frame in adv_frames:
            p = frame["parsed"]
            name = p.get("name", "?")
            adv_type = p.get("adv_type_name", "?")
            pk = p.get("pub_key_hex", "")[:16] + "..."
            print(f"  {name:20s}  type={adv_type:10s}  key={pk}  SNR={p.get('snr', 0):+.1f}")

    # Text lines
    if reader.text_lines:
        print(f"\nText output (last 10 of {len(reader.text_lines)}):")
        for line in reader.text_lines[-10:]:
            print(f"  > {line}")

    # Overall result
    has_handshake = any(f["type"] == FRAME_TYPE_HANDSHAKE for f in reader.frames)
    has_heartbeat = any(f["type"] == FRAME_TYPE_HEARTBEAT for f in reader.frames)
    all_ok = total_invalid == 0 and len(reader.errors) == 0

    print(f"\n{'=' * 60}")
    if has_handshake and has_heartbeat and all_ok:
        print("RESULT: PASS - Collector API working correctly")
    elif has_handshake and has_heartbeat:
        print(
            f"RESULT: PARTIAL - API works, {total_invalid} frame(s) had issues"
        )
    else:
        missing = []
        if not has_handshake:
            missing.append("handshake")
        if not has_heartbeat:
            missing.append("heartbeat")
        print(f"RESULT: FAIL - Missing: {', '.join(missing)}")

    return 0 if (has_handshake and has_heartbeat) else 1


def main():
    parser = argparse.ArgumentParser(
        description="MeshCore Collector Repeater - API Validation Test"
    )
    parser.add_argument(
        "--port",
        default="/dev/cu.usbserial-0001",
        help="Serial port (default: /dev/cu.usbserial-0001)",
    )
    parser.add_argument(
        "--baud", type=int, default=115200, help="Baud rate (default: 115200)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Seconds to collect mesh traffic (default: 30)",
    )
    args = parser.parse_args()

    sys.exit(run_test(args.port, args.baud, args.duration))


if __name__ == "__main__":
    main()
