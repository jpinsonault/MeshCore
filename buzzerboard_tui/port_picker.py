"""Port picker activity -- serial port and BLE device selection screen."""

from functools import partial

from pyos.Activity import Activity
from pyos import Keys
from pyos.CentralDispatch import CentralDispatch
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.ScrollList import ScrollList
from pyos.printers.printers import print_line


class PortPickerActivity(Activity):
    """Lists serial ports and BLE devices; ENTER connects and segues to InstrumentActivity."""

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll_change)

        self.ports = self._scan_ports()
        self.ble_devices = []  # [(address, name), ...]
        self._ble_scanning = False
        self._ble_scan_active = True  # keep scanning until user connects/exits

        self.tab_order = ["ble_devices", "serial_ports"]
        self._set_focus("ble_devices")

        self.display_state = {
            "top": TopBar.display_state(
                items={"title": "BuzzerBoard", "sub": "Select Connection"}
            ),
            "ble_label": {
                "layout": {"height": 1},
                "line_generator": lambda ctx, h: [
                    partial(print_line, 2, self._ble_label_text())
                ],
            },
            "ble_devices": ScrollList.display_state(
                screen=self.screen,
                items=self._format_ble_devices(),
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
            ),
            "serial_label": {
                "layout": {"height": 1},
                "line_generator": lambda ctx, h: [partial(print_line, 2, "Serial Ports:")],
            },
            "serial_ports": ScrollList.display_state(
                screen=self.screen,
                items=self._format_ports(),
                selected_index=0,
                focused=False,
                input_handler=handle_scroll_list_input,
            ),
            "bottom": BottomBar.display_state(
                items={
                    "nav": "TAB: switch",
                    "select": "ENTER: connect",
                    "refresh": "r: refresh",
                    "quit": "ESC: quit",
                }
            ),
        }

        # Kick off initial BLE scan in background
        self._start_ble_scan()

    def _scan_ports(self) -> list:
        """Return list of (port, description) tuples."""
        try:
            from serial.tools.list_ports import comports

            return [(p.device, p.description) for p in comports()]
        except ImportError:
            return []

    def _format_ports(self) -> list:
        if not self.ports:
            return ["No serial ports found."]
        return [f"{port}  -  {desc}" for port, desc in self.ports]

    def _ble_label_text(self) -> str:
        if self._ble_scanning:
            return "BLE Devices: (scanning...)"
        return "BLE Devices:"

    def _format_ble_devices(self) -> list:
        if not self.ble_devices:
            if self._ble_scanning:
                return ["Scanning..."]
            return ["No BLE devices found."]
        return [f"{name}  ({addr})" for addr, name in self.ble_devices]

    def _start_ble_scan(self):
        """Run BLE scan in background thread."""
        if self._ble_scanning:
            return
        self._ble_scanning = True
        # Update label to show scanning indicator, but keep existing device list
        if not self.ble_devices:
            self.display_state["ble_devices"]["items"] = self._format_ble_devices()
        self.refresh_screen()

        def do_scan():
            try:
                from .ble_service import BuzzerBLEService
                devices = BuzzerBLEService.scan_sync(timeout=15.0)
            except Exception:
                devices = []
            self.main_thread.submit_async(self._on_ble_scan_done, devices)

        CentralDispatch.future(do_scan)

    def _on_ble_scan_done(self, devices):
        """Called on main thread when BLE scan completes."""
        self._ble_scanning = False

        # Preserve selected device across list updates
        selected_addr = None
        if self.ble_devices:
            idx = self.display_state["ble_devices"]["selected_index"]
            if 0 <= idx < len(self.ble_devices):
                selected_addr = self.ble_devices[idx][0]

        # Merge: keep existing devices, add new ones, update names
        known = {addr: name for addr, name in self.ble_devices}
        for addr, name in devices:
            known[addr] = name
        self.ble_devices = [(addr, name) for addr, name in known.items()]

        self.display_state["ble_devices"]["items"] = self._format_ble_devices()

        # Restore selection to same device
        new_idx = 0
        if selected_addr:
            for i, (addr, _) in enumerate(self.ble_devices):
                if addr == selected_addr:
                    new_idx = i
                    break
        self.display_state["ble_devices"]["selected_index"] = new_idx
        self.refresh_screen()

        # Continue scanning if still active
        if self._ble_scan_active:
            self._start_ble_scan()

    def on_key_stroke(self, event: KeyStroke):
        key = event.key

        if key == Keys.ESC:
            self._ble_scan_active = False
            self.application.pop_activity()
            return

        if key == Keys.TAB:
            self.cycle_focus()
            self.refresh_screen()
            return

        if key == ord("r"):
            self.ports = self._scan_ports()
            self.display_state["serial_ports"]["items"] = self._format_ports()
            self.display_state["serial_ports"]["selected_index"] = 0
            self._start_ble_scan()
            self.refresh_screen()
            return

        if key == Keys.ENTER:
            focused = self.focus
            if focused == "serial_ports" and self.ports:
                idx = self.display_state["serial_ports"]["selected_index"]
                if 0 <= idx < len(self.ports):
                    port = self.ports[idx][0]
                    self._connect_serial(port)
                    return
            if focused == "ble_devices" and self.ble_devices:
                idx = self.display_state["ble_devices"]["selected_index"]
                if 0 <= idx < len(self.ble_devices):
                    address = self.ble_devices[idx][0]
                    self._connect_ble(address)
                    return

        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll_change(self, event):
        self.refresh_screen()

    def _connect_serial(self, port: str):
        """Register serial service and segue to InstrumentActivity."""
        self._ble_scan_active = False
        from .serial_service import BuzzerSerialService
        from .instrument import InstrumentActivity

        svc = BuzzerSerialService(port)
        self.application._services.pop("buzzer_serial", None)
        self.application.register_service("buzzer_serial", svc)

        self.display_state["bottom"]["items"]["status"] = f"Connecting to {port}..."
        self.refresh_screen()

        def do_connect():
            try:
                svc.on_start()
            except Exception as e:
                self.main_thread.submit_async(self._on_connect_failed, str(e))
                return
            self.main_thread.submit_async(
                lambda: self.application.segue_to(InstrumentActivity())
            )

        CentralDispatch.future(do_connect)

    def _connect_ble(self, address: str, max_attempts: int = 3):
        """Register BLE service and segue to InstrumentActivity, with retries."""
        self._ble_scan_active = False

        self.display_state["bottom"]["items"]["status"] = "Waiting for scan..."
        self.refresh_screen()

        def do_connect():
            import time as _time
            from .ble_service import BuzzerBLEService
            from .instrument import InstrumentActivity

            # Wait for any in-progress BLE scan to finish —
            # CoreBluetooth can't scan and connect at the same time.
            while self._ble_scanning:
                _time.sleep(0.25)

            for attempt in range(1, max_attempts + 1):
                self.main_thread.submit_async(
                    self._set_status, "Connecting ({}/{})...".format(attempt, max_attempts)
                )
                svc = BuzzerBLEService(address)
                self.application._services.pop("buzzer_serial", None)
                self.application.register_service("buzzer_serial", svc)
                try:
                    svc.on_start()
                    self.main_thread.submit_async(
                        lambda: self.application.segue_to(InstrumentActivity())
                    )
                    return
                except Exception as e:
                    from loguru import logger
                    logger.warning(f"BLE connect attempt {attempt}/{max_attempts} failed: {type(e).__name__}: {e}")
                    try:
                        svc._disconnect_sync()
                    except Exception:
                        pass

            # All attempts failed — show error and resume scanning
            self.main_thread.submit_async(self._on_connect_failed, "Connection failed after {} attempts".format(max_attempts))

        CentralDispatch.future(do_connect)

    def _set_status(self, message: str):
        self.display_state["bottom"]["items"]["status"] = message
        self.refresh_screen()

    def _on_connect_failed(self, message: str):
        """Show error and resume scanning so user can retry."""
        self.display_state["bottom"]["items"]["status"] = f"Error: {message}"
        self._ble_scan_active = True
        self._start_ble_scan()
        self.refresh_screen()

    def _show_error(self, message: str):
        self.display_state["bottom"]["items"]["status"] = f"Error: {message}"
        self.refresh_screen()
