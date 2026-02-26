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

        self.tab_order = ["serial_ports", "ble_devices"]
        self._set_focus("serial_ports")

        self.display_state = {
            "top": TopBar.display_state(
                items={"title": "BuzzerBoard", "sub": "Select Connection"}
            ),
            "serial_label": {
                "layout": {"height": 1},
                "line_generator": lambda ctx, h: [partial(print_line, 2, "Serial Ports:")],
            },
            "serial_ports": ScrollList.display_state(
                screen=self.screen,
                items=self._format_ports(),
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
            ),
            "ble_label": {
                "layout": {"height": 1},
                "line_generator": lambda ctx, h: [partial(print_line, 2, "BLE Devices:")],
            },
            "ble_devices": ScrollList.display_state(
                screen=self.screen,
                items=self._format_ble_devices(),
                selected_index=0,
                focused=False,
                input_handler=handle_scroll_list_input,
            ),
            "bottom": BottomBar.display_state(
                items={
                    "nav": "TAB: switch",
                    "select": "ENTER: connect",
                    "refresh": "r: refresh",
                    "ble": "b: BLE scan",
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

    def _format_ble_devices(self) -> list:
        if self._ble_scanning:
            return ["Scanning..."]
        if not self.ble_devices:
            return ["No BLE devices found. Press 'b' to scan."]
        return [f"{name}  ({addr})" for addr, name in self.ble_devices]

    def _start_ble_scan(self):
        """Run BLE scan in background thread."""
        if self._ble_scanning:
            return
        self._ble_scanning = True
        self.display_state["ble_devices"]["items"] = self._format_ble_devices()
        self.refresh_screen()

        def do_scan():
            try:
                from .ble_service import BuzzerBLEService
                devices = BuzzerBLEService.scan_sync(timeout=5.0)
            except Exception:
                devices = []
            self.main_thread.submit_async(self._on_ble_scan_done, devices)

        CentralDispatch.future(do_scan)

    def _on_ble_scan_done(self, devices):
        """Called on main thread when BLE scan completes."""
        self._ble_scanning = False
        self.ble_devices = devices
        self.display_state["ble_devices"]["items"] = self._format_ble_devices()
        self.display_state["ble_devices"]["selected_index"] = 0
        self.refresh_screen()

    def on_key_stroke(self, event: KeyStroke):
        key = event.key

        if key == Keys.ESC:
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

        if key == ord("b"):
            self._start_ble_scan()
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
        from .serial_service import BuzzerSerialService
        from .instrument import InstrumentActivity

        svc = BuzzerSerialService(port)
        self.application.register_service("buzzer_serial", svc)

        self.display_state["bottom"]["items"]["status"] = f"Connecting to {port}..."
        self.refresh_screen()

        def do_connect():
            try:
                svc.on_start()
            except Exception as e:
                self.main_thread.submit_async(self._show_error, str(e))
                return
            self.main_thread.submit_async(
                lambda: self.application.segue_to(InstrumentActivity())
            )

        CentralDispatch.future(do_connect)

    def _connect_ble(self, address: str):
        """Register BLE service and segue to InstrumentActivity."""
        from .ble_service import BuzzerBLEService
        from .instrument import InstrumentActivity

        svc = BuzzerBLEService(address)
        self.application.register_service("buzzer_serial", svc)

        self.display_state["bottom"]["items"]["status"] = f"Connecting BLE {address}..."
        self.refresh_screen()

        def do_connect():
            try:
                svc.on_start()
            except Exception as e:
                self.main_thread.submit_async(self._show_error, str(e))
                return
            self.main_thread.submit_async(
                lambda: self.application.segue_to(InstrumentActivity())
            )

        CentralDispatch.future(do_connect)

    def _show_error(self, message: str):
        self.display_state["bottom"]["items"]["status"] = f"Error: {message}"
        self.refresh_screen()
