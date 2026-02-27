"""Hardware system test: BLE scan → connect → play → log dump.

Tests the REAL BuzzerBLEService._connect() code path that was failing
with "not found during pre-connect scan".  The bug: port picker scans
in one event loop, then on_start() creates a NEW event loop whose
CoreBluetooth manager never saw the device.  The fix: try direct
BleakClient(address).connect() first (uses OS-level cache), then
fall back to scanning.

This test reproduces the exact failing scenario:
  1. Scan for the device with BuzzerBLEService.scan_sync() — same as port picker
  2. Create BuzzerBLEService(address) and call on_start() — creates a NEW
     event loop and runs _connect(), the code that was broken
  3. Wire the connected service into the TUI
  4. Press keys to play notes, press 'l' to dump log

Run with:  python -m pytest tests/test_ble_hardware.py -v -s
Headful:   python -m pytest tests/test_ble_hardware.py -v -s --headful
Skip with: python -m pytest tests/ --ignore=tests/test_ble_hardware.py
"""

import os
import time
import pytest
from pyos.testing import MockScreen, HarnessApplication
from pyos import Keys

from buzzerboard_tui.ble_service import BuzzerBLEService
from buzzerboard_tui.instrument import InstrumentActivity


# ---------------------------------------------------------------------------
# Phase 1: Scan for a BuzzerBoard — same code path as the port picker
# ---------------------------------------------------------------------------

def _discover_buzzerboard():
    """Scan for a BuzzerBoard over BLE. Returns (address, name) or (None, None)."""
    cached = os.environ.get("BUZZER_BLE", "")
    if cached:
        print(f"\n  Using cached BLE address: {cached}")
        return cached, "BuzzerBoard"

    print("\n  BLE scanning (30s)...")
    results = BuzzerBLEService.scan_sync(timeout=30.0)
    print(f"  Found {len(results)} NUS devices")
    for addr, name in results:
        print(f"    {name} ({addr})")
    if results:
        return results[0]
    return None, None


_address, _name = _discover_buzzerboard()

pytestmark = pytest.mark.skipif(
    _address is None,
    reason="No BuzzerBoard found over BLE — hardware not available",
)


# ---------------------------------------------------------------------------
# Phase 2: Connect using on_start() — the code path that was failing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ble_service():
    """Connect to real BLE hardware through BuzzerBLEService.on_start().

    This is the EXACT code path that was failing: scan happens in one
    event loop (scan_sync above), then on_start() creates a DIFFERENT
    event loop and calls _connect(). The fix makes _connect() try
    direct BleakClient(address).connect() first.
    """
    print(f"\n  Connecting to {_name} ({_address}) via on_start()...")
    svc = BuzzerBLEService(_address)
    svc.on_start()  # <-- THIS is what was failing before the fix
    print(f"  Connected!")
    yield svc
    svc.on_stop()
    print(f"\n  Disconnected from {_name}")


@pytest.fixture(scope="module")
def hw_app(ble_service):
    """Wire the real BLE service into the TUI."""
    screen = MockScreen(24, 80)
    app = HarnessApplication(screen)
    app.setup()
    app.register_service("buzzer_serial", ble_service)
    app.start_activity(InstrumentActivity())
    app.drain()

    assert isinstance(app.current_activity(), InstrumentActivity)
    print("  InstrumentActivity is live — ready to play")

    yield app
    app.teardown()


# ===========================================================================
# Tests
# ===========================================================================


class TestBLEConnect:
    """Verify the fixed _connect() actually works end-to-end."""

    def test_service_connected(self, ble_service):
        """on_start() should have connected and verified PING."""
        assert ble_service._client is not None
        assert ble_service._loop is not None
        assert ble_service._loop.is_running()

    def test_ping(self, ble_service):
        """Send PING and verify PONG — confirms the BLE link is live."""
        response = ble_service.send_command("PING")
        assert "+PONG" in response, f"PING failed: {response!r}"


class TestBLETUI:
    def test_instrument_screen_rendered(self, hw_app):
        """InstrumentActivity should show the piano UI."""
        hw_app.assert_text_on_screen("BuzzerBoard")
        hw_app.assert_text_on_screen("Oct: 5")

    def test_play_c_major_scale(self, hw_app):
        """Press keys a-s-d-f-g-h-j-k to play C major scale. Listen!"""
        notes = ["C5", "D5", "E5", "F5", "G5", "A5", "B5", "C6"]
        keys = [ord("a"), ord("s"), ord("d"), ord("f"),
                ord("g"), ord("h"), ord("j"), ord("k")]
        for key, expected_note in zip(keys, notes):
            hw_app.send_key(key)
            hw_app.assert_text_on_screen(expected_note)
            time.sleep(0.2)
        time.sleep(0.3)

    def test_play_sharps(self, hw_app):
        """Press w-e-t-y-u to play the black keys."""
        for key in [ord("w"), ord("e"), ord("t"), ord("y"), ord("u")]:
            hw_app.send_key(key)
            time.sleep(0.2)
        time.sleep(0.3)

    def test_octave_shift_and_play(self, hw_app):
        """Shift octave up, play a note, shift back."""
        hw_app.send_key(Keys.RIGHT_BRACKET)  # octave 6
        hw_app.send_key(ord("a"))  # C6
        time.sleep(0.3)

        hw_app.assert_text_on_screen("Oct: 6")

        hw_app.send_key(Keys.LEFT_BRACKET)  # back to 5
        time.sleep(0.1)

    def test_record_and_playback(self, hw_app):
        """Record C-E-G, play it back as RTTTL — should hear the melody twice."""
        # Let any held-key grace period from previous test expire
        time.sleep(1.0)
        hw_app.send_key(Keys.FORWARD_SLASH)  # start recording
        time.sleep(0.3)
        hw_app.send_key(ord("a"))  # C5
        time.sleep(0.5)
        hw_app.send_key(ord("d"))  # E5
        time.sleep(0.5)
        hw_app.send_key(ord("g"))  # G5
        time.sleep(0.5)
        hw_app.send_key(Keys.FORWARD_SLASH)  # stop recording

        hw_app.assert_text_on_screen("3 notes")

        hw_app.send_key(ord("."))  # playback
        time.sleep(2.0)  # let RTTTL melody play

    def test_log_dump_via_tui(self, hw_app):
        """Press 'l' to dump firmware log — previous tests generated entries."""
        hw_app.send_key(ord("l"))

        # _dump_log spawns a background thread that calls fetch_log over BLE,
        # then submits _on_log_done to the main thread. Give BLE time.
        time.sleep(5.0)

        # The callback should have updated the screen by now
        hw_app.assert_text_on_screen("entries")
