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
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.HorizontalBar import HorizontalBar
from pyos.printers.ScrollList import ScrollList

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
        self.tab_order = ["channel_list", "messages"]
        self.focus = "channel_list"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(ChannelMessage, self, self._on_channel_message)

        self._load_data()
        self._build_display()

    def _load_data(self):
        """Load channel summaries and messages from store."""
        if not self._store:
            return
        self._channels = self._store.get_channel_summary()
        self._total_count = self._store.get_channel_message_count()
        if self._selected_channel:
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
                "status": f"{self._total_count} msgs decoded",
                "help": "TAB:switch  ENTER:select  r:refresh  ESC:back",
            }),
        }

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

    def _update_display(self):
        """Refresh display with current data."""
        self.display_state["top"]["items"]["help"] = (
            f"{len(self._channels)} channel{'s' if len(self._channels) != 1 else ''}"
        )
        self.display_state["channel_list"]["items"] = self._channel_items()
        self.display_state["messages"]["items"] = self._message_items()
        self.display_state["bottom"]["items"]["status"] = f"{self._total_count} msgs decoded"
        self.refresh_screen()

    # --- Event handlers ---

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
            return

        if event.key == Keys.TAB:
            self.cycle_focus()
            self.refresh_screen()
            return

        if event.key == Keys.ENTER and self.focus == "channel_list":
            self._select_channel()
            return

        if event.key == ord("r") or event.key == ord("R"):
            self._load_data()
            self._update_display()
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

        # Add to message list if it matches the filter
        if self._selected_channel is None or self._selected_channel == msg.channel_name:
            self._messages.append({
                "timestamp": msg.raw_timestamp,
                "msg_timestamp": msg.timestamp,
                "sender": msg.sender,
                "text": msg.text,
                "channel_name": msg.channel_name,
            })

        self._update_display()
