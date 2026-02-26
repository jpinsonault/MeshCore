"""Hardware system test for BuzzerBoard firmware LOG command.

Connects to real hardware (serial or BLE), sends commands, then verifies
the firmware's diagnostic log captures them correctly.

Diagnoses the "Log empty" bug: BLE notifications arriving faster than
the single-variable polling loop could consume them.

Run:     python -m pytest tests/test_log_hardware.py -v -s
Skip:    python -m pytest tests/ --ignore=tests/test_log_hardware.py
Serial:  BUZZER_PORT=/dev/cu.usbmodemXXXX python -m pytest tests/test_log_hardware.py -v -s
"""

import os
import time
import pytest

# ---------------------------------------------------------------------------
# Auto-detect connection: serial port from env, else BLE scan
# ---------------------------------------------------------------------------

SERIAL_PORT = os.environ.get("BUZZER_PORT", "")
USE_SERIAL = bool(SERIAL_PORT)


def _find_serial_port():
    """Try to auto-detect a BuzzerBoard serial port."""
    try:
        import serial.tools.list_ports
        for port_info in serial.tools.list_ports.comports():
            desc = (port_info.description or "").lower()
            hwid = (port_info.hwid or "").lower()
            # T1000-E shows up as "T1000-E-BOOT" with Adafruit VID 239A
            if any(kw in desc for kw in ("t1000", "nrf", "buzzer", "nordic")) \
               or "239a" in hwid:
                return port_info.device
    except ImportError:
        pass
    return ""


if not SERIAL_PORT:
    SERIAL_PORT = _find_serial_port()
    USE_SERIAL = bool(SERIAL_PORT)


# ---------------------------------------------------------------------------
# Serial fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def serial_conn():
    """Raw serial connection to BuzzerBoard. Skips if not available."""
    if not USE_SERIAL:
        pytest.skip("No serial port — set BUZZER_PORT or connect via USB")

    import serial as pyserial
    ser = pyserial.Serial(SERIAL_PORT, 115200, timeout=2)
    time.sleep(1.0)
    ser.reset_input_buffer()

    # Verify PING
    ser.write(b"PING\n")
    ser.flush()
    response = ser.readline().decode("ascii", errors="replace").strip()
    if "+PONG" not in response:
        ser.close()
        pytest.skip(f"Device did not respond to PING (got: {response!r})")

    print(f"\nConnected to BuzzerBoard on {SERIAL_PORT}")
    yield ser
    ser.close()
    print(f"\nDisconnected from {SERIAL_PORT}")


@pytest.fixture(scope="module")
def serial_service():
    """BuzzerSerialService connected to real hardware."""
    if not USE_SERIAL:
        pytest.skip("No serial port — set BUZZER_PORT or connect via USB")

    from buzzerboard_tui.serial_service import BuzzerSerialService
    svc = BuzzerSerialService(SERIAL_PORT)
    svc.on_start()
    print(f"\nBuzzerSerialService connected on {SERIAL_PORT}")
    yield svc
    svc.on_stop()


# ---------------------------------------------------------------------------
# BLE fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ble_service():
    """BuzzerBLEService connected to real hardware. Skips if not available."""

    import asyncio
    import threading
    from buzzerboard_tui.ble_service import BuzzerBLEService

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def scan_and_connect():
        from bleak import BleakScanner, BleakClient
        nus_lower = BuzzerBLEService.NUS_SVC.lower()

        # Try cached address first — on macOS, BleakClient can connect
        # to a previously-seen device by UUID without re-scanning.
        cached = os.environ.get("BUZZER_BLE", "")
        if cached:
            print(f"\n  Connecting directly to {cached}...")
            try:
                client = BleakClient(cached, timeout=15.0)
                await client.connect()
                print(f"  Connected via cached address")
                return client, cached, "BuzzerBoard"
            except Exception as e:
                print(f"  Direct connect failed: {e}")

        print("\n  BLE scanning (30s)...")
        devices = await BleakScanner.discover(timeout=30.0, return_adv=True)
        print(f"  Found {len(devices)} BLE devices total")
        for addr, (device, adv_data) in devices.items():
            name = adv_data.local_name or device.name or "?"
            adv_uuids = [u.lower() for u in (adv_data.service_uuids or [])]
            if nus_lower in adv_uuids:
                print(f"  -> {name} ({addr}) [NUS] — connecting...")
                client = BleakClient(device)
                await client.connect()
                return client, device.address, name
        print("  No NUS devices found")
        return None, None, None

    client, address, name = loop.run_until_complete(scan_and_connect())
    if client is None:
        pytest.skip("No BuzzerBoard found over BLE")

    svc = BuzzerBLEService(address)
    svc._client = client
    svc._loop = loop
    svc._loop_thread = threading.Thread(target=svc._run_loop, daemon=True)
    svc._loop_thread.start()

    future = asyncio.run_coroutine_threadsafe(
        client.start_notify(svc.NUS_RX, svc._on_nus_rx), loop
    )
    future.result(timeout=5.0)

    response = svc.send_command("PING")
    assert "+PONG" in response, f"BLE PING failed: {response!r}"
    svc._running_event.set()

    print(f"\nBuzzerBLEService connected to {name} ({address})")
    yield svc
    svc.on_stop()
    print(f"\nDisconnected from {name}")


# ===========================================================================
# 1. Raw serial: verify LOG works at the wire level
# ===========================================================================


class TestRawSerialLog:
    """Bypass the service layer — read raw bytes to see exactly what's on the wire."""

    def test_log_after_ping(self, serial_conn):
        """LOG should contain at least the PING from fixture + this LOG command."""
        ser = serial_conn
        ser.reset_input_buffer()

        ser.write(b"LOG\n")
        ser.flush()

        raw_lines = []
        while True:
            line = ser.readline().decode("ascii", errors="replace").strip()
            raw_lines.append(line)
            print(f"  RAW: {line!r}")
            if not line or line == "+LOG END":
                break

        log_lines = [l for l in raw_lines if l.startswith("+LOG ")]
        print(f"  Got {len(log_lines)} log entries")
        assert len(log_lines) >= 1, f"Expected log entries, got: {raw_lines}"

    def test_log_after_tone_start_and_stop(self, serial_conn):
        """Play a note, stop it, then verify LOG captured both commands."""
        ser = serial_conn
        ser.reset_input_buffer()

        # Send TONE_START (fire-and-forget -- don't read response)
        ser.write(b"TONE_START 440\n")
        ser.flush()
        time.sleep(0.3)

        # Send STOP (read response to clear buffer)
        ser.write(b"STOP\n")
        ser.flush()

        # Read all pending responses (stale +OK from TONE_START + STOP's +OK)
        time.sleep(0.1)
        stale = ser.read(ser.in_waiting).decode("ascii", errors="replace")
        print(f"  Stale data after TONE_START+STOP: {stale!r}")

        # Now fetch LOG
        ser.write(b"LOG\n")
        ser.flush()

        raw_lines = []
        while True:
            line = ser.readline().decode("ascii", errors="replace").strip()
            raw_lines.append(line)
            print(f"  RAW: {line!r}")
            if not line or line == "+LOG END":
                break

        log_lines = [l[5:] for l in raw_lines if l.startswith("+LOG ")]
        print(f"  Log entries: {log_lines}")

        tone_entries = [l for l in log_lines if "TONE_START" in l]
        stop_entries = [l for l in log_lines if l.endswith("STOP")]
        assert len(tone_entries) >= 1, f"Missing TONE_START in log: {log_lines}"
        assert len(stop_entries) >= 1, f"Missing STOP in log: {log_lines}"

    def test_stale_ok_does_not_corrupt_log(self, serial_conn):
        """Fire-and-forget TONE_START leaves +OK in buffer. LOG should still work."""
        ser = serial_conn
        ser.reset_input_buffer()

        # Fire-and-forget: write TONE_START, do NOT read the +OK response
        ser.write(b"TONE_START 523\n")
        ser.flush()
        time.sleep(0.2)

        # Stop it (also fire-and-forget -- leave BOTH +OKs in buffer)
        ser.write(b"STOP\n")
        ser.flush()
        time.sleep(0.1)

        # Do NOT drain buffer -- simulate what the TUI does
        # Now fetch LOG with stale +OKs sitting in the buffer
        ser.write(b"LOG\n")
        ser.flush()

        raw_lines = []
        while True:
            line = ser.readline().decode("ascii", errors="replace").strip()
            raw_lines.append(line)
            print(f"  RAW: {line!r}")
            if not line or line == "+LOG END":
                break

        # Should have consumed stale +OKs and still got log entries
        log_lines = [l[5:] for l in raw_lines if l.startswith("+LOG ")]
        stale_oks = [l for l in raw_lines if l == "+OK"]
        print(f"  Stale +OKs consumed: {len(stale_oks)}")
        print(f"  Log entries: {len(log_lines)}")
        assert len(log_lines) >= 1, f"Log empty despite stale data. Raw: {raw_lines}"


# ===========================================================================
# 2. Serial service layer: verify fetch_log works through the service
# ===========================================================================


class TestSerialServiceLog:
    """Test fetch_log through BuzzerSerialService on real hardware."""

    def test_fetch_log_after_tone(self, serial_service):
        """Play a note via the service, then fetch_log should have entries."""
        svc = serial_service

        svc.tone_start(440)
        time.sleep(0.3)
        svc.stop_playback()
        time.sleep(0.1)

        lines = svc.fetch_log()
        print(f"  fetch_log returned: {lines}")
        assert len(lines) >= 1, f"Expected log entries, got empty list"

        tone_entries = [l for l in lines if "TONE_START" in l]
        assert len(tone_entries) >= 1, f"Missing TONE_START: {lines}"

    def test_fetch_log_multiple_notes(self, serial_service):
        """Play several notes, verify all appear in log."""
        svc = serial_service

        for freq in [262, 294, 330, 349, 392]:
            svc.tone_start(freq)
            time.sleep(0.15)
        svc.stop_playback()
        time.sleep(0.1)

        lines = svc.fetch_log()
        print(f"  fetch_log returned {len(lines)} entries:")
        for line in lines:
            print(f"    {line}")

        tone_entries = [l for l in lines if "TONE_START" in l]
        # Should have at least 5 TONE_START entries
        assert len(tone_entries) >= 5, (
            f"Expected 5 TONE_START entries, got {len(tone_entries)}: {lines}"
        )

    def test_fetch_log_twice_clears(self, serial_service):
        """Second fetch_log should be empty (firmware clears after dump)."""
        svc = serial_service

        svc.tone_start(440)
        time.sleep(0.2)
        svc.stop_playback()
        time.sleep(0.1)

        first = svc.fetch_log()
        print(f"  First fetch: {len(first)} entries")
        assert len(first) >= 1

        second = svc.fetch_log()
        print(f"  Second fetch: {len(second)} entries")
        # Second fetch should only have the LOG command itself
        assert len(second) <= 1, f"Expected empty/minimal, got: {second}"


# ===========================================================================
# 3. BLE service layer: verify fetch_log works over BLE
# ===========================================================================


class TestBLEServiceLog:
    """Test fetch_log through BuzzerBLEService on real hardware."""

    def test_fetch_log_after_tone(self, ble_service):
        """Play a note over BLE, then fetch_log should have entries."""
        svc = ble_service

        svc.tone_start(440)
        time.sleep(0.3)
        svc.stop_playback()
        time.sleep(0.2)

        lines = svc.fetch_log()
        print(f"  BLE fetch_log returned: {lines}")
        assert len(lines) >= 1, f"Expected log entries, got empty list"

        tone_entries = [l for l in lines if "TONE_START" in l]
        assert len(tone_entries) >= 1, f"Missing TONE_START: {lines}"

    def test_fetch_log_rapid_notes(self, ble_service):
        """Play rapid notes over BLE — stress-test the notification queue."""
        svc = ble_service

        for freq in [262, 294, 330, 349, 392, 440, 494, 523]:
            svc.tone_start(freq)
            time.sleep(0.1)
        svc.stop_playback()
        time.sleep(0.2)

        lines = svc.fetch_log()
        print(f"  BLE fetch_log returned {len(lines)} entries:")
        for line in lines:
            print(f"    {line}")

        tone_entries = [l for l in lines if "TONE_START" in l]
        # BLE UART has limited bandwidth — the firmware writes all log entries
        # in a tight loop and the nRF52 BLE buffer may truncate some lines.
        # Expect most entries but allow some loss over BLE.
        assert len(tone_entries) >= 5, (
            f"Expected >= 5 TONE_START entries, got {len(tone_entries)}: {lines}"
        )

    def test_fetch_log_no_lost_entries(self, ble_service):
        """Verify the queue-based fetch_log doesn't drop entries."""
        svc = ble_service

        # Generate exactly 3 entries: TONE_START, STOP, then LOG itself
        svc.tone_start(440)
        time.sleep(0.2)
        svc.stop_playback()
        time.sleep(0.2)

        lines = svc.fetch_log()
        print(f"  BLE fetch_log entries: {lines}")

        # Should have TONE_START, STOP, and LOG (3 entries)
        assert len(lines) >= 3, (
            f"Expected >= 3 entries (TONE_START, STOP, LOG), got {len(lines)}: {lines}"
        )
