"""
Help Activity — shows keybindings for the current context.

Accessed by pressing '?' from any screen. Displays all available
keyboard shortcuts for the screen that was active.
"""

import os
import sys

sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.ScrollList import ScrollList


HELP_SECTIONS = {
    "dashboard": {
        "title": "Dashboard",
        "keys": [
            ("TAB", "Switch focus between packets and nodes"),
            ("ENTER", "Open detail view for selected item"),
            ("c / C", "Open channel browser"),
            ("d / D", "Open debug log"),
            ("?", "Show this help screen"),
            ("ESC", "Go back to port selection"),
            ("q / Q", "Quit application"),
            ("UP/DOWN", "Scroll through list items"),
        ],
    },
    "channels": {
        "title": "Channel Browser",
        "keys": [
            ("TAB", "Switch focus between channel list and messages"),
            ("ENTER", "Select/deselect channel to filter"),
            ("/", "Open search bar (filter by text or sender)"),
            ("r / R", "Refresh data from database"),
            ("?", "Show this help screen"),
            ("ESC", "Close search / go back to dashboard"),
            ("UP/DOWN", "Scroll through list items"),
        ],
    },
    "port_select": {
        "title": "Port Selection",
        "keys": [
            ("ENTER", "Connect to selected port"),
            ("r / R", "Refresh port list"),
            ("?", "Show this help screen"),
            ("ESC", "Quit"),
            ("UP/DOWN", "Navigate port list"),
        ],
    },
    "packet_detail": {
        "title": "Packet Detail",
        "keys": [
            ("ESC", "Go back to dashboard"),
            ("UP/DOWN", "Scroll through content"),
        ],
    },
    "node_detail": {
        "title": "Node Detail",
        "keys": [
            ("ESC", "Go back to dashboard"),
            ("UP/DOWN", "Scroll through content"),
        ],
    },
    "debug_log": {
        "title": "Debug Log",
        "keys": [
            ("TAB", "Switch focus between frames and text"),
            ("x / X", "Clear all log entries"),
            ("?", "Show this help screen"),
            ("ESC", "Go back to dashboard"),
            ("UP/DOWN", "Scroll through entries"),
        ],
    },
}

# Default help shown when context is not recognized
DEFAULT_HELP = {
    "title": "General",
    "keys": [
        ("ESC", "Go back / quit"),
        ("?", "Show help"),
        ("UP/DOWN", "Scroll"),
    ],
}


def build_help_lines(context):
    """Build help text lines for a given context."""
    section = HELP_SECTIONS.get(context, DEFAULT_HELP)
    lines = []
    lines.append(f"  {section['title']} Keys")
    lines.append(f"  {'=' * (len(section['title']) + 5)}")
    lines.append("")

    max_key_len = max(len(k) for k, _ in section["keys"]) if section["keys"] else 0
    for key, description in section["keys"]:
        lines.append(f"  {key:<{max_key_len + 2}} {description}")

    lines.append("")
    lines.append("  Press ESC to close this help screen.")

    return lines


class HelpActivity(Activity):
    """Shows keybindings for the current context."""

    def __init__(self, context="dashboard"):
        super().__init__()
        self._context = context
        self.tab_order = ["content"]
        self.focus = "content"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self._build_display()

    def _build_display(self):
        lines = build_help_lines(self._context)
        section = HELP_SECTIONS.get(self._context, DEFAULT_HELP)

        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "Help",
                "help": section["title"],
            }),
            "content": ScrollList.display_state(
                self.screen,
                items=lines,
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
                min_height=5,
                flex=1,
            ),
            "bottom": BottomBar.display_state(items={
                "status": "Press ESC to close",
                "help": "",
            }),
        }

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC or event.key == ord("?"):
            self.application.pop_activity()
            return
        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()
