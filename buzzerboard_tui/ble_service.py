"""BLE communication service for BuzzerBoard firmware via Nordic UART Service."""

import asyncio
import threading

from loguru import logger
from pyos.Service import Service


class BuzzerBLEService(Service):
    """Manages BLE connection to BuzzerBoard firmware over NUS.

    Registered under the name "buzzer_serial" with the Application
    so InstrumentActivity works unchanged.
    """

    # Nordic UART Service UUIDs
    NUS_SVC = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    NUS_TX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # write (TUI -> device)
    NUS_RX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # notify (device -> TUI)

    def __init__(self, address: str):
        super().__init__()
        self.address = address
        self._client = None
        self._loop = None
        self._loop_thread = None
        self._response_event = threading.Event()
        self._response_line = ""
        self._lock = threading.Lock()

    def on_start(self):
        """Start asyncio loop in background thread, connect, verify PING."""
        import bleak  # noqa: F401 — verify import at start

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True
        )
        self._loop_thread.start()

        # Connect and verify
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        future.result(timeout=15.0)

        # Verify firmware responds
        response = ""
        for attempt in range(3):
            response = self.send_command("PING")
            if "+PONG" in response:
                break
            import time
            time.sleep(0.5)

        if "+PONG" not in response:
            self._disconnect_sync()
            raise ConnectionError(
                f"BLE firmware did not respond to PING (got: {response!r})"
            )
        logger.info(f"BuzzerBoard connected via BLE to {self.address}")
        self._running_event.set()

    def on_stop(self):
        """Disconnect and stop the asyncio loop."""
        self._disconnect_sync()

    def send_command(self, cmd: str) -> str:
        """Send a command and wait for the response line. Thread-safe."""
        with self._lock:
            if not self._client or not self._loop:
                return ""
            self._response_event.clear()
            self._response_line = ""
            data = (cmd + "\n").encode("ascii")
            future = asyncio.run_coroutine_threadsafe(
                self._client.write_gatt_char(self.NUS_TX, data), self._loop
            )
            try:
                future.result(timeout=3.0)
            except Exception:
                return ""
            # Wait for notification response
            if self._response_event.wait(timeout=3.0):
                return self._response_line
            return ""

    def play_tone(self, freq: int, duration_ms: int = 150):
        """Fire-and-forget TONE command over BLE."""
        with self._lock:
            if not self._client or not self._loop:
                return
            data = f"TONE {freq} {duration_ms}\n".encode("ascii")
            asyncio.run_coroutine_threadsafe(
                self._client.write_gatt_char(self.NUS_TX, data, response=False),
                self._loop,
            )

    def play_rtttl(self, rtttl_str: str) -> str:
        """Send RTTTL string for melody playback."""
        return self.send_command(f"RTTTL {rtttl_str}")

    def stop_playback(self):
        """Send STOP command."""
        self.send_command("STOP")

    def _run_loop(self):
        """Run the asyncio event loop in a background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(self):
        """Connect to the BLE device and subscribe to NUS RX notifications."""
        from bleak import BleakClient

        self._client = BleakClient(self.address)
        await self._client.connect()
        await self._client.start_notify(self.NUS_RX, self._on_nus_rx)

    def _on_nus_rx(self, sender, data: bytearray):
        """Callback for NUS RX notifications."""
        line = data.decode("ascii", errors="replace").strip()
        if line:
            self._response_line = line
            self._response_event.set()

    def _disconnect_sync(self):
        """Disconnect and shut down the asyncio loop."""
        if self._client and self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._client.disconnect(), self._loop
            )
            try:
                future.result(timeout=5.0)
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread:
            self._loop_thread.join(timeout=3.0)
        self._client = None
        self._loop = None
        self._loop_thread = None

    @staticmethod
    async def scan(timeout: float = 5.0) -> list:
        """Scan for BLE devices advertising NUS. Returns [(address, name), ...]."""
        from bleak import BleakScanner

        devices = await BleakScanner.discover(
            timeout=timeout,
            service_uuids=[BuzzerBLEService.NUS_SVC],
            return_adv=True,
        )
        results = []
        for addr, (device, adv_data) in devices.items():
            # Use the raw advertised name, not the OS-cached name
            name = adv_data.local_name or device.name or "Unknown"
            results.append((device.address, name))
        return results

    @staticmethod
    def scan_sync(timeout: float = 5.0) -> list:
        """Blocking wrapper around scan() for use from sync code."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(BuzzerBLEService.scan(timeout))
        finally:
            loop.close()
