"""
Dashboard Activity — live view of mesh network traffic.

Shows real-time statistics, recent packets, and known nodes.
Subscribes to CollectorFrame events from the MeshCollectorService.
"""

import curses
import time
from collections import deque
from datetime import datetime, timezone
from functools import partial

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
from pyos.printers.Table import Table
from pyos.printers.MultilineText import MultilineText

from ..events import (
    ChannelMessage,
    CollectorConnected,
    CollectorDisconnected,
    CollectorError,
    CollectorFrame,
)
from ..mesh_service import MeshCollectorService
from ..protocol import (
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_HEARTBEAT,
    FRAME_TYPE_NAMES,
    FRAME_TYPE_RX_RAW,
    FRAME_TYPE_TX_RAW,
    PAYLOAD_TYPES,
    ROUTE_TYPES,
)

SPARK_CHARS = " ▁▂▃▄▅▆▇█"
SPARKLINE_BUCKETS = 30
SPARKLINE_WINDOW = 60  # seconds


def _fmt_time(ts):
    """Format a unix timestamp as HH:MM:SS."""
    if not ts:
        return "---"
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OSError, ValueError):
        return "---"


def _fmt_uptime(secs):
    """Format seconds as Xh Xm Xs."""
    if secs is None:
        return "---"
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def make_sparkline(timestamps, now=None, window=SPARKLINE_WINDOW, buckets=SPARKLINE_BUCKETS):
    """Generate a sparkline string from a list of timestamps.

    Divides the last `window` seconds into `buckets` time slots and maps
    each bucket's count to a block character.
    """
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


def calc_packet_rate(timestamps, now=None, window=10):
    """Calculate packets per second over the last `window` seconds."""
    if now is None:
        now = time.time()
    cutoff = now - window
    count = sum(1 for ts in timestamps if ts >= cutoff)
    return count / window


def make_type_distribution(type_counts, width=20):
    """Build a mini payload-type bar from a dict of {name: count}."""
    if not type_counts:
        return ""
    total = sum(type_counts.values())
    if total == 0:
        return ""
    parts = []
    for name, count in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        bar_len = max(1, int(count / total * width))
        parts.append(f"{name}:{'|' * bar_len}")
    return "  ".join(parts[:4])  # top 4 types


class DashboardActivity(Activity):
    """Main collector dashboard showing live mesh data."""

    def __init__(self, port, baud=115200, auto_start=True, ws_port=None):
        super().__init__()
        self._port = port
        self._baud = baud
        self._auto_start = auto_start
        self._ws_port = ws_port  # WebSocket port or None
        self._server = None
        self._frame_count = 0
        self._rx_count = 0
        self._tx_count = 0
        self._adv_count = 0
        self._recent_packets = []  # last N packet dicts (with full frame data)
        self._nodes = {}  # pub_key_hex -> info
        self._node_snr_history = {}  # pub_key_hex -> deque of (timestamp, snr)
        self._last_heartbeat = None
        self._channel_msg_count = 0
        self._channel_count = 0
        self._status = "Connecting..."
        self._max_recent = 200
        self._packet_times = deque(maxlen=1000)  # timestamps for rate/sparkline
        self._payload_type_counts = {}  # payload_name -> count
        self._sorted_node_keys = []  # ordered pub_key_hex list for index lookup
        self._service_started = False  # True after first on_start creates the service
        self._channel_names = set()  # track unique channel names
        self.tab_order = ["packets", "nodes"]
        self.focus = "packets"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(CollectorConnected, self, self._on_connected)
        self.application.subscribe(CollectorDisconnected, self, self._on_disconnected)
        self.application.subscribe(CollectorFrame, self, self._on_frame)
        self.application.subscribe(CollectorError, self, self._on_error)
        self.application.subscribe(ChannelMessage, self, self._on_channel_message)

        if self._service_started:
            # Re-entry after segue — service is still running, reload state from SQLite
            self._reload_from_store()
            self._build_display()
            self._update_display()
        else:
            if self._auto_start:
                self._start_collector_service()
                if self._ws_port:
                    self._start_server()
                self._service_started = True
            self._build_display()

    def on_stop(self):
        # Service and server keep running across screen transitions.
        # They are application-scoped and outlive this activity.
        pass

    def _start_collector_service(self):
        """Register and start the collector service.

        If a previous instance exists (e.g. user went back and re-selected),
        replace it.
        """
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
        """Start the WebSocket + HTTP server alongside the collector."""
        try:
            svc = self.application.service("collector")
            core = svc.core
        except (KeyError, RuntimeError):
            return
        from ..server import CollectorServer
        self._server = CollectorServer(core, ws_port=self._ws_port)
        self._server.start()

    def _reload_from_store(self):
        """Rebuild in-memory dashboard state from SQLite after a segue return."""
        try:
            svc = self.application.service("collector")
            store = svc.store
        except (KeyError, RuntimeError):
            return

        stats = store.get_stats()
        self._frame_count = stats["total_packets"] + stats["advert_count"]
        self._rx_count = stats["rx_count"]
        self._tx_count = stats["tx_count"]
        self._adv_count = stats["advert_count"]
        self._last_heartbeat = stats.get("latest_heartbeat")
        self._channel_msg_count = stats["channel_msg_count"]
        self._status = "Connected"

        # Channel count
        summaries = store.get_channel_summary()
        self._channel_count = len(summaries)
        self._channel_names = {s["channel_name"] for s in summaries}

        # Recent packets — rebuild display dicts from DB rows
        rows = store.get_recent_packets(limit=self._max_recent)
        rows.reverse()  # oldest first, like live accumulation
        self._recent_packets = []
        for row in rows:
            direction = row["direction"].upper()
            route_name = ROUTE_TYPES.get(row.get("route_type"), "?")
            ptype_name = PAYLOAD_TYPES.get(row.get("payload_type"), "?")
            if direction == "RX":
                extra = f"SNR:{row.get('snr', 0) or 0:+.1f} RSSI:{row.get('rssi', 0) or 0}"
            else:
                extra = f"len:{row.get('raw_len', 0)}"
            self._recent_packets.append({
                "time": row["timestamp"],
                "dir": direction,
                "route": route_name,
                "ptype": ptype_name,
                "extra": extra,
                "frame": {
                    "type": FRAME_TYPE_RX_RAW if direction == "RX" else FRAME_TYPE_TX_RAW,
                    "received_at": row["timestamp"],
                    "parsed": {
                        "snr": row.get("snr"),
                        "rssi": row.get("rssi"),
                        "route_type": row.get("route_type"),
                        "route_name": route_name,
                        "payload_type": row.get("payload_type"),
                        "payload_name": ptype_name,
                        "raw_len": row.get("raw_len", 0),
                        "raw": bytes.fromhex(row.get("raw_hex", "") or ""),
                    },
                },
            })

        # Nodes
        node_rows = store.get_nodes()
        self._nodes = {}
        for n in node_rows:
            pk = n["pub_key_hex"]
            self._nodes[pk] = {
                "name": n.get("name", "?"),
                "adv_type_name": n.get("adv_type_name", "?"),
                "snr": 0,  # last SNR not stored in nodes table
                "last_seen": n.get("last_seen"),
                "count": n.get("advert_count", 0),
                "lat": n.get("lat"),
                "lon": n.get("lon"),
                "pub_key_hex": pk,
            }

        # Packet timestamps for sparkline/rate
        ts_list = store.get_recent_packet_timestamps(limit=1000)
        self._packet_times = deque(reversed(ts_list), maxlen=1000)

        # Payload type distribution
        type_rows = store.get_traffic_by_type()
        self._payload_type_counts = {}
        for tr in type_rows:
            name = PAYLOAD_TYPES.get(tr["payload_type"], f"0x{tr['payload_type']:02X}" if tr["payload_type"] is not None else "?")
            self._payload_type_counts[name] = tr["cnt"]

        # SNR history can't be fully recovered; starts empty, repopulates from live data
        self._node_snr_history = {}

    def _resolve_db_path(self):
        from ..config import load_config, DEFAULT_CONFIG_DIR
        config = load_config()
        db_path = config.get("db_path", "collector.db")
        if not os.path.isabs(db_path):
            db_path = str(DEFAULT_CONFIG_DIR / db_path)
        return db_path

    def _build_display(self):
        """Build the initial display_state layout."""
        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "MeshCore Collector",
                "help": f"{self._port}",
            }),
            "stats": MultilineText.display_state(
                lines=self._stats_lines(),
                min_height=3,
                max_height=5,
            ),
            "hr1": HorizontalBar.display_state(),
            "packets": ScrollList.display_state(
                self.screen,
                items=["(waiting for packets...)"],
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
                min_height=5,
                flex=2,
            ),
            "hr2": HorizontalBar.display_state(),
            "nodes": ScrollList.display_state(
                self.screen,
                items=["(waiting for advertisements...)"],
                selected_index=0,
                focused=False,
                input_handler=handle_scroll_list_input,
                min_height=3,
                flex=1,
            ),
            "bottom": BottomBar.display_state(items={
                "status": self._status,
                "help": "TAB:focus  ENTER:detail  c:chan  d:log  ?:help  q:quit",
            }),
        }

    def _stats_lines(self):
        """Generate the summary stats lines."""
        lines = []
        now = time.time()

        # Line 1: heartbeat data or waiting message
        hb = self._last_heartbeat
        if hb:
            lines.append(
                f"  Uptime: {_fmt_uptime(hb.get('uptime_secs'))}  |  "
                f"Battery: {hb.get('battery_mv', '?')}mV  |  "
                f"Free pkts: {hb.get('free_pkts', '?')}"
            )
            lines.append(
                f"  Device RX: {hb.get('rx_flood', 0)}F/{hb.get('rx_direct', 0)}D  |  "
                f"Device TX: {hb.get('tx_flood', 0)}F/{hb.get('tx_direct', 0)}D"
            )
        else:
            lines.append("  (awaiting first heartbeat...)")

        # Line 2: capture stats + packet rate
        rate = calc_packet_rate(self._packet_times, now)
        rate_str = f"  {rate:.1f} pkt/s" if self._frame_count > 0 else ""
        lines.append(
            f"  Captured: {self._frame_count} frames  "
            f"({self._rx_count} RX, {self._tx_count} TX, {self._adv_count} ADV)"
            f"{rate_str}"
        )

        # Line 3: sparkline + channel stats
        extra_parts = []
        if self._packet_times:
            spark = make_sparkline(self._packet_times, now)
            extra_parts.append(f"  Traffic: {spark}")
        if self._channel_msg_count > 0:
            extra_parts.append(
                f"  Channels: {self._channel_msg_count} msgs decoded "
                f"({self._channel_count} channel{'s' if self._channel_count != 1 else ''})"
            )
        if extra_parts:
            lines.append("".join(extra_parts))

        if self._server and self._server.is_running:
            lines.append(f"  WS: {self._server.ws_url}")

        return lines

    def _packet_line(self, pkt):
        """Format a packet dict into a single display line."""
        ts = _fmt_time(pkt.get("time"))
        direction = pkt.get("dir", "?")
        route = pkt.get("route", "?")
        ptype = pkt.get("ptype", "?")
        extra = pkt.get("extra", "")
        return f" {ts} {direction:2s} {route:18s} {ptype:10s} {extra}"

    def _node_line(self, key, info):
        """Format a node info dict into a display line."""
        name = info.get("name", "?")
        atype = info.get("adv_type_name", "?")
        snr = info.get("snr", 0)
        seen = _fmt_time(info.get("last_seen"))
        count = info.get("count", 0)
        # SNR sparkline from history
        snr_history = self._node_snr_history.get(key, deque())
        if len(snr_history) >= 2:
            spark = self._snr_sparkline(snr_history)
            snr_part = f"SNR:{snr:+5.1f} {spark}"
        else:
            snr_part = f"SNR:{snr:+5.1f}"
        pk_short = key[:12] + ".."
        return f" {name:16s} {atype:9s} {snr_part}  seen:{seen}  x{count}  [{pk_short}]"

    def _snr_sparkline(self, snr_history, width=8):
        """Create a mini sparkline from SNR readings."""
        values = [snr for _, snr in snr_history]
        if len(values) > width:
            values = values[-width:]
        # Map SNR range (-20 to +10) into sparkline chars
        min_snr, max_snr = -20.0, 10.0
        chars = []
        for v in values:
            normalized = (v - min_snr) / (max_snr - min_snr)
            normalized = max(0.0, min(1.0, normalized))
            idx = int(normalized * (len(SPARK_CHARS) - 1))
            chars.append(SPARK_CHARS[idx])
        return "".join(chars)

    def _update_display(self):
        """Refresh display_state with current data."""
        self.display_state["stats"]["lines"] = self._stats_lines()

        if self._recent_packets:
            self.display_state["packets"]["items"] = [
                self._packet_line(p) for p in self._recent_packets
            ]
        else:
            self.display_state["packets"]["items"] = ["(waiting for packets...)"]

        if self._nodes:
            sorted_nodes = sorted(
                self._nodes.items(),
                key=lambda kv: kv[1].get("last_seen", 0),
                reverse=True,
            )
            self._sorted_node_keys = [k for k, v in sorted_nodes]
            self.display_state["nodes"]["items"] = [
                self._node_line(k, v) for k, v in sorted_nodes
            ]
        else:
            self._sorted_node_keys = []
            self.display_state["nodes"]["items"] = ["(waiting for advertisements...)"]

        self.display_state["bottom"]["items"]["status"] = self._status
        self.refresh_screen()

    # --- Event handlers ---

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
            return
        if event.key == ord("q") or event.key == ord("Q"):
            from pyos.EventTypes import StopApplication
            self.event_queue.put(StopApplication())
            return
        if event.key == ord("c") or event.key == ord("C"):
            self._open_channels()
            return
        if event.key == ord("d") or event.key == ord("D"):
            self._open_debug_log()
            return
        if event.key == ord("?"):
            self._open_help()
            return
        if event.key == Keys.TAB:
            self.cycle_focus()
            self.refresh_screen()
            return
        if event.key == Keys.ENTER:
            self._open_detail()
            return

        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()

    def _on_connected(self, event):
        self._status = "Connected"
        self._update_display()

    def _on_disconnected(self, event):
        self._status = f"Disconnected: {event.reason}"
        self._update_display()

    def _on_error(self, event):
        self._status = f"Error: {event.message}"
        self._update_display()

    def _on_frame(self, event):
        frame = event.frame
        ft = frame["type"]
        parsed = frame.get("parsed", {})
        self._frame_count += 1
        received_at = frame.get("received_at", time.time())
        self._packet_times.append(received_at)

        if ft == FRAME_TYPE_RX_RAW:
            self._rx_count += 1
            ptype_name = parsed.get("payload_name", "?")
            self._payload_type_counts[ptype_name] = self._payload_type_counts.get(ptype_name, 0) + 1
            self._recent_packets.append({
                "time": received_at,
                "dir": "RX",
                "route": parsed.get("route_name", "?"),
                "ptype": ptype_name,
                "extra": f"SNR:{parsed.get('snr', 0):+.1f} RSSI:{parsed.get('rssi', 0)}",
                "frame": frame,
            })
        elif ft == FRAME_TYPE_TX_RAW:
            self._tx_count += 1
            ptype_name = parsed.get("payload_name", "?")
            self._payload_type_counts[ptype_name] = self._payload_type_counts.get(ptype_name, 0) + 1
            self._recent_packets.append({
                "time": received_at,
                "dir": "TX",
                "route": parsed.get("route_name", "?"),
                "ptype": ptype_name,
                "extra": f"len:{parsed.get('raw_len', 0)}",
                "frame": frame,
            })
        elif ft == FRAME_TYPE_ADVERTISEMENT:
            self._adv_count += 1
            pk = parsed.get("pub_key_hex", "")
            if pk:
                existing = self._nodes.get(pk, {})
                self._nodes[pk] = {
                    "name": parsed.get("name") or existing.get("name", "?"),
                    "adv_type_name": parsed.get("adv_type_name") or existing.get("adv_type_name", "?"),
                    "snr": parsed.get("snr", existing.get("snr", 0)),
                    "last_seen": received_at,
                    "count": existing.get("count", 0) + 1,
                    "lat": parsed.get("lat", existing.get("lat")),
                    "lon": parsed.get("lon", existing.get("lon")),
                    "pub_key_hex": pk,
                }
                # Track SNR history
                if pk not in self._node_snr_history:
                    self._node_snr_history[pk] = deque(maxlen=50)
                self._node_snr_history[pk].append((received_at, parsed.get("snr", 0)))
        elif ft == FRAME_TYPE_HEARTBEAT:
            self._last_heartbeat = parsed

        # Trim recent packets
        if len(self._recent_packets) > self._max_recent:
            self._recent_packets = self._recent_packets[-self._max_recent:]

        self._update_display()

    def _on_channel_message(self, event):
        self._channel_msg_count += 1
        self._channel_names.add(event.msg.channel_name)
        self._channel_count = len(self._channel_names)
        self._update_display()

    def _open_detail(self):
        """Open detail view for the focused item."""
        if self.focus == "packets" and self._recent_packets:
            idx = self.display_state["packets"]["selected_index"]
            if 0 <= idx < len(self._recent_packets):
                pkt = self._recent_packets[idx]
                frame = pkt.get("frame")
                if frame:
                    from .packet_detail import PacketDetailActivity
                    self.application.segue_to(PacketDetailActivity(frame=frame))
        elif self.focus == "nodes" and self._sorted_node_keys:
            idx = self.display_state["nodes"]["selected_index"]
            if 0 <= idx < len(self._sorted_node_keys):
                pk = self._sorted_node_keys[idx]
                info = self._nodes.get(pk, {})
                snr_history = list(self._node_snr_history.get(pk, []))
                from .node_detail import NodeDetailActivity
                self.application.segue_to(NodeDetailActivity(
                    pub_key_hex=pk,
                    info=info,
                    snr_history=snr_history,
                ))

    def _open_channels(self):
        """Open the channel browser."""
        try:
            svc = self.application.service("collector")
            store = svc.store
        except (KeyError, RuntimeError):
            store = None
        if store:
            from .channels import ChannelBrowserActivity
            self.application.segue_to(ChannelBrowserActivity(store=store))

    def _open_debug_log(self):
        """Open the debug log viewer."""
        from .debug_log import DebugLogActivity
        self.application.segue_to(DebugLogActivity())

    def _open_help(self):
        """Open the help overlay."""
        from .help_overlay import HelpActivity
        self.application.segue_to(HelpActivity(context="dashboard"))
