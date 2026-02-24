"""
Node Detail Activity — drill-down view of a known mesh node.

Shows full public key, device type, GPS coordinates, SNR history
with sparkline visualization, and advertisement statistics.
Accessed by pressing ENTER on a node in the dashboard.
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
from pyos.printers.ScrollList import ScrollList

SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def _fmt_time(ts):
    if not ts:
        return "---"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return "---"


def _fmt_ago(ts):
    """Format a timestamp as relative time (e.g. '2m ago')."""
    if not ts:
        return "---"
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def snr_sparkline(snr_history, width=30):
    """Create a sparkline from SNR readings.

    snr_history: list of (timestamp, snr) tuples
    Returns a string of block characters.
    """
    if not snr_history:
        return "(no data)"
    values = [snr for _, snr in snr_history]
    if len(values) > width:
        values = values[-width:]
    min_snr, max_snr = -20.0, 10.0
    chars = []
    for v in values:
        normalized = (v - min_snr) / (max_snr - min_snr)
        normalized = max(0.0, min(1.0, normalized))
        idx = int(normalized * (len(SPARK_CHARS) - 1))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)


def snr_stats(snr_history):
    """Compute min/max/avg/current SNR from history."""
    if not snr_history:
        return None
    values = [snr for _, snr in snr_history]
    return {
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "current": values[-1],
        "samples": len(values),
    }


def build_node_detail_lines(pub_key_hex, info, snr_history):
    """Build the full detail view lines for a node."""
    lines = []
    name = info.get("name", "?")
    atype = info.get("adv_type_name", "?")

    lines.append(f"  Name:          {name}")
    lines.append(f"  Type:          {atype}")
    lines.append("")

    # Public key (formatted in groups of 8)
    lines.append("  Public Key:")
    for i in range(0, len(pub_key_hex), 16):
        chunk = pub_key_hex[i:i + 16]
        spaced = " ".join(chunk[j:j + 2] for j in range(0, len(chunk), 2))
        lines.append(f"    {spaced}")
    lines.append("")

    # GPS
    lat = info.get("lat")
    lon = info.get("lon")
    if lat is not None and lon is not None:
        lines.append(f"  Location:      {lat:.6f}, {lon:.6f}")
    else:
        lines.append("  Location:      (no GPS data)")
    lines.append("")

    # Timing
    first_seen = info.get("first_seen")
    last_seen = info.get("last_seen")
    count = info.get("count", 0)
    lines.append(f"  First Seen:    {_fmt_time(first_seen)}")
    lines.append(f"  Last Seen:     {_fmt_time(last_seen)} ({_fmt_ago(last_seen)})")
    lines.append(f"  Advertisements: {count}")
    lines.append("")

    # SNR section
    lines.append("  --- Signal Quality ---")
    stats = snr_stats(snr_history)
    if stats:
        lines.append(f"  Current SNR:   {stats['current']:+.1f} dB")
        lines.append(f"  Min/Avg/Max:   {stats['min']:+.1f} / {stats['avg']:+.1f} / {stats['max']:+.1f} dB")
        lines.append(f"  Samples:       {stats['samples']}")
        lines.append("")
        spark = snr_sparkline(snr_history)
        lines.append(f"  SNR History:   {spark}")
        lines.append("                 -20dB              +10dB")
    else:
        lines.append("  (no signal data yet)")

    return lines


class NodeDetailActivity(Activity):
    """Detailed view of a mesh node."""

    def __init__(self, pub_key_hex, info, snr_history=None):
        super().__init__()
        self._pub_key_hex = pub_key_hex
        self._info = info
        self._snr_history = snr_history or []
        self.tab_order = ["content"]
        self.focus = "content"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self._build_display()

    def _build_display(self):
        name = self._info.get("name", "?")
        atype = self._info.get("adv_type_name", "?")
        lines = build_node_detail_lines(
            self._pub_key_hex, self._info, self._snr_history
        )

        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "Node Detail",
                "help": f"{name} ({atype})",
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
                "status": self._pub_key_hex[:16] + "...",
                "help": "UP/DOWN:scroll  ESC:back",
            }),
        }

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
            return
        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()
