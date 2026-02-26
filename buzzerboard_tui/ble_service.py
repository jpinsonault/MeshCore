"""BLE communication service for BuzzerBoard firmware via Nordic UART Service."""

import atexit
import asyncio
import queue
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
        self._rx_queue = queue.Queue()
        self._rx_buf = ""  # reassembly buffer for fragmented BLE notifications
        self._lock = threading.Lock()

    def on_start(self):
        """Start asyncio loop in background thread, connect, verify PING."""
        import time as _time
        import bleak  # noqa: F401 — verify import at start

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True
        )
        self._loop_thread.start()

        # Connect and verify
        t0 = _time.monotonic()
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        future.result(timeout=45.0)
        logger.info(f"BLE connect took {_time.monotonic() - t0:.2f}s")

        # Verify firmware responds
        response = ""
        for attempt in range(3):
            t1 = _time.monotonic()
            response = self.send_command("PING")
            logger.info(f"PING attempt {attempt+1}: {response!r} ({_time.monotonic() - t1:.2f}s)")
            if "+PONG" in response:
                break
            _time.sleep(0.5)

        if "+PONG" not in response:
            self._disconnect_sync()
            raise ConnectionError(
                f"BLE firmware did not respond to PING (got: {response!r})"
            )
        logger.info(f"BuzzerBoard connected via BLE to {self.address} (total {_time.monotonic() - t0:.2f}s)")
        atexit.register(self._atexit_disconnect)
        self._running_event.set()

    def on_stop(self):
        """Disconnect and stop the asyncio loop."""
        self._disconnect_sync()

    def send_command(self, cmd: str) -> str:
        """Send a command and wait for the response line. Thread-safe."""
        with self._lock:
            if not self._client or not self._loop:
                return ""
            self._drain_queue()
            data = (cmd + "\n").encode("ascii")
            future = asyncio.run_coroutine_threadsafe(
                self._client.write_gatt_char(self.NUS_TX, data), self._loop
            )
            try:
                future.result(timeout=3.0)
            except Exception:
                return ""
            try:
                return self._rx_queue.get(timeout=3.0)
            except queue.Empty:
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

    def tone_start(self, freq: int):
        """Fire-and-forget TONE_START command over BLE -- plays until STOP."""
        with self._lock:
            if not self._client or not self._loop:
                return
            data = f"TONE_START {freq}\n".encode("ascii")
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

    def fetch_log(self) -> list[str]:
        """Fetch timestamped command log from firmware. Returns list of log lines."""
        with self._lock:
            if not self._client or not self._loop:
                return []
            self._drain_queue()
            data = b"LOG\n"
            future = asyncio.run_coroutine_threadsafe(
                self._client.write_gatt_char(self.NUS_TX, data), self._loop
            )
            try:
                future.result(timeout=3.0)
            except Exception:
                return []
            lines = []
            import time
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    line = self._rx_queue.get(timeout=0.5)
                except queue.Empty:
                    break
                if line == "+LOG END":
                    break
                if line.startswith("+LOG "):
                    lines.append(line[5:])
            return lines

    def _run_loop(self):
        """Run the asyncio event loop in a background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(self, max_attempts: int = 3):
        """Connect to the BLE device and subscribe to NUS RX notifications.

        On macOS, CoreBluetooth gives each asyncio event loop its own
        CBCentralManager.  BleakClient.connect() only works when the
        *same* manager discovered the device, so we scan in THIS loop
        first to populate the cache, then hand the BLEDevice object
        (which carries the manager reference) to BleakClient.

        Retries happen here (same event loop) so the CBCentralManager
        cache is preserved across attempts.
        """
        from bleak import BleakClient, BleakScanner

        last_error = None
        for attempt in range(1, max_attempts + 1):
            logger.info(f"BLE connect attempt {attempt}/{max_attempts}")
            try:
                device = await BleakScanner.find_device_by_address(
                    self.address, timeout=10.0
                )
                if device is None:
                    raise ConnectionError(
                        f"BLE device {self.address} not found during pre-connect scan"
                    )

                self._client = BleakClient(device)
                await self._client.connect()
                await self._client.start_notify(self.NUS_RX, self._on_nus_rx)
                return  # success
            except Exception as e:
                last_error = e
                logger.warning(f"BLE connect attempt {attempt}/{max_attempts} failed: {e}")
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
                if attempt < max_attempts:
                    await asyncio.sleep(1.0)

        raise last_error

    def _on_nus_rx(self, sender, data: bytearray):
        """Callback for NUS RX notifications.

        BLE UART fragments lines across multiple notifications (~20 byte MTU
        chunks).  Reassemble into complete lines before enqueuing.
        """
        self._rx_buf += data.decode("ascii", errors="replace")
        while "\n" in self._rx_buf:
            line, self._rx_buf = self._rx_buf.split("\n", 1)
            line = line.strip()
            if line:
                self._rx_queue.put(line)

    def _drain_queue(self):
        """Discard stale notifications (e.g. unread +OK from fire-and-forget writes)."""
        while not self._rx_queue.empty():
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break

    def _atexit_disconnect(self):
        """Last-resort cleanup if on_stop() was never called."""
        if self._client:
            logger.info("atexit: forcing BLE disconnect")
            self._disconnect_sync()

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

        # Don't pass service_uuids to CoreBluetooth — its filtering is
        # unreliable on macOS and silently drops matching devices.
        # Instead, scan everything and filter ourselves.
        devices = await BleakScanner.discover(
            timeout=timeout,
            return_adv=True,
        )
        results = []
        nus_lower = BuzzerBLEService.NUS_SVC.lower()
        for addr, (device, adv_data) in devices.items():
            adv_uuids = [u.lower() for u in (adv_data.service_uuids or [])]
            if nus_lower not in adv_uuids:
                continue
            name = adv_data.local_name or device.name or "Unknown"
            results.append((device.address, name))
        return results

    @staticmethod
    def scan_sync(timeout: float = 5.0) -> list:
        """Blocking wrapper around scan() for use from sync code."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(BuzzerBLEService.scan(timeout))
        finally:
            loop.close()
