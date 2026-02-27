"""Auto-connect activity -- scan for BLE devices and connect to the first one found."""

from functools import partial

from pyos.Activity import Activity
from pyos import Keys
from pyos.CentralDispatch import CentralDispatch
from pyos.EventTypes import KeyStroke
from pyos.printers.printers import print_line, print_empty_line


class AutoConnectActivity(Activity):
    """Scans for BLE devices and connects to the first one found."""

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self._status = "Scanning for BLE devices..."
        self._connecting = False

        self.display_state = {
            "body": {
                "layout": {"flex": 1},
                "line_generator": self._render,
            },
        }

        CentralDispatch.future(self._scan)

    def _render(self, context, remaining_height):
        lines = [print_empty_line, partial(print_line, 2, f"  {self._status}")]
        while len(lines) < remaining_height:
            lines.append(print_empty_line)
        return lines[:remaining_height]

    def _scan(self):
        try:
            from .ble_service import BuzzerBLEService
            devices = BuzzerBLEService.scan_sync(timeout=5.0)
        except Exception:
            devices = []

        if devices:
            address, name = devices[0]
            self.main_thread.submit_async(self._connect, address, name)
        else:
            self.main_thread.submit_async(self._fallback_to_picker)

    def _connect(self, address, name):
        self._connecting = True
        self._status = f"Connecting to {name} ({address})..."
        self.refresh_screen()

        def do_connect():
            from .ble_service import BuzzerBLEService
            from .instrument import InstrumentActivity

            svc = BuzzerBLEService(address)
            self.application._services.pop("buzzer_serial", None)
            self.application.register_service("buzzer_serial", svc)
            try:
                svc.on_start()
                self.main_thread.submit_async(
                    lambda: self.application.segue_to(InstrumentActivity())
                )
            except Exception as e:
                try:
                    svc._disconnect_sync()
                except Exception:
                    pass
                self.main_thread.submit_async(self._fallback_to_picker)

        CentralDispatch.future(do_connect)

    def _fallback_to_picker(self):
        from .port_picker import PortPickerActivity
        self.application.segue_to(PortPickerActivity())

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
