"""
Chat Activity — IRC-style main screen for the MeshCore Collector.

Side-by-side layout: channel list + status on the left, messages on the right.
Always-visible command input line at the bottom for /commands.
Replaces the old DashboardActivity as the landing screen.
"""

import os
import time
from collections import deque
from datetime import datetime

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange, TextBoxChange, TextBoxSubmit
from pyos.input_handlers import handle_text_box_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.TextInput import TextInput

from ..events import (
    ChannelDiscovered,
    ChannelMessage,
    CollectorConnected,
    CollectorDisconnected,
    CollectorError,
    CollectorFrame,
    UndecryptablePacket,
)
from ..mesh_service import MeshCollectorService
from ..split_view import SplitView
from ..protocol import (
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
)


SPARK_CHARS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
SPARKLINE_BUCKETS = 20
SPARKLINE_WINDOW = 60


def _fmt_time(ts):
    """Format a unix timestamp as HH:MM."""
    if not ts:
        return "---"
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except (OSError, ValueError):
        return "---"


def _fmt_uptime_short(secs):
    """Format seconds as compact XhXm."""
    if secs is None:
        return "---"
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h > 0:
        return f"{h}h{m}m"
    if m > 0:
        return f"{m}m{s}s"
    return f"{s}s"


def _calc_packet_rate(timestamps, now=None, window=10):
    if now is None:
        now = time.time()
    cutoff = now - window
    count = sum(1 for ts in timestamps if ts >= cutoff)
    return count / window


def _make_sparkline(timestamps, now=None, window=SPARKLINE_WINDOW, buckets=SPARKLINE_BUCKETS):
    if now is None:
        now = time.time()
    cutoff = now - window
    bucket_width = window / buckets
    counts = [0] * buckets
    for ts in timestamps:
        if ts < cutoff:
            continue
        idx = int((ts - cutoff) / bucket_width)
        if 0 <= idx < buckets:
            counts[idx] += 1
    max_count = max(counts) if counts else 0
    if max_count == 0:
        return SPARK_CHARS[0] * buckets
    chars = []
    for c in counts:
        level = int(c / max_count * (len(SPARK_CHARS) - 1))
        chars.append(SPARK_CHARS[level])
    return "".join(chars)


# Separator lines for sidebar sections
CHANNELS_SEPARATOR = "\u2500" * 2 + " channels " + "\u2500" * 5
NETWORK_SEPARATOR = "\u2500" * 2 + " network " + "\u2500" * 6
STATUS_SEPARATOR = "\u2500" * 2 + " status " + "\u2500" * 7


class ChatActivity(Activity):
    """IRC-style chat interface — the new main screen."""

    def __init__(self, port, baud=115200, ws_port=None, auto_start=True):
        super().__init__()
        self._port = port
        self._baud = baud
        self._ws_port = ws_port
        self._auto_start = auto_start
        self._server = None
        self._service_started = False

        # Channel state
        self._channels = []         # list of dicts: {name, msg_count}
        self._selected_idx = 0      # index into _channels
        self._selected_channel = None  # channel name or None for "all"

        # Message state
        self._messages = []         # list of message dicts for display
        self._system_messages = deque(maxlen=200)  # capped ring buffer

        # Dashboard stats (woven into sidebar)
        self._node_count = 0
        self._repeater_count = 0
        self._room_count = 0
        self._rx_count = 0
        self._tx_count = 0
        self._adv_count = 0
        self._last_heartbeat = None
        self._packet_times = deque(maxlen=1000)
        self._status = "Connecting..."
        self._connected = False

        # Sidebar navigation targets — parallel array to sidebar items
        # Each entry: None (not selectable), ("channel", name), ("nav", key)
        self._sidebar_nav_targets = []

        # Undecryptable tracking
        self._undecryptable_count = 0

        # Search
        self._search_text = ""

        # Send
        self._sender_name = "collector"

        # Focus model: split and command_input
        self.tab_order = ["split", "command_input"]
        self.focus = "command_input"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(TextBoxChange, self, self._on_text_change)
        self.application.subscribe(TextBoxSubmit, self, self._on_text_submit)
        self.application.subscribe(CollectorConnected, self, self._on_connected)
        self.application.subscribe(CollectorDisconnected, self, self._on_disconnected)
        self.application.subscribe(CollectorFrame, self, self._on_frame)
        self.application.subscribe(CollectorError, self, self._on_error)
        self.application.subscribe(ChannelMessage, self, self._on_channel_message)
        self.application.subscribe(ChannelDiscovered, self, self._on_channel_discovered)
        self.application.subscribe(UndecryptablePacket, self, self._on_undecryptable)

        if self._service_started:
            # Re-entry after segue — reload from store
            self._reload_from_store()
            self._build_display()
            self._update_display()
        else:
            if self._auto_start:
                self._start_collector_service()
                if self._ws_port:
                    self._start_server()
                self._start_cracker()
            self._service_started = True
            self._load_channels_from_config()
            self._load_sender_name()
            self._reload_from_store()
            self._add_system_message(f"Connected to {self._port}")
            self._add_system_message("Type /help for available commands")
            self._build_display()

    def on_stop(self):
        pass

    # --- Service lifecycle ---

    def _start_collector_service(self):
        svc = MeshCollectorService(
            port=self._port,
            baud=self._baud,
            db_path=self._resolve_db_path(),
        )
        if "collector" in self.application._services:
            old = self.application._services["collector"]
            if old.is_running:
                try:
                    old.on_stop()
                except Exception:
                    pass
            self.application._services["collector"] = svc
            svc._application = self.application
        else:
            self.application.register_service("collector", svc)
        self.application.start_service("collector")

    def _start_cracker(self):
        """Auto-start the channel cracker after the collector service launches."""
        try:
            svc = self.application.service("collector")
            core = svc.core
        except (KeyError, RuntimeError):
            return
        from ..cracker import ChannelCracker
        cracker = ChannelCracker(core.store, core._channels)
        # Load cached cracks and add them to core's live channel list
        cached = cracker.load_cache()
        for ch in cached:
            core.add_channel(ch)
            if not any(c["name"] == ch.name for c in self._channels):
                self._channels.append({"name": ch.name, "msg_count": 0})
        # Wire callback to bridge into core's on_channel_discovered
        def _on_discovered(name, decoded_count):
            core.add_channel(Channel.from_hashtag(name))
            if core.on_channel_discovered:
                core.on_channel_discovered(name, decoded_count)
        from ..crypto import Channel
        cracker.on_channel_discovered = _on_discovered
        core.set_cracker(cracker)
        cracker.start()

    def _start_server(self):
        try:
            svc = self.application.service("collector")
            core = svc.core
        except (KeyError, RuntimeError):
            return
        from ..server import CollectorServer
        self._server = CollectorServer(core, ws_port=self._ws_port)
        self._server.start()

    def _resolve_db_path(self):
        from ..config import load_config, DEFAULT_CONFIG_DIR
        config = load_config()
        db_path = config.get("db_path", "collector.db")
        if not os.path.isabs(db_path):
            db_path = str(DEFAULT_CONFIG_DIR / db_path)
        return db_path

    def _load_channels_from_config(self):
        """Load configured channels into the sidebar list."""
        # Always include the default public channel
        from ..crypto import DEFAULT_PUBLIC_CHANNEL_NAME
        if not any(c["name"] == DEFAULT_PUBLIC_CHANNEL_NAME for c in self._channels):
            self._channels.insert(0, {"name": DEFAULT_PUBLIC_CHANNEL_NAME, "msg_count": 0})

        try:
            from ..config import load_config, load_channels
            config = load_config()
            channels = load_channels(config)
            for ch in channels:
                if not any(c["name"] == ch.name for c in self._channels):
                    self._channels.append({"name": ch.name, "msg_count": 0})
        except Exception:
            pass

    def _load_sender_name(self):
        """Load sender display name from config."""
        try:
            from ..config import load_config
            config = load_config()
            self._sender_name = config.get("sender_name", "collector")
        except Exception:
            pass

    def _reload_from_store(self):
        """Rebuild state from SQLite after a segue return."""
        try:
            svc = self.application.service("collector")
            store = svc.store
        except (KeyError, RuntimeError):
            return

        if not store:
            return

        stats = store.get_stats()
        self._rx_count = stats["rx_count"]
        self._tx_count = stats["tx_count"]
        self._adv_count = stats["advert_count"]
        self._last_heartbeat = stats.get("latest_heartbeat")
        self._status = "Connected"
        self._connected = True

        # Node counts by type
        counts = store.get_node_count_by_type()
        self._node_count = sum(counts.values())
        self._repeater_count = counts.get(2, 0)
        self._room_count = counts.get(3, 0)

        # Channel summaries
        summaries = store.get_channel_summary()
        for s in summaries:
            found = False
            for ch in self._channels:
                if ch["name"] == s["channel_name"]:
                    ch["msg_count"] = s["msg_count"]
                    found = True
                    break
            if not found:
                self._channels.append({
                    "name": s["channel_name"],
                    "msg_count": s["msg_count"],
                })

        # Reload messages for selected channel
        self._reload_messages(store)

        # Packet timestamps for sparkline
        ts_list = store.get_recent_packet_timestamps(limit=1000)
        self._packet_times = deque(reversed(ts_list), maxlen=1000)

    def _reload_messages(self, store=None):
        """Reload messages from store for the selected channel."""
        if store is None:
            try:
                svc = self.application.service("collector")
                store = svc.store
            except (KeyError, RuntimeError):
                return

        if not store:
            return

        if self._search_text:
            rows = store.search_channel_messages(
                channel_name=self._selected_channel,
                search_text=self._search_text,
                limit=200,
            )
        elif self._selected_channel:
            rows = store.get_channel_messages(
                channel_name=self._selected_channel, limit=200,
            )
        else:
            rows = store.get_channel_messages(limit=200)

        self._messages = []
        for m in rows:
            self._messages.append({
                "timestamp": m.get("timestamp"),
                "sender": m.get("sender", "?"),
                "text": m.get("text", ""),
                "channel_name": m.get("channel_name", ""),
            })

    # --- Display ---

    def _build_display(self):
        left_items = self._sidebar_items()
        right_items = self._message_items()
        right_title = self._selected_channel or "All"

        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "MeshCore Collector",
                "help": self._port,
            }),
            "split": SplitView.display_state(
                self.screen,
                left_items=left_items,
                right_items=right_items,
                left_title="Rooms",
                right_title=right_title,
                left_selected=self._selected_idx,
                right_selected=max(0, len(right_items) - 1),
                split_ratio=0.25,
                focused=(self.focus == "split"),
                min_height=5,
                flex=1,
            ),
            "command_input": TextInput.display_state(
                label="> ",
                text="",
                focused=(self.focus == "command_input"),
                input_handler=handle_text_box_input,
            ),
            "bottom": BottomBar.display_state(items={
                "status": self._bottom_status(),
                "help": "TAB:rooms  ?:help  /nick  /help",
            }),
        }

    def _sidebar_items(self):
        """Build left panel: channels + network nav + status block."""
        items = []
        targets = []

        # --- channels section ---
        items.append(CHANNELS_SEPARATOR)
        targets.append(None)

        if not self._channels:
            items.append("  (no channels)")
            targets.append(None)
            items.append("  /join #name")
            targets.append(None)
        else:
            for ch in self._channels:
                marker = " >" if ch["name"] == self._selected_channel else "  "
                count = ch["msg_count"]
                items.append(f"{marker} {ch['name']:14s} {count:3d}")
                targets.append(("channel", ch["name"]))

        if self._undecryptable_count > 0:
            items.append(f"   {self._undecryptable_count} encrypted")
            targets.append(None)

        # --- network section ---
        items.append(NETWORK_SEPARATOR)
        targets.append(None)

        items.append(f"   Repeaters ({self._repeater_count})   >")
        targets.append(("nav", "repeaters"))
        items.append(f"   Rooms ({self._room_count})       >")
        targets.append(("nav", "rooms"))
        items.append(f"   All Nodes ({self._node_count})  >")
        targets.append(("nav", "nodes"))

        # --- status section ---
        items.append(STATUS_SEPARATOR)
        targets.append(None)
        for line in self._status_block():
            items.append(line)
            targets.append(None)

        self._sidebar_nav_targets = targets
        return items

    def _status_block(self):
        """Build the status lines for the sidebar."""
        lines = []
        now = time.time()

        parts = [f"{self._node_count} nodes"]
        rate = _calc_packet_rate(self._packet_times, now)
        parts.append(f"{rate:.1f}p/s")
        lines.append(" " + "  ".join(parts))

        parts2 = []
        hb = self._last_heartbeat
        if hb:
            parts2.append(f"Up:{_fmt_uptime_short(hb.get('uptime_secs'))}")
            batt = hb.get("battery_mv")
            if batt:
                parts2.append(f"{batt}mV")
        if parts2:
            lines.append(" " + "  ".join(parts2))

        lines.append(f" {self._rx_count}RX {self._tx_count}TX {self._adv_count}ADV")

        if self._packet_times:
            spark = _make_sparkline(self._packet_times, now)
            lines.append(f" {spark}")

        return lines

    def _message_items(self):
        """Build right panel: system messages + channel messages."""
        items = []
        # System messages first
        for m in self._system_messages:
            items.append(f" * {m['text']}")

        # Channel messages
        for m in self._messages:
            ts = _fmt_time(m.get("timestamp"))
            sender = m.get("sender", "?")
            text = m.get("text", "")
            items.append(f" {ts} {sender}: {text}")

        if not items:
            items.append(" (no messages yet)")
        return items

    def _bottom_status(self):
        """Condensed status for the bottom bar."""
        parts = []
        if self._connected:
            parts.append("Connected")
        else:
            parts.append(self._status)
        parts.append(f"{self._node_count} nodes")
        rate = _calc_packet_rate(self._packet_times)
        parts.append(f"{rate:.1f}p/s")
        hb = self._last_heartbeat
        if hb:
            parts.append(f"Up:{_fmt_uptime_short(hb.get('uptime_secs'))}")
        return "  ".join(parts)

    def _update_display(self):
        """Refresh display with current data."""
        split = self.display_state["split"]

        # Capture scroll state before replacing items
        old_right_len = len(split.get("right_items", []))
        was_at_bottom = split["right_selected"] >= max(0, old_right_len - 1)
        focused_right = (
            self.focus == "split"
            and split.get("focused_panel") == "right"
        )

        split["left_items"] = self._sidebar_items()
        split["right_items"] = self._message_items()
        split["right_title"] = self._selected_channel or "All"
        # Clamp left selection to selectable range
        max_sel = self._max_selectable_sidebar_idx()
        if split["left_selected"] > max_sel:
            split["left_selected"] = max_sel
        # Auto-scroll messages to bottom unless user is browsing
        right_items = split["right_items"]
        if not focused_right or was_at_bottom:
            split["right_selected"] = max(0, len(right_items) - 1)

        self.display_state["bottom"]["items"]["status"] = self._bottom_status()
        self.display_state["split"]["focused"] = (self.focus == "split")
        self.display_state["command_input"]["focused"] = (self.focus == "command_input")
        self.refresh_screen()

    def _max_selectable_sidebar_idx(self):
        """Return the highest sidebar index that has a selectable target."""
        max_idx = 0
        for i, t in enumerate(self._sidebar_nav_targets):
            if t is not None:
                max_idx = i
        return max_idx

    def _snap_to_selectable(self, idx, direction=1):
        """Snap idx to the nearest selectable sidebar item in the given direction."""
        targets = self._sidebar_nav_targets
        if not targets:
            return idx
        if 0 <= idx < len(targets) and targets[idx] is not None:
            return idx
        max_idx = self._max_selectable_sidebar_idx()
        # Search in direction of movement
        i = idx + direction
        while 0 <= i <= max_idx:
            if i < len(targets) and targets[i] is not None:
                return i
            i += direction
        # Reverse if nothing found
        i = idx - direction
        while 0 <= i <= max_idx:
            if i < len(targets) and targets[i] is not None:
                return i
            i -= direction
        return idx

    def _add_system_message(self, text):
        """Add an in-memory system message (shown with * prefix)."""
        self._system_messages.append({
            "text": text,
            "timestamp": time.time(),
        })

    # --- Event handlers ---

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            if self.focus == "command_input":
                self._set_focus("split")
                self.display_state["split"]["focused_panel"] = "left"
                self._update_display()
                return
            self.application.pop_activity()
            return

        if event.key == ord("q") or event.key == ord("Q"):
            if self.focus != "command_input":
                from pyos.EventTypes import StopApplication
                self.event_queue.put(StopApplication())
                return

        if event.key == ord("?"):
            if self.focus != "command_input":
                from .help_overlay import HelpActivity
                self.application.segue_to(HelpActivity(context="chat"))
                return

        if event.key == Keys.TAB:
            self._cycle_focus()
            self._update_display()
            return

        if event.key == Keys.ENTER:
            if self.focus == "split" and self.display_state["split"]["focused_panel"] == "left":
                self._select_channel_from_sidebar()
                return

        # Track sidebar position before delegating for direction detection
        old_left_idx = self.display_state["split"]["left_selected"]
        self.delegate_to_focused(event)
        # Snap left_selected to nearest selectable item
        if self.focus == "split":
            split = self.display_state["split"]
            if split["focused_panel"] == "left" and self._sidebar_nav_targets:
                new_idx = split["left_selected"]
                direction = 1 if new_idx >= old_left_idx else -1
                split["left_selected"] = self._snap_to_selectable(new_idx, direction)
        self.refresh_screen()

    def _cycle_focus(self):
        """TAB cycle: split(left) -> split(right) -> command_input -> split(left)."""
        if self.focus == "split":
            panel = self.display_state["split"]["focused_panel"]
            if panel == "left":
                self.display_state["split"]["focused_panel"] = "right"
            else:
                self._set_focus("command_input")
        elif self.focus == "command_input":
            self._set_focus("split")
            self.display_state["split"]["focused_panel"] = "left"

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()

    def _on_text_change(self, event):
        pass

    def _on_text_submit(self, event):
        text = self.display_state["command_input"]["text"]
        self.display_state["command_input"]["text"] = ""
        self.display_state["command_input"]["cursor_index"] = 0
        if text.strip():
            self._dispatch_command(text)
        self._update_display()

    def _on_connected(self, event):
        self._status = "Connected"
        self._connected = True
        self._update_display()

    def _on_disconnected(self, event):
        self._status = f"Disconnected: {event.reason}"
        self._connected = False
        self._add_system_message(f"Disconnected: {event.reason}")
        self._update_display()

    def _on_error(self, event):
        self._status = f"Error: {event.message}"
        self._add_system_message(f"Error: {event.message}")
        self._update_display()

    def _on_frame(self, event):
        frame = event.frame
        ft = frame["type"]
        parsed = frame.get("parsed", {})
        received_at = frame.get("received_at", time.time())
        self._packet_times.append(received_at)

        if ft == FRAME_TYPE_RX_RAW:
            self._rx_count += 1
        elif ft == FRAME_TYPE_TX_RAW:
            self._tx_count += 1
        elif ft == FRAME_TYPE_ADVERTISEMENT:
            self._adv_count += 1
            pk = parsed.get("pub_key_hex", "")
            if pk:
                self._update_node_counts()
        elif ft == FRAME_TYPE_HEARTBEAT:
            self._last_heartbeat = parsed

        self._update_display()

    def _update_node_counts(self):
        """Refresh node/repeater/room counts from store."""
        try:
            svc = self.application.service("collector")
            store = svc.store
            if store:
                counts = store.get_node_count_by_type()
                self._node_count = sum(counts.values())
                self._repeater_count = counts.get(2, 0)
                self._room_count = counts.get(3, 0)
        except (KeyError, RuntimeError):
            pass

    def _on_channel_message(self, event):
        msg = event.msg
        # Update channel counts
        found = False
        for ch in self._channels:
            if ch["name"] == msg.channel_name:
                ch["msg_count"] += 1
                found = True
                break
        if not found:
            self._channels.append({
                "name": msg.channel_name,
                "msg_count": 1,
            })

        # Add to messages if it matches active filter
        channel_match = (
            self._selected_channel is None or
            self._selected_channel == msg.channel_name
        )
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
                "sender": msg.sender,
                "text": msg.text,
                "channel_name": msg.channel_name,
            })

        self._update_display()

    def _on_undecryptable(self, event):
        self._undecryptable_count = event.count
        self._update_display()

    def _on_channel_discovered(self, event):
        """Cracker found a new channel — add to sidebar and show system message."""
        name = event.channel_name
        count = event.decoded_count
        # Add to sidebar if not present
        found = False
        for ch in self._channels:
            if ch["name"] == name:
                ch["msg_count"] += count
                found = True
                break
        if not found:
            self._channels.append({"name": name, "msg_count": count})

        self._add_system_message(f"Cracked channel {name} ({count} messages decoded)")

        # Reload messages if viewing All or this channel
        if self._selected_channel is None or self._selected_channel == name:
            self._reload_messages()

        self._update_display()

    # --- Channel selection ---

    def _select_channel_from_sidebar(self):
        """ENTER on a sidebar item: select channel, or navigate to node list."""
        idx = self.display_state["split"]["left_selected"]
        if idx >= len(self._sidebar_nav_targets):
            return
        target = self._sidebar_nav_targets[idx]
        if target is None:
            return

        kind, value = target
        if kind == "channel":
            if self._selected_channel == value:
                self._selected_channel = None
            else:
                self._selected_channel = value
            self._selected_idx = idx
            self._reload_messages()
            self._update_display()
        elif kind == "nav":
            self._navigate_to(value)

    def _navigate_to(self, key):
        """Open a sub-screen based on sidebar nav key."""
        from .node_list import NodeListActivity
        if key == "repeaters":
            self.application.segue_to(NodeListActivity(adv_type=2))
        elif key == "rooms":
            self.application.segue_to(NodeListActivity(adv_type=3))
        elif key == "nodes":
            self.application.segue_to(NodeListActivity())

    # --- Command dispatcher ---

    def _dispatch_command(self, text):
        text = text.strip()
        if not text:
            return

        if not text.startswith("/"):
            # Bare text → send to selected channel
            self._send_message(text)
            return

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/join":
            self._cmd_join(arg)
        elif cmd == "/part":
            self._cmd_part(arg)
        elif cmd == "/nick":
            self._cmd_nick(arg)
        elif cmd == "/send":
            self._cmd_send(arg)
        elif cmd == "/search":
            self._cmd_search(arg)
        elif cmd == "/crack":
            self._cmd_crack(arg)
        elif cmd == "/diag":
            self._cmd_segue_diag()
        elif cmd == "/repeaters":
            self._navigate_to("repeaters")
        elif cmd == "/rooms":
            self._navigate_to("rooms")
        elif cmd == "/contacts" or cmd == "/nodes":
            self._navigate_to("nodes")
        elif cmd == "/packets":
            self._cmd_segue_dashboard("left")
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/help":
            self._cmd_help()
        else:
            self._add_system_message(f"Unknown command: {cmd}. Type /help for commands.")

    def _send_message(self, text):
        """Send bare text to the currently selected channel."""
        if not self._connected:
            self._add_system_message("Not connected")
            return
        if not self._selected_channel:
            self._add_system_message("Select a channel first (click sidebar or /join #name)")
            return
        try:
            svc = self.application.service("collector")
            result = svc.send_message(self._selected_channel, self._sender_name, text)
            if not result:
                self._add_system_message("Failed to send (connection lost)")
        except (KeyError, RuntimeError) as e:
            self._add_system_message(f"Send error: {e}")

    def _cmd_nick(self, arg):
        """Set sender display name."""
        if not arg:
            self._add_system_message(f"Current nick: {self._sender_name}")
            self._add_system_message("Usage: /nick <name>")
            return
        name = arg.split()[0]  # first word only
        self._sender_name = name
        from ..config import set_sender_name
        set_sender_name(name)
        self._add_system_message(f"Nick set to: {name}")

    def _cmd_send(self, arg):
        """Explicit send: /send [#channel] message text."""
        if not arg:
            self._add_system_message("Usage: /send [#channel] message")
            return
        parts = arg.split(None, 1)
        if parts[0].startswith("#"):
            channel = parts[0]
            text = parts[1].strip() if len(parts) > 1 else ""
        else:
            channel = self._selected_channel
            text = arg
        if not channel:
            self._add_system_message("No channel specified. Use /send #channel message")
            return
        if not text:
            self._add_system_message("No message text")
            return
        if not self._connected:
            self._add_system_message("Not connected")
            return
        try:
            svc = self.application.service("collector")
            result = svc.send_message(channel, self._sender_name, text)
            if not result:
                self._add_system_message("Failed to send (connection lost)")
        except (KeyError, RuntimeError) as e:
            self._add_system_message(f"Send error: {e}")

    def _cmd_join(self, arg):
        if not arg:
            self._add_system_message("Usage: /join #name  or  /join Name base64psk")
            return

        parts = arg.split()
        from ..crypto import Channel

        if parts[0].startswith("#"):
            # Hashtag channel
            name = parts[0]
            channel = Channel.from_hashtag(name)
            psk_arg = None
        elif len(parts) >= 2:
            # PSK channel: /join RoomName base64psk
            name = parts[0]
            psk_arg = parts[1]
            try:
                channel = Channel.from_psk(name, psk_arg)
            except (ValueError, Exception) as e:
                self._add_system_message(f"Invalid PSK: {e}")
                return
        else:
            # Assume hashtag
            name = f"#{parts[0]}"
            channel = Channel.from_hashtag(name)
            psk_arg = None

        # Check if already joined
        for ch in self._channels:
            if ch["name"] == name:
                self._add_system_message(f"Already joined {name}")
                return

        # Add to core's live channel list
        try:
            svc = self.application.service("collector")
            svc.core.add_channel(channel)
        except (KeyError, RuntimeError):
            pass

        # Save to config
        from ..config import add_channel_to_config
        add_channel_to_config(name, psk=psk_arg)

        # Retroactive decrypt via cracker pipeline
        decoded_count = 0
        try:
            svc = self.application.service("collector")
            cracker = svc.core._cracker
            if cracker:
                decoded_count = cracker.retroactive_decrypt(channel)
        except (KeyError, RuntimeError):
            pass

        # Add to local list
        self._channels.append({"name": name, "msg_count": decoded_count})

        if decoded_count > 0:
            self._add_system_message(f"Joined {name} ({decoded_count} historical messages decoded)")
            self._reload_messages()
        else:
            self._add_system_message(f"Joined {name}")

    def _cmd_part(self, arg):
        if not arg:
            # Part currently selected channel
            if self._selected_channel:
                arg = self._selected_channel
            else:
                self._add_system_message("Usage: /part #channelname (or select a channel first)")
                return
        # Try exact match first, fall back to adding # prefix
        if any(ch["name"] == arg for ch in self._channels):
            name = arg
        else:
            name = arg if arg.startswith("#") else f"#{arg}"
        # Remove from core
        try:
            svc = self.application.service("collector")
            svc.core.remove_channel(name)
        except (KeyError, RuntimeError):
            pass
        # Remove from config
        from ..config import remove_channel_from_config
        remove_channel_from_config(name)
        # Remove from local list
        self._channels = [ch for ch in self._channels if ch["name"] != name]
        if self._selected_channel == name:
            self._selected_channel = None
            self._selected_idx = 0
        self._add_system_message(f"Left {name}")

    def _cmd_search(self, arg):
        self._search_text = arg
        self._reload_messages()
        if arg:
            self._add_system_message(f"Searching for: {arg}")
        else:
            self._add_system_message("Search cleared")

    def _cmd_segue_diag(self):
        from .system_diag import SystemDiagActivity
        self.application.segue_to(SystemDiagActivity())

    def _cmd_segue_dashboard(self, focus_panel="left"):
        from .dashboard import DashboardActivity
        self.application.segue_to(
            DashboardActivity(port=self._port, auto_start=False)
        )

    def _cmd_crack(self, arg):
        """Handle /crack command: status, start, stop, wordlist."""
        try:
            svc = self.application.service("collector")
            cracker = svc.core._cracker
        except (KeyError, RuntimeError):
            cracker = None

        if not cracker:
            self._add_system_message("Cracker not available (no collector connected)")
            return

        sub = arg.strip().split(None, 1) if arg.strip() else ["status"]
        subcmd = sub[0].lower()

        if subcmd == "status":
            st = cracker.status()
            state = "running" if st["running"] else "stopped"
            self._add_system_message(
                f"Cracker: {state}, {st['cracked_count']} cracked, "
                f"{st['pending_count']} pending, {st['wordlist_size']} words"
            )
            if st["cracked_names"]:
                self._add_system_message(
                    f"  Cracked: {', '.join(st['cracked_names'])}"
                )
            if st["pending_hashes"]:
                hexes = ", ".join(f"0x{h:02X}" for h in st["pending_hashes"])
                self._add_system_message(f"  Pending hashes: {hexes}")
        elif subcmd == "start":
            cracker.start()
            self._add_system_message("Cracker started")
        elif subcmd == "stop":
            cracker.stop()
            self._add_system_message("Cracker stopped")
        elif subcmd == "wordlist":
            if len(sub) > 1:
                path = sub[1].strip()
                cracker.add_wordlist(path)
                self._add_system_message(f"Loaded wordlist: {path}")
            else:
                self._add_system_message("Usage: /crack wordlist /path/to/file")
        else:
            self._add_system_message("Usage: /crack [status|start|stop|wordlist path]")

    def _cmd_status(self):
        self._add_system_message(f"Port: {self._port}")
        self._add_system_message(
            f"Status: {'Connected' if self._connected else self._status}"
        )
        self._add_system_message(f"Nodes: {self._node_count}")
        self._add_system_message(f"Packets: {self._rx_count}RX {self._tx_count}TX {self._adv_count}ADV")
        hb = self._last_heartbeat
        if hb:
            self._add_system_message(
                f"Uptime: {_fmt_uptime_short(hb.get('uptime_secs'))}  "
                f"Battery: {hb.get('battery_mv', '?')}mV"
            )
        self._add_system_message(f"Channels: {len(self._channels)}")

    def _cmd_help(self):
        self._add_system_message("Available commands:")
        self._add_system_message("  (bare text)         - Send message to selected channel")
        self._add_system_message("  /nick <name>        - Set your sender name")
        self._add_system_message("  /send [#chan] <msg>  - Send to current or specified channel")
        self._add_system_message("  /join #name         - Join a hashtag channel")
        self._add_system_message("  /join Name b64psk   - Join a PSK channel")
        self._add_system_message("  /part [#name]       - Leave current or named channel")
        self._add_system_message("  /search text        - Filter messages (empty to clear)")
        self._add_system_message("  /crack [status]     - Show cracker state")
        self._add_system_message("  /crack start|stop   - Toggle channel cracker")
        self._add_system_message("  /crack wordlist f   - Load custom wordlist")
        self._add_system_message("  /repeaters          - Open repeater list")
        self._add_system_message("  /rooms              - Open room server list")
        self._add_system_message("  /contacts           - Open all nodes list")
        self._add_system_message("  /diag               - Open system diagnostics")
        self._add_system_message("  /packets            - Open packet list (dashboard)")
        self._add_system_message("  /status             - Show connection info")
        self._add_system_message("  /help               - Show this help")
