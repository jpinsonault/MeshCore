"""
System Diagnostics Activity — OS-level telemetry from the repeater.

Shows MCU temperature, heap usage, radio metrics, error flags, dedup stats,
and packet pool info. Data arrives via DIAGNOSTICS frames (0xD4) every 30s,
with battery/uptime supplemented from HEARTBEAT frames.

Accessed by pressing 's' from the dashboard.
"""

import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.ScrollList import ScrollList

from ..events import CollectorFrame
from ..protocol import FRAME_TYPE_DIAGNOSTICS, FRAME_TYPE_HEARTBEAT

SPARK_CHARS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

ERR_FLAG_BITS = [
    (0x01, "Pool exhausted"),
    (0x02, "CAD timeout"),
    (0x04, "RX start fail"),
]


def _value_sparkline(values, width=30, min_val=None, max_val=None):
    """Create a sparkline from a list of numeric values."""
    if not values:
        return "(no data)"
    vals = list(values)[-width:]
    lo = min_val if min_val is not None else min(vals)
    hi = max_val if max_val is not None else max(vals)
    if hi == lo:
        return SPARK_CHARS[4] * len(vals)
    chars = []
    for v in vals:
        normalized = (v - lo) / (hi - lo)
        normalized = max(0.0, min(1.0, normalized))
        idx = int(normalized * (len(SPARK_CHARS) - 1))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)


def _fmt_heap_bar(free, total, width=12):
    """Format a heap usage bar: ████████░░░░ 287,432 free / 368,640 B (78% used)"""
    if not total or total == 0:
        return "---"
    used = total - free
    pct = used / total
    filled = int(pct * width)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return f"{bar} {free:,} free / {total:,} B ({pct * 100:.0f}% used)"


def _fmt_duty(airtime_ms, uptime_secs):
    """Format duty cycle: 2.3% (342s / 14,820s)"""
    if not uptime_secs or uptime_secs == 0:
        return "---"
    airtime_s = airtime_ms / 1000.0
    pct = (airtime_s / uptime_secs) * 100
    return f"{pct:.1f}% ({airtime_s:,.0f}s / {uptime_secs:,}s)"


def _fmt_error_flag(flags, bit, label):
    """Format a single error flag bit."""
    if flags & bit:
        return f"\u26a0 {label}"
    return f"\u2713 OK"


def _fmt_recv_errors(errors, total):
    """Format receive errors: 4,523 total (12 errors, 0.27%)"""
    if total is None or total == 0:
        return "0 total"
    if errors is None or errors == 0:
        return f"{total:,} total (0 errors)"
    pct = (errors / total) * 100
    return f"{total:,} total ({errors:,} errors, {pct:.2f}%)"


def _fmt_uptime(secs):
    """Format seconds as Xd Xh Xm."""
    if secs is None:
        return "---"
    d = secs // 86400
    h = (secs % 86400) // 3600
    m = (secs % 3600) // 60
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m {secs % 60}s"


def _fmt_ago(ts):
    """Format a timestamp as relative time."""
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    return f"{int(delta / 3600)}h ago"


def build_diag_lines(diag, heartbeat=None, temp_history=None, heap_history=None):
    """Build the formatted display lines from diagnostics + heartbeat data.

    Pure function — no curses dependency. Used by both TUI and tests.

    Args:
        diag: dict from parse_diagnostics() or store row, or None
        heartbeat: dict from parse_heartbeat() or store row, or None
        temp_history: list of float temperature values for sparkline
        heap_history: list of (free, total) tuples for sparkline
    """
    lines = []

    if not diag:
        lines.append("")
        lines.append("  (waiting for first diagnostics frame...)")
        lines.append("")
        lines.append("  Diagnostics are sent every 30s.")
        lines.append("  Press 'r' to request an immediate update.")

        # Show heartbeat data if available while waiting
        if heartbeat:
            lines.append("")
            uptime = heartbeat.get("uptime_secs")
            if uptime is not None:
                lines.append(f"  Uptime          {_fmt_uptime(uptime)}")
            batt = heartbeat.get("battery_mv")
            if batt is not None:
                lines.append(f"  Battery         {batt:,} mV")
            fp = heartbeat.get("free_pkts")
            if fp is not None:
                lines.append(f"  Packet pool     {fp} free")

        return lines

    # --- PROCESSOR ---
    lines.append("")
    lines.append("  PROCESSOR")
    temp = diag.get("mcu_temp")
    if temp is not None and temp == temp:  # NaN check
        lines.append(f"    Temperature     {temp:.1f}\u00b0C")
    else:
        lines.append("    Temperature     (not available)")

    uptime = None
    if heartbeat:
        uptime = heartbeat.get("uptime_secs")
    if uptime is not None:
        lines.append(f"    Uptime          {_fmt_uptime(uptime)}")

    if temp_history and len(temp_history) >= 2:
        spark = _value_sparkline(temp_history, width=30, min_val=20.0, max_val=80.0)
        lines.append(f"    Temp history    {spark}")

    # --- MEMORY ---
    lines.append("")
    lines.append("  MEMORY")
    free_heap = diag.get("free_heap")
    total_heap = diag.get("total_heap")
    min_free = diag.get("min_free_heap")
    if free_heap is not None and total_heap:
        lines.append(f"    Heap            {_fmt_heap_bar(free_heap, total_heap)}")
    else:
        lines.append("    Heap            ---")
    if min_free is not None:
        lines.append(f"    Minimum         {min_free:,} B (lowest since boot)")

    if heap_history and len(heap_history) >= 2:
        pcts = []
        for fh, th in heap_history:
            if th and th > 0:
                pcts.append((th - fh) / th * 100)
        if pcts:
            spark = _value_sparkline(pcts, width=30, min_val=0, max_val=100)
            lines.append(f"    Heap history    {spark}")

    # --- RADIO ---
    lines.append("")
    lines.append("  RADIO")
    nf = diag.get("noise_floor")
    if nf is not None:
        lines.append(f"    Noise floor     {nf} dBm")
    rssi = diag.get("last_rssi")
    if rssi is not None:
        lines.append(f"    Last RSSI       {rssi} dBm")
    snr = diag.get("last_snr")
    if snr is not None:
        lines.append(f"    Last SNR        {snr:+.1f} dB")
    tx_air = diag.get("tx_airtime_ms", 0)
    rx_air = diag.get("rx_airtime_ms", 0)
    if uptime:
        lines.append(f"    TX duty         {_fmt_duty(tx_air, uptime)}")
        lines.append(f"    RX duty         {_fmt_duty(rx_air, uptime)}")

    # --- POWER ---
    if heartbeat:
        batt = heartbeat.get("battery_mv")
        if batt is not None:
            lines.append("")
            lines.append("  POWER")
            lines.append(f"    Battery         {batt:,} mV")

    # --- PACKETS ---
    lines.append("")
    lines.append("  PACKETS")
    # Pool info from heartbeat
    if heartbeat and heartbeat.get("free_pkts") is not None:
        lines.append(f"    Pool            {heartbeat['free_pkts']} free")
    tq = diag.get("tx_queue_len")
    if tq is not None:
        lines.append(f"    TX queue        {tq} pending")
    n_recv = diag.get("n_recv")
    recv_err = diag.get("recv_errors")
    if n_recv is not None:
        lines.append(f"    Received        {_fmt_recv_errors(recv_err, n_recv)}")
    n_sent = diag.get("n_sent")
    if n_sent is not None:
        lines.append(f"    Sent            {n_sent:,} total")
    dd = diag.get("direct_dups", 0)
    fd = diag.get("flood_dups", 0)
    if dd or fd:
        lines.append(f"    Deduped         {fd} flood + {dd} direct saved")

    # --- ERRORS ---
    err_flags = diag.get("err_flags", 0)
    lines.append("")
    lines.append("  ERRORS")
    for bit, label in ERR_FLAG_BITS:
        lines.append(f"    {label:18s}{_fmt_error_flag(err_flags, bit, label)}")

    # --- Timestamp ---
    diag_ts = diag.get("timestamp")
    lines.append("")
    lines.append(f"                               Updated {_fmt_ago(diag_ts)}")

    return lines


class SystemDiagActivity(Activity):
    """System diagnostics view — OS telemetry from the repeater."""

    def __init__(self):
        super().__init__()
        self._diag = None
        self._heartbeat = None
        self._temp_history = deque(maxlen=60)
        self._heap_history = deque(maxlen=60)
        self._diag_timestamp = None
        self.tab_order = ["content"]
        self.focus = "content"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(CollectorFrame, self, self._on_frame)

        # Load latest from store
        self._load_from_store()
        self._build_display()

    def _load_from_store(self):
        """Load latest diagnostics and heartbeat from SQLite."""
        try:
            svc = self.application.service("collector")
            store = svc.store
        except (KeyError, RuntimeError):
            return

        if store:
            d = store.get_latest_diagnostics()
            if d:
                self._diag = d
                self._diag_timestamp = d.get("timestamp")
                self._temp_history.append(d.get("mcu_temp", 0))
                fh = d.get("free_heap")
                th = d.get("total_heap")
                if fh is not None and th:
                    self._heap_history.append((fh, th))

            hbs = store.get_heartbeats(limit=1)
            if hbs:
                self._heartbeat = hbs[0]

    def _build_display(self):
        lines = build_diag_lines(
            self._diag, self._heartbeat,
            list(self._temp_history), list(self._heap_history),
        )
        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "System Diagnostics",
                "help": "",
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
                "status": "",
                "help": "ESC:back  r:refresh                                    s:diag",
            }),
        }

    def _update_display(self):
        lines = build_diag_lines(
            self._diag, self._heartbeat,
            list(self._temp_history), list(self._heap_history),
        )
        self.display_state["content"]["items"] = lines
        self.refresh_screen()

    def _on_frame(self, event):
        frame = event.frame
        ft = frame["type"]
        parsed = frame.get("parsed", {})

        if ft == FRAME_TYPE_DIAGNOSTICS and parsed:
            self._diag = parsed
            self._diag_timestamp = frame.get("received_at", time.time())
            # Inject timestamp for display
            if "timestamp" not in self._diag:
                self._diag["timestamp"] = self._diag_timestamp

            temp = parsed.get("mcu_temp")
            if temp is not None:
                self._temp_history.append(temp)
            fh = parsed.get("free_heap")
            th = parsed.get("total_heap")
            if fh is not None and th:
                self._heap_history.append((fh, th))
            self._update_display()
        elif ft == FRAME_TYPE_HEARTBEAT and parsed:
            self._heartbeat = parsed
            self._update_display()

    def _request_diag(self):
        """Send 'collector diag' to the device for an immediate diagnostics frame."""
        try:
            svc = self.application.service("collector")
            core = svc.core
            if core.send_command(b"collector diag\r"):
                self._set_status("Requesting diagnostics...")
            else:
                self._set_status("No serial connection")
        except (KeyError, RuntimeError):
            self._set_status("Collector not running")

    def _set_status(self, text):
        """Update the bottom bar status text."""
        if self.display_state and "bottom" in self.display_state:
            self.display_state["bottom"]["items"]["status"] = text
            self.refresh_screen()

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
            return
        if event.key == ord("r") or event.key == ord("R"):
            self._request_diag()
            return
        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()
