"""
Dashboard Activity — live view of mesh network traffic.

Shows real-time statistics, recent packets, and known nodes.
Subscribes to CollectorFrame events from the MeshCollectorService.
"""

import curses
import time
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


class DashboardActivity(Activity):
    """Main collector dashboard showing live mesh data."""

    def __init__(self, port, baud=115200, auto_start=True):
        super().__init__()
        self._port = port
        self._baud = baud
        self._auto_start = auto_start
        self._frame_count = 0
        self._rx_count = 0
        self._tx_count = 0
        self._adv_count = 0
        self._recent_packets = []  # last N packet summaries
        self._nodes = {}  # pub_key_hex -> info
        self._last_heartbeat = None
        self._status = "Connecting..."
        self._max_recent = 100
        self.tab_order = ["packets", "nodes"]
        self.focus = "packets"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(CollectorConnected, self, self._on_connected)
        self.application.subscribe(CollectorDisconnected, self, self._on_disconnected)
        self.application.subscribe(CollectorFrame, self, self._on_frame)
        self.application.subscribe(CollectorError, self, self._on_error)

        self._build_display()

        if self._auto_start:
            self._start_collector_service()

    def on_stop(self):
        try:
            svc = self.application.service("collector")
            if svc.is_running:
                self.application.stop_service("collector")
        except (KeyError, RuntimeError):
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
                max_height=4,
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
                "help": "TAB:switch  ESC:back  q:quit",
            }),
        }

    def _stats_lines(self):
        """Generate the summary stats lines."""
        lines = []
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

        lines.append(
            f"  Captured: {self._frame_count} frames  "
            f"({self._rx_count} RX, {self._tx_count} TX, {self._adv_count} ADV)"
        )
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
        pk_short = key[:12] + ".."
        return f" {name:16s} {atype:9s} SNR:{snr:+5.1f}  seen:{seen}  x{count}  [{pk_short}]"

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
            self.display_state["nodes"]["items"] = [
                self._node_line(k, v) for k, v in sorted(
                    self._nodes.items(),
                    key=lambda kv: kv[1].get("last_seen", 0),
                    reverse=True,
                )
            ]
        else:
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
        if event.key == Keys.TAB:
            self.cycle_focus()
            self.refresh_screen()
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

        if ft == FRAME_TYPE_RX_RAW:
            self._rx_count += 1
            self._recent_packets.append({
                "time": frame.get("received_at"),
                "dir": "RX",
                "route": parsed.get("route_name", "?"),
                "ptype": parsed.get("payload_name", "?"),
                "extra": f"SNR:{parsed.get('snr', 0):+.1f} RSSI:{parsed.get('rssi', 0)}",
            })
        elif ft == FRAME_TYPE_TX_RAW:
            self._tx_count += 1
            self._recent_packets.append({
                "time": frame.get("received_at"),
                "dir": "TX",
                "route": parsed.get("route_name", "?"),
                "ptype": parsed.get("payload_name", "?"),
                "extra": f"len:{parsed.get('raw_len', 0)}",
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
                    "last_seen": frame.get("received_at"),
                    "count": existing.get("count", 0) + 1,
                }
        elif ft == FRAME_TYPE_HEARTBEAT:
            self._last_heartbeat = parsed

        # Trim recent packets
        if len(self._recent_packets) > self._max_recent:
            self._recent_packets = self._recent_packets[-self._max_recent:]

        self._update_display()
