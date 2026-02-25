"""
Chat Activity — IRC-style main screen for the MeshCore Collector.

Side-by-side layout: channel list + status on the left, messages on the right.
Always-visible command input line at the bottom for /commands.
Replaces the old DashboardActivity as the landing screen.
"""

import os
import sys
import time
from collections import deque
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange, TextBoxChange, TextBoxSubmit
from pyos.input_handlers import handle_text_box_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.TextInput import TextInput

from ..events import (
    ChannelMessage,
    CollectorConnected,
    CollectorDisconnected,
    CollectorError,
    CollectorFrame,
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


# Separator line for the sidebar between channels and status
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
        self._system_messages = []  # in-memory system messages

        # Dashboard stats (woven into sidebar)
        self._node_count = 0
        self._rx_count = 0
        self._tx_count = 0
        self._adv_count = 0
        self._last_heartbeat = None
        self._packet_times = deque(maxlen=1000)
        self._status = "Connecting..."
        self._connected = False

        # Search
        self._search_text = ""

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
            self._service_started = True
            self._load_channels_from_config()
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
        try:
            from ..config import load_config, load_channels
            config = load_config()
            channels = load_channels(config)
            for ch in channels:
                if not any(c["name"] == ch.name for c in self._channels):
                    self._channels.append({"name": ch.name, "msg_count": 0})
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

        # Node count
        node_rows = store.get_nodes()
        self._node_count = len(node_rows)

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
                "help": "TAB:rooms  ?:help  /help",
            }),
        }

    def _sidebar_items(self):
        """Build left panel: channel list + status block."""
        items = []
        if not self._channels:
            items.append("  (no channels)")
            items.append("  /join #name")
        else:
            for i, ch in enumerate(self._channels):
                marker = " >" if ch["name"] == self._selected_channel else "  "
                count = ch["msg_count"]
                items.append(f"{marker} {ch['name']:14s} {count:3d}")

        # Status separator + stats (not selectable — below channel entries)
        items.append(STATUS_SEPARATOR)
        items.extend(self._status_block())
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
        split["left_items"] = self._sidebar_items()
        split["right_items"] = self._message_items()
        split["right_title"] = self._selected_channel or "All"
        # Clamp left selection to channel count (don't select status lines)
        max_ch_idx = max(0, len(self._channels) - 1) if self._channels else 0
        if split["left_selected"] > max_ch_idx:
            split["left_selected"] = max_ch_idx
        # Auto-scroll messages to bottom
        right_items = split["right_items"]
        split["right_selected"] = max(0, len(right_items) - 1)

        self.display_state["bottom"]["items"]["status"] = self._bottom_status()
        self.display_state["split"]["focused"] = (self.focus == "split")
        self.display_state["command_input"]["focused"] = (self.focus == "command_input")
        self.refresh_screen()

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

        self.delegate_to_focused(event)
        # Clamp left_selected after any scroll to prevent selecting status lines
        if self.focus == "split":
            split = self.display_state["split"]
            if split["focused_panel"] == "left" and self._channels:
                max_idx = len(self._channels) - 1
                if split["left_selected"] > max_idx:
                    split["left_selected"] = max_idx
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
                self._node_count = max(self._node_count, len(self._get_known_nodes()))
        elif ft == FRAME_TYPE_HEARTBEAT:
            self._last_heartbeat = parsed

        self._update_display()

    def _get_known_nodes(self):
        """Get node count from store if available."""
        try:
            svc = self.application.service("collector")
            store = svc.store
            if store:
                return store.get_nodes()
        except (KeyError, RuntimeError):
            pass
        return []

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

    # --- Channel selection ---

    def _select_channel_from_sidebar(self):
        """ENTER on a channel in the left panel: select/deselect it."""
        if not self._channels:
            return
        idx = self.display_state["split"]["left_selected"]
        if idx >= len(self._channels):
            return
        ch_name = self._channels[idx]["name"]
        if self._selected_channel == ch_name:
            self._selected_channel = None
        else:
            self._selected_channel = ch_name
        self._selected_idx = idx
        self._reload_messages()
        self._update_display()

    # --- Command dispatcher ---

    def _dispatch_command(self, text):
        text = text.strip()
        if not text:
            return
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/join":
            self._cmd_join(arg)
        elif cmd == "/part":
            self._cmd_part(arg)
        elif cmd == "/search":
            self._cmd_search(arg)
        elif cmd == "/diag":
            self._cmd_segue_diag()
        elif cmd == "/nodes":
            self._cmd_segue_dashboard("right")
        elif cmd == "/packets":
            self._cmd_segue_dashboard("left")
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/help":
            self._cmd_help()
        else:
            self._add_system_message(f"Unknown command: {cmd}. Type /help for commands.")

    def _cmd_join(self, arg):
        if not arg:
            self._add_system_message("Usage: /join #channelname")
            return
        name = arg if arg.startswith("#") else f"#{arg}"
        # Check if already joined
        for ch in self._channels:
            if ch["name"] == name:
                self._add_system_message(f"Already joined {name}")
                return
        # Create channel and add to core
        from ..crypto import Channel
        channel = Channel.from_hashtag(name)
        try:
            svc = self.application.service("collector")
            svc.core.add_channel(channel)
        except (KeyError, RuntimeError):
            pass
        # Save to config
        from ..config import add_channel_to_config
        add_channel_to_config(name)
        # Add to local list
        self._channels.append({"name": name, "msg_count": 0})
        self._add_system_message(f"Joined {name}")

    def _cmd_part(self, arg):
        if not arg:
            # Part currently selected channel
            if self._selected_channel:
                arg = self._selected_channel
            else:
                self._add_system_message("Usage: /part #channelname (or select a channel first)")
                return
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
        self._add_system_message("  /join #name  - Join a hashtag channel")
        self._add_system_message("  /part [#name] - Leave current or named channel")
        self._add_system_message("  /search text - Filter messages (empty to clear)")
        self._add_system_message("  /diag        - Open system diagnostics")
        self._add_system_message("  /nodes       - Open node list (dashboard)")
        self._add_system_message("  /packets     - Open packet list (dashboard)")
        self._add_system_message("  /status      - Show connection info")
        self._add_system_message("  /help        - Show this help")
