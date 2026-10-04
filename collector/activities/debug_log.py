"""
Debug Log Activity — scrollable log of firmware output and frame events.

Shows raw CLI text from the firmware, connection state changes, and
frame parse events. Subscribes to CollectorText, CollectorFrame,
CollectorConnected, and CollectorDisconnected events.
"""

import os
import sys
import time
from collections import deque
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.HorizontalBar import HorizontalBar
from pyos.printers.ScrollList import ScrollList

from ..events import (
    CollectorConnected,
    CollectorDisconnected,
    CollectorError,
    CollectorFrame,
    CollectorText,
)
from ..protocol import FRAME_TYPE_NAMES


def _ts():
    """Current time as HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")


class DebugLogActivity(Activity):
    """Scrollable debug log showing firmware text and frame events."""

    def __init__(self, max_entries=500):
        super().__init__()
        self._max_entries = max_entries
        self._frame_entries = deque(maxlen=max_entries)
        self._text_entries = deque(maxlen=max_entries)
        self.tab_order = ["frames", "text"]
        self.focus = "frames"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(CollectorFrame, self, self._on_frame)
        self.application.subscribe(CollectorText, self, self._on_text)
        self.application.subscribe(CollectorConnected, self, self._on_connected)
        self.application.subscribe(CollectorDisconnected, self, self._on_disconnected)
        self.application.subscribe(CollectorError, self, self._on_error)

        self._build_display()

    def _build_display(self):
        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "Debug Log",
                "help": "live firmware output",
            }),
            "frames": ScrollList.display_state(
                self.screen,
                items=["(waiting for frames...)"],
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
                min_height=4,
                flex=1,
            ),
            "hr": HorizontalBar.display_state(),
            "text": ScrollList.display_state(
                self.screen,
                items=["(waiting for serial text...)"],
                selected_index=0,
                focused=False,
                input_handler=handle_scroll_list_input,
                min_height=4,
                flex=1,
            ),
            "bottom": BottomBar.display_state(items={
                "status": "0 frames | 0 text",
                "help": "TAB:switch  x:clear  ?:help  ESC:back",
            }),
        }

    def _update_display(self):
        if self._frame_entries:
            self.display_state["frames"]["items"] = list(self._frame_entries)
        else:
            self.display_state["frames"]["items"] = ["(waiting for frames...)"]

        if self._text_entries:
            self.display_state["text"]["items"] = list(self._text_entries)
        else:
            self.display_state["text"]["items"] = ["(waiting for serial text...)"]

        self.display_state["bottom"]["items"]["status"] = (
            f"{len(self._frame_entries)} frames | {len(self._text_entries)} text"
        )
        self.refresh_screen()

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
            return
        if event.key == Keys.TAB:
            self.cycle_focus()
            self.refresh_screen()
            return
        if event.key == ord("x") or event.key == ord("X"):
            self._frame_entries.clear()
            self._text_entries.clear()
            self._update_display()
            return
        if event.key == ord("?"):
            from .help_overlay import HelpActivity
            self.application.segue_to(HelpActivity(context="debug_log"))
            return
        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()

    def _on_frame(self, event):
        frame = event.frame
        ft = frame.get("type", 0)
        ft_name = FRAME_TYPE_NAMES.get(ft, f"0x{ft:02X}")
        parsed = frame.get("parsed", {})
        route = parsed.get("route_name", "")
        ptype = parsed.get("payload_name", "")
        raw_len = parsed.get("raw_len", "?")
        snr = parsed.get("snr")
        snr_str = f" SNR:{snr:+.1f}" if snr is not None else ""

        entry = f" {_ts()} {ft_name:14s} {route:18s} {ptype:10s} {raw_len}b{snr_str}"
        self._frame_entries.append(entry)
        self._update_display()

    def _on_text(self, event):
        entry = f" {_ts()} {event.line}"
        self._text_entries.append(entry)
        self._update_display()

    def _on_connected(self, event):
        entry = f" {_ts()} ** CONNECTED **"
        self._frame_entries.append(entry)
        self._update_display()

    def _on_disconnected(self, event):
        entry = f" {_ts()} ** DISCONNECTED: {event.reason} **"
        self._frame_entries.append(entry)
        self._update_display()

    def _on_error(self, event):
        entry = f" {_ts()} ** ERROR: {event.message} **"
        self._frame_entries.append(entry)
        self._update_display()
