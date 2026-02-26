"""Hardware integration test: scan → connect → play notes over real BLE.

Uses HarnessApplication to drive the actual TUI. BLE scan and connection
share a single asyncio event loop (= single CoreBluetooth CBCentralManager)
so the connect can use the cached device from the scan. After connecting,
the loop is handed to a background thread so play_tone / send_command work
via run_coroutine_threadsafe.

Run with:  python -m pytest tests/test_ble_hardware.py -v -s
Skip with: python -m pytest tests/ --ignore=tests/test_ble_hardware.py
"""

import asyncio
import threading
import time
import pytest
from pyos.testing import MockScreen, HarnessApplication
from pyos import Keys

from buzzerboard_tui.ble_service import BuzzerBLEService
from buzzerboard_tui.instrument import InstrumentActivity

# After 60s the firmware drops to 5s advertising intervals,
# so we need a longer scan window to reliably discover it.
SCAN_TIMEOUT = 30.0


# --- Single shared event loop for scan + connect ---
# CoreBluetooth on macOS creates one CBCentralManager per asyncio event loop.
# BleakClient.connect() only works if the SAME manager discovered the device,
# so we use ONE loop for everything BLE-related.

_ble_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_ble_loop)


async def _scan_and_connect(timeout: float):
    """Scan for BuzzerBoard and connect — all in one event loop."""
    from bleak import BleakScanner, BleakClient

    # Phase 1: scan
    nus_lower = BuzzerBLEService.NUS_SVC.lower()
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for addr, (device, adv_data) in devices.items():
        adv_uuids = [u.lower() for u in (adv_data.service_uuids or [])]
        if nus_lower in adv_uuids:
            name = adv_data.local_name or device.name or "Unknown"
            results.append((device, name))

    if not results:
        return None, None, None

    device, name = results[0]

    # Phase 2: connect
    client = BleakClient(device)
    await client.connect()
    return client, device.address, name


# Run scan+connect at module load time in the shared loop
_client, _address, _name = _ble_loop.run_until_complete(
    _scan_and_connect(SCAN_TIMEOUT)
)

# Skip the entire module if no BLE hardware is reachable
pytestmark = pytest.mark.skipif(
    _client is None,
    reason="No BuzzerBoard found over BLE — hardware not available",
)


@pytest.fixture(scope="module")
def hw_app():
    """Wire connected BLE into TUI with InstrumentActivity."""
    print(f"\nConnected to {_name} ({_address})")

    # Build the service around the already-connected client + loop
    svc = BuzzerBLEService(_address)
    svc._client = _client
    svc._loop = _ble_loop

    # Hand the loop to a background thread for ongoing operations
    svc._loop_thread = threading.Thread(target=svc._run_loop, daemon=True)
    svc._loop_thread.start()

    # Subscribe to NUS notifications
    future = asyncio.run_coroutine_threadsafe(
        svc._client.start_notify(svc.NUS_RX, svc._on_nus_rx), svc._loop
    )
    future.result(timeout=5.0)

    # Verify PING
    response = svc.send_command("PING")
    assert "+PONG" in response, f"PING failed: {response!r}"
    print("PING OK")

    svc._running_event.set()

    # --- Wire into TUI ---
    screen = MockScreen(24, 80)
    app = HarnessApplication(screen)
    app.setup()
    app.register_service("buzzer_serial", svc)
    app.start_activity(InstrumentActivity())
    app.drain()

    assert isinstance(app.current_activity(), InstrumentActivity)
    print("InstrumentActivity is live — ready to play")

    yield app

    # Teardown
    svc.on_stop()
    app.teardown()
    print(f"\nDisconnected from {_name}")


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
        hw_app.send_key(Keys.FORWARD_SLASH)  # start recording
        hw_app.send_key(ord("a"))  # C5
        time.sleep(0.2)
        hw_app.send_key(ord("d"))  # E5
        time.sleep(0.2)
        hw_app.send_key(ord("g"))  # G5
        time.sleep(0.2)
        hw_app.send_key(Keys.FORWARD_SLASH)  # stop recording

        hw_app.assert_text_on_screen("3 notes")

        hw_app.send_key(ord("."))  # playback
        time.sleep(2.0)  # let RTTTL melody play
