"""Serial communication service for BuzzerBoard firmware."""

import threading
import time

from loguru import logger
from pyos.Service import Service


class BuzzerSerialService(Service):
    """Manages serial connection to BuzzerBoard firmware.

    Registered under the name "buzzer_serial" with the Application.
    Activities use application.service("buzzer_serial") to access it.
    """

    def __init__(self, port: str, baudrate: int = 115200):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self._serial = None
        self._lock = threading.Lock()

    def on_start(self):
        """Open serial port and verify firmware responds to PING."""
        import serial

        self._serial = serial.Serial(self.port, self.baudrate, timeout=2)
        time.sleep(1.0)  # allow firmware to boot after USB enumeration

        # Drain any startup banner
        self._serial.reset_input_buffer()

        # Retry PING a few times — firmware may still be booting
        response = ""
        for attempt in range(3):
            response = self.send_command("PING")
            if "+PONG" in response:
                break
            time.sleep(1.0)

        if "+PONG" not in response:
            self._serial.close()
            self._serial = None
            raise ConnectionError(
                f"Firmware did not respond to PING after 3 attempts (got: {response!r}). "
                f"Is the device in bootloader mode?"
            )
        logger.info(f"BuzzerBoard connected on {self.port}")
        self._running_event.set()

    def on_stop(self):
        """Send STOP and close serial port."""
        if self._serial and self._serial.is_open:
            try:
                self.send_command("STOP")
            except Exception:
                pass
            self._serial.close()
        self._serial = None

    def send_command(self, cmd: str) -> str:
        """Send a command and return the response line. Thread-safe."""
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return ""
            self._serial.write((cmd + "\n").encode("ascii"))
            self._serial.flush()
            line = self._serial.readline().decode("ascii", errors="replace").strip()
            return line

    def play_tone(self, freq: int, duration_ms: int = 150):
        """Send TONE command. Fire-and-forget -- no response wait for latency."""
        with self._lock:
            if not self._serial or not self._serial.is_open:
                return
            cmd = f"TONE {freq} {duration_ms}\n"
            self._serial.write(cmd.encode("ascii"))
            self._serial.flush()

    def play_rtttl(self, rtttl_str: str) -> str:
        """Send RTTTL string for melody playback."""
        return self.send_command(f"RTTTL {rtttl_str}")

    def stop_playback(self):
        """Send STOP command."""
        self.send_command("STOP")
