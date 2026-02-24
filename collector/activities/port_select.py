"""
Port Selection Activity — choose which serial device to connect to.

Shows available serial ports with descriptions. Remembers the last choice
and pre-selects it. Highlights the previously-used port if still available.
"""

import curses

import sys
import os
sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.HorizontalBar import HorizontalBar
from pyos.printers.ScrollList import ScrollList
from pyos.printers.MultilineText import MultilineText

from ..core import list_serial_ports
from ..config import load_config, save_config


def _port_label(port_info, saved_port):
    """Format a port entry for the list."""
    device = port_info["device"]
    desc = port_info["description"]
    marker = " *" if device == saved_port else ""
    if desc and desc != "n/a":
        return f"{device}  ({desc}){marker}"
    return f"{device}{marker}"


class PortSelectActivity(Activity):
    """Let the user pick a serial port, then segue to the dashboard."""

    def __init__(self):
        super().__init__()
        self._ports = []
        self._config = {}

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)

        self._config = load_config()
        self._refresh_ports()

    def _refresh_ports(self):
        """Scan serial ports and rebuild the display."""
        self._ports = list_serial_ports()
        saved_port = self._config.get("port")

        if not self._ports:
            items = ["(no serial ports found)"]
            initial_idx = 0
            hint = "No serial devices detected. Plug in your device and press 'r' to refresh."
        else:
            items = [_port_label(p, saved_port) for p in self._ports]
            initial_idx = 0
            for i, p in enumerate(self._ports):
                if p["device"] == saved_port:
                    initial_idx = i
                    break
            hint = "ENTER: connect  |  r: refresh  |  ESC: quit"

        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "MeshCore Collector",
                "help": "Serial Port Selection",
            }),
            "hint": MultilineText.display_state(
                lines=[hint],
                min_height=1,
                max_height=2,
            ),
            "hr": HorizontalBar.display_state(),
            "ports": ScrollList.display_state(
                self.screen,
                items=items,
                selected_index=initial_idx,
                focused=True,
                input_handler=handle_scroll_list_input,
                min_height=3,
                flex=1,
            ),
            "bottom": BottomBar.display_state(items={
                "status": f"{len(self._ports)} port(s) found",
                "saved": f"Last: {saved_port}" if saved_port else "",
            }),
        }
        self.refresh_screen()

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
            return

        if event.key == ord("r") or event.key == ord("R"):
            self._refresh_ports()
            return

        if event.key == Keys.ENTER:
            self._select_port()
            return

        if event.key == ord("?"):
            from .help_overlay import HelpActivity
            self.application.segue_to(HelpActivity(context="port_select"))
            return

        handle_scroll_list_input("ports", self.display_state["ports"], event, self.event_queue)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        idx = self.display_state["ports"]["selected_index"]
        if self._ports and idx < len(self._ports):
            p = self._ports[idx]
            self.display_state["bottom"]["items"]["status"] = f"{p['device']}  hwid: {p['hwid']}"
        self.refresh_screen()

    def _select_port(self):
        if not self._ports:
            return
        idx = self.display_state["ports"]["selected_index"]
        if idx >= len(self._ports):
            return

        selected = self._ports[idx]
        port = selected["device"]

        # Save selection
        self._config["port"] = port
        save_config(self._config)

        # Start the dashboard with this port
        from .dashboard import DashboardActivity
        self.application.segue_to(
            DashboardActivity(port=port, baud=self._config.get("baud", 115200)),
        )
