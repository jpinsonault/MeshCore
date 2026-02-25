"""
Channel Browser Activity — browse decoded group channel messages.

Split-view layout: channel list on top, messages below.
Subscribes to ChannelMessage events for live updates.
"""

import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange, TextBoxChange, TextBoxSubmit
from pyos.input_handlers import handle_scroll_list_input, handle_text_box_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.HorizontalBar import HorizontalBar
from pyos.printers.ScrollList import ScrollList
from pyos.printers.TextInput import TextInput

from ..events import ChannelMessage


def _fmt_time(ts):
    """Format a unix timestamp as HH:MM:SS."""
    if not ts:
        return "---"
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OSError, ValueError):
        return "---"


class ChannelBrowserActivity(Activity):
    """Browse decoded group channel messages."""

    def __init__(self, store):
        super().__init__()
        self._store = store
        self._channels = []     # channel summary dicts
        self._messages = []     # message dicts for selected channel
        self._selected_channel = None  # channel_name or None for all
        self._total_count = 0
        self._search_active = False
        self._search_text = ""
        self.tab_order = ["channel_list", "messages"]
        self.focus = "channel_list"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(TextBoxChange, self, self._on_search_change)
        self.application.subscribe(TextBoxSubmit, self, self._on_search_submit)
        self.application.subscribe(ChannelMessage, self, self._on_channel_message)

        self._load_data()
        self._build_display()

    def _load_data(self):
        """Load channel summaries and messages from store."""
        if not self._store:
            return
        self._channels = self._store.get_channel_summary()
        self._total_count = self._store.get_channel_message_count()
        if self._search_text:
            self._messages = self._store.search_channel_messages(
                channel_name=self._selected_channel,
                search_text=self._search_text,
                limit=200,
            )
        elif self._selected_channel:
            self._messages = self._store.get_channel_messages(
                channel_name=self._selected_channel, limit=200
            )
        else:
            self._messages = self._store.get_channel_messages(limit=200)

    def _build_display(self):
        """Build the display_state layout."""
        channel_items = self._channel_items()
        message_items = self._message_items()
        channel_count = len(self._channels)

        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "Channel Browser",
                "help": f"{channel_count} channel{'s' if channel_count != 1 else ''}",
            }),
            "channel_list": ScrollList.display_state(
                self.screen,
                items=channel_items,
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
                min_height=3,
                flex=1,
            ),
            "search_input": TextInput.display_state(
                label="/",
                text="",
                focused=False,
                input_handler=handle_text_box_input,
            ),
            "hr": HorizontalBar.display_state(),
            "messages": ScrollList.display_state(
                self.screen,
                items=message_items,
                selected_index=max(0, len(message_items) - 1),
                focused=False,
                input_handler=handle_scroll_list_input,
                min_height=5,
                flex=3,
            ),
            "bottom": BottomBar.display_state(items={
                "status": self._status_text(),
                "help": "TAB:switch  ENTER:select  /:search  r:refresh  ESC:back",
            }),
        }
        if not self._search_active:
            self.display_state["search_input"]["hidden"] = True

    def _channel_items(self):
        """Format channel summary for the list."""
        if not self._channels:
            return ["(no channels decoded yet)"]
        items = []
        for ch in self._channels:
            name = ch["channel_name"]
            count = ch["msg_count"]
            senders = ch["unique_senders"]
            last = _fmt_time(ch.get("last_activity"))
            marker = " >" if name == self._selected_channel else "  "
            items.append(
                f"{marker} {name:20s} {count:4d} msgs  last: {last}  {senders} sender{'s' if senders != 1 else ''}"
            )
        return items

    def _message_items(self):
        """Format messages for the list."""
        if not self._messages:
            if self._selected_channel:
                return [f"(no messages in {self._selected_channel})"]
            return ["(no messages decoded yet)"]
        items = []
        for m in self._messages:
            ts = _fmt_time(m.get("timestamp"))
            sender = m.get("sender", "?")
            text = m.get("text", "")
            items.append(f"  {ts}  {sender}: {text}")
        return items

    def _status_text(self):
        """Build the status bar text."""
        if self._search_text:
            count = len(self._messages)
            return f"{count} match{'es' if count != 1 else ''} | {self._total_count} total"
        return f"{self._total_count} msgs decoded"

    def _update_display(self):
        """Refresh display with current data."""
        self.display_state["top"]["items"]["help"] = (
            f"{len(self._channels)} channel{'s' if len(self._channels) != 1 else ''}"
        )
        self.display_state["channel_list"]["items"] = self._channel_items()
        self.display_state["messages"]["items"] = self._message_items()
        self.display_state["bottom"]["items"]["status"] = self._status_text()
        self.display_state["search_input"]["hidden"] = not self._search_active
        self.refresh_screen()

    # --- Event handlers ---

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            if self._search_active:
                self._close_search()
                return
            self.application.pop_activity()
            return

        if event.key == ord("/") and self.focus != "search_input":
            self._open_search()
            return

        if event.key == Keys.TAB:
            self.cycle_focus()
            self.refresh_screen()
            return

        if event.key == Keys.ENTER and self.focus == "channel_list":
            self._select_channel()
            return

        if event.key == ord("r") or event.key == ord("R"):
            if self.focus != "search_input":
                self._load_data()
                self._update_display()
                return

        if event.key == ord("?"):
            if self.focus != "search_input":
                from .help_overlay import HelpActivity
                self.application.segue_to(HelpActivity(context="channels"))
                return

        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()

    def _select_channel(self):
        """Select a channel from the list to filter messages."""
        if not self._channels:
            return
        idx = self.display_state["channel_list"]["selected_index"]
        if idx >= len(self._channels):
            return

        ch_name = self._channels[idx]["channel_name"]
        if self._selected_channel == ch_name:
            # Deselect (show all)
            self._selected_channel = None
        else:
            self._selected_channel = ch_name

        self._load_data()
        # Scroll messages to bottom (newest)
        msg_items = self._message_items()
        self.display_state["messages"]["selected_index"] = max(0, len(msg_items) - 1)
        self._update_display()

    def _open_search(self):
        """Activate the search bar."""
        self._search_active = True
        self.display_state["search_input"]["hidden"] = False
        self.display_state["search_input"]["text"] = ""
        self.display_state["search_input"]["cursor_index"] = 0
        if "search_input" not in self.tab_order:
            self.tab_order = ["channel_list", "search_input", "messages"]
        self.focus = "search_input"
        self._update_display()

    def _close_search(self):
        """Deactivate the search bar and clear filter."""
        self._search_active = False
        self._search_text = ""
        self.display_state["search_input"]["hidden"] = True
        self.display_state["search_input"]["text"] = ""
        self.tab_order = ["channel_list", "messages"]
        self.focus = "channel_list"
        self._load_data()
        self._update_display()

    def _on_search_change(self, event):
        """Handle live search as user types."""
        if not self._search_active:
            return
        self._search_text = self.display_state["search_input"]["text"]
        self._load_data()
        # Scroll messages to bottom
        msg_items = self._message_items()
        self.display_state["messages"]["selected_index"] = max(0, len(msg_items) - 1)
        self._update_display()

    def _on_search_submit(self, event):
        """Handle ENTER in search bar — lock search and move focus to messages."""
        if not self._search_active:
            return
        self._search_text = self.display_state["search_input"]["text"]
        self.focus = "messages"
        self.refresh_screen()

    def _on_channel_message(self, event):
        """Handle live channel message events."""
        self._total_count += 1
        msg = event.msg

        # Update channel summary in-memory
        found = False
        for ch in self._channels:
            if ch["channel_name"] == msg.channel_name:
                ch["msg_count"] += 1
                ch["last_activity"] = msg.raw_timestamp
                found = True
                break
        if not found:
            self._channels.append({
                "channel_name": msg.channel_name,
                "channel_hash": msg.channel_hash,
                "msg_count": 1,
                "last_activity": msg.raw_timestamp,
                "unique_senders": 1,
            })

        # Add to message list if it matches the active filters
        channel_match = self._selected_channel is None or self._selected_channel == msg.channel_name
        search_match = True
        if self._search_text:
            q = self._search_text.lower()
            search_match = (
                (msg.text and q in msg.text.lower()) or
                (msg.sender and q in msg.sender.lower())
            )
        if channel_match and search_match:
            self._messages.append({
                "timestamp": msg.raw_timestamp,
                "msg_timestamp": msg.timestamp,
                "sender": msg.sender,
                "text": msg.text,
                "channel_name": msg.channel_name,
            })

        self._update_display()
