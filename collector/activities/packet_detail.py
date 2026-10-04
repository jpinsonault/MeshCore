"""
Packet Detail Activity — drill-down view of a single captured packet.

Shows hex dump, decoded header fields, route/payload types, signal info,
and path bytes. Accessed by pressing ENTER on a packet in the dashboard.
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

from ..protocol import FRAME_TYPE_NAMES, PAYLOAD_TYPES, ROUTE_TYPES


def _fmt_time(ts):
    if not ts:
        return "---"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return "---"


def _hex_dump(data, bytes_per_line=16):
    """Format raw bytes as an xxd-style hex dump.

    Returns a list of strings like:
      0000  C0 D0 01 02 ...  |....abcd|
    """
    if not data:
        return ["  (no raw data)"]
    lines = []
    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset:offset + bytes_per_line]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        # Pad hex part for alignment
        hex_padded = hex_part.ljust(bytes_per_line * 3 - 1)
        lines.append(f"  {offset:04X}  {hex_padded}  |{ascii_part}|")
    return lines


def build_packet_detail_lines(frame):
    """Build the full detail view lines from a frame dict."""
    parsed = frame.get("parsed", {})
    ft = frame.get("type", 0)
    ft_name = FRAME_TYPE_NAMES.get(ft, f"UNKNOWN(0x{ft:02X})")
    received_at = frame.get("received_at")

    lines = []
    lines.append(f"  Frame Type:    {ft_name} (0x{ft:02X})")
    lines.append(f"  Received:      {_fmt_time(received_at)}")
    lines.append("")

    # Route and payload
    route_type = parsed.get("route_type")
    payload_type = parsed.get("payload_type")
    if route_type is not None:
        route_name = ROUTE_TYPES.get(route_type, f"?{route_type}")
        lines.append(f"  Route Type:    {route_name} ({route_type})")
    if payload_type is not None:
        payload_name = PAYLOAD_TYPES.get(payload_type, f"?0x{payload_type:X}")
        lines.append(f"  Payload Type:  {payload_name} (0x{payload_type:02X})")

    # Header byte analysis
    header = parsed.get("header")
    if header is not None:
        version = (header >> 6) & 0x03
        lines.append(f"  Header Byte:   0x{header:02X} (version={version})")

    lines.append("")

    # Signal info (RX only)
    snr = parsed.get("snr")
    rssi = parsed.get("rssi")
    if snr is not None:
        lines.append(f"  SNR:           {snr:+.1f} dB")
    if rssi is not None:
        lines.append(f"  RSSI:          {rssi} dBm")

    # Length
    raw_len = parsed.get("raw_len")
    if raw_len is not None:
        lines.append(f"  Raw Length:    {raw_len} bytes")

    lines.append("")

    # Hex dump
    raw = parsed.get("raw")
    if isinstance(raw, (bytes, bytearray)):
        lines.append("  --- Hex Dump ---")
        lines.extend(_hex_dump(raw))
    elif isinstance(raw, str):
        # raw might be stored as hex string
        try:
            raw_bytes = bytes.fromhex(raw)
            lines.append("  --- Hex Dump ---")
            lines.extend(_hex_dump(raw_bytes))
        except ValueError:
            pass

    return lines


class PacketDetailActivity(Activity):
    """Detailed view of a single captured packet."""

    def __init__(self, frame):
        super().__init__()
        self._frame = frame
        self.tab_order = ["content"]
        self.focus = "content"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self._build_display()

    def _build_display(self):
        parsed = self._frame.get("parsed", {})
        direction = "RX" if self._frame.get("type") == 0xD0 else "TX"
        ptype = parsed.get("payload_name", "?")
        route = parsed.get("route_name", "?")
        title = f"{direction} {route} {ptype}"

        lines = build_packet_detail_lines(self._frame)

        self.display_state = {
            "top": TopBar.display_state(items={
                "title": "Packet Detail",
                "help": title,
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
                "status": f"{parsed.get('raw_len', '?')} bytes",
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
