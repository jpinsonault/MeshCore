"""Port picker activity -- serial port selection screen."""

from functools import partial

from pyos.Activity import Activity
from pyos import Keys
from pyos.CentralDispatch import CentralDispatch
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.ScrollList import ScrollList


class PortPickerActivity(Activity):
    """Lists serial ports; ENTER connects and segues to InstrumentActivity."""

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll_change)

        self.ports = self._scan_ports()

        self.tab_order = ["ports"]
        self._set_focus("ports")

        self.display_state = {
            "top": TopBar.display_state(
                items={"title": "BuzzerBoard", "sub": "Select Serial Port"}
            ),
            "ports": ScrollList.display_state(
                screen=self.screen,
                items=self._format_ports(),
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
            ),
            "bottom": BottomBar.display_state(
                items={
                    "nav": "UP/DOWN: navigate",
                    "select": "ENTER: connect",
                    "refresh": "r: refresh",
                    "quit": "ESC: quit",
                }
            ),
        }

    def _scan_ports(self) -> list:
        """Return list of (port, description) tuples."""
        try:
            from serial.tools.list_ports import comports

            return [(p.device, p.description) for p in comports()]
        except ImportError:
            return []

    def _format_ports(self) -> list:
        if not self.ports:
            return ["No serial ports found. Press 'r' to refresh."]
        return [f"{port}  -  {desc}" for port, desc in self.ports]

    def on_key_stroke(self, event: KeyStroke):
        key = event.key

        if key == Keys.ESC:
            self.application.pop_activity()
            return

        if key == ord("r"):
            self.ports = self._scan_ports()
            self.display_state["ports"]["items"] = self._format_ports()
            self.display_state["ports"]["selected_index"] = 0
            self.refresh_screen()
            return

        if key == Keys.ENTER and self.ports:
            idx = self.display_state["ports"]["selected_index"]
            if 0 <= idx < len(self.ports):
                port = self.ports[idx][0]
                self._connect(port)
                return

        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll_change(self, event):
        self.refresh_screen()

    def _connect(self, port: str):
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

    def _show_error(self, message: str):
        self.display_state["bottom"]["items"]["status"] = f"Error: {message}"
        self.refresh_screen()
