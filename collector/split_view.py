"""
SplitView — side-by-side scrollable panel component for pyos TUI.

Renders two panels separated by a vertical line with header/footer.
Supports independent scrolling, focus toggling, and resizable split.
"""

import curses
from functools import partial

import sys
import os
sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.EventTypes import ScrollChange

# Box-drawing characters
H_LINE = "\u2500"  # ─
V_LINE = "\u2502"  # │
T_DOWN = "\u252c"  # ┬
T_UP = "\u2534"  # ┴


def _start_stop(index, window_size, list_size):
    """Calculate start/stop indices for visible items in a scrollable window."""
    if list_size <= window_size:
        return 0, list_size
    up = window_size // 2
    down = window_size // 2 - (window_size % 2 == 0)
    if index - up < 0:
        return 0, min(window_size, list_size)
    if index + down >= list_size:
        return max(0, list_size - window_size), list_size
    return max(0, index - up), min(list_size, index + down + 1)


def _print_split_header(left_title, right_title, split_col, screen, y):
    """Draw: ─── Left Title ───┬─── Right Title ───"""
    _, num_cols = screen.getmaxyx()
    left_text = f"{H_LINE * 3} {left_title} "
    left_pad = H_LINE * max(0, split_col - len(left_text))
    left_part = (left_text + left_pad)[:split_col]

    right_avail = num_cols - split_col - 1
    right_text = f"{H_LINE * 3} {right_title} "
    right_pad = H_LINE * max(0, right_avail - len(right_text))
    right_part = (right_text + right_pad)[:right_avail]

    line = left_part + T_DOWN + right_part
    try:
        screen.addstr(y, 0, line[:num_cols - 1], curses.A_NORMAL)
    except curses.error:
        pass


def _print_split_footer(split_col, screen, y):
    """Draw: ──────────────┴──────────────"""
    _, num_cols = screen.getmaxyx()
    left_part = H_LINE * split_col
    right_avail = num_cols - split_col - 1
    right_part = H_LINE * right_avail
    line = left_part + T_UP + right_part
    try:
        screen.addstr(y, 0, line[:num_cols - 1], curses.A_NORMAL)
    except curses.error:
        pass


def _print_split_row(left_text, right_text, split_col,
                     left_hl, right_hl, left_dim, right_dim, screen, y):
    """Draw one content row: left_text│right_text with highlights."""
    _, num_cols = screen.getmaxyx()
    right_width = max(0, num_cols - split_col - 1)

    lt = str(left_text)[:split_col].ljust(split_col)
    rt = str(right_text)[:right_width]

    left_attr = curses.A_NORMAL
    if left_hl:
        left_attr = curses.A_REVERSE
    elif left_dim:
        left_attr = curses.A_DIM | curses.A_REVERSE

    right_attr = curses.A_NORMAL
    if right_hl:
        right_attr = curses.A_REVERSE
    elif right_dim:
        right_attr = curses.A_DIM | curses.A_REVERSE

    try:
        screen.addstr(y, 0, lt, left_attr)
        screen.addstr(y, split_col, V_LINE, curses.A_NORMAL)
        if rt:
            screen.addstr(y, split_col + 1, rt, right_attr)
    except curses.error:
        pass


def make_split_view(screen, context, remaining_height):
    """Line generator: produces per-row printer callables for the SplitView."""
    _, num_cols = screen.getmaxyx()

    split_ratio = context.get("split_ratio", 0.5)
    left_items = context.get("left_items", [])
    right_items = context.get("right_items", [])
    left_selected = context.get("left_selected", 0)
    right_selected = context.get("right_selected", 0)
    left_title = context.get("left_title", "")
    right_title = context.get("right_title", "")
    focused_panel = context.get("focused_panel", "left")
    is_focused = context.get("focused", False)

    split_col = max(4, min(num_cols - 5, int(num_cols * split_ratio)))
    content_height = max(1, remaining_height - 2)

    printers = []

    # Header
    printers.append(partial(_print_split_header, left_title, right_title, split_col))

    # Content rows
    left_start, left_stop = _start_stop(left_selected, content_height, len(left_items))
    right_start, right_stop = _start_stop(right_selected, content_height, len(right_items))

    for row in range(content_height):
        left_idx = left_start + row
        right_idx = right_start + row

        lt = left_items[left_idx] if left_idx < left_stop else ""
        rt = right_items[right_idx] if right_idx < right_stop else ""

        left_hl = (is_focused and focused_panel == "left"
                   and left_idx == left_selected and left_idx < len(left_items))
        right_hl = (is_focused and focused_panel == "right"
                    and right_idx == right_selected and right_idx < len(right_items))
        left_dim = (is_focused and focused_panel != "left"
                    and left_idx == left_selected and left_idx < len(left_items))
        right_dim = (is_focused and focused_panel != "right"
                     and right_idx == right_selected and right_idx < len(right_items))

        printers.append(partial(
            _print_split_row, lt, rt, split_col,
            left_hl, right_hl, left_dim, right_dim,
        ))

    # Footer
    printers.append(partial(_print_split_footer, split_col))

    return printers


def handle_split_view_input(element, context, event, queue):
    """Input handler for SplitView: UP/DOWN scroll, LEFT/RIGHT resize."""
    key = event.key
    panel = context.get("focused_panel", "left")

    if panel == "left":
        items = context.get("left_items", [])
        sel_key = "left_selected"
    else:
        items = context.get("right_items", [])
        sel_key = "right_selected"

    idx = context.get(sel_key, 0)

    if key == curses.KEY_UP:
        context[sel_key] = max(0, idx - 1)
        queue.put(ScrollChange(element))
    elif key == curses.KEY_DOWN:
        context[sel_key] = min(max(0, len(items) - 1), idx + 1)
        queue.put(ScrollChange(element))
    elif key == curses.KEY_LEFT:
        ratio = context.get("split_ratio", 0.5)
        context["split_ratio"] = max(0.15, ratio - 0.05)
        queue.put(ScrollChange(element))
    elif key == curses.KEY_RIGHT:
        ratio = context.get("split_ratio", 0.5)
        context["split_ratio"] = min(0.85, ratio + 0.05)
        queue.put(ScrollChange(element))


class SplitView:
    """Side-by-side scrollable panel component."""

    @staticmethod
    def layout(min_height=7, flex=1):
        return {"flex": flex, "min_height": min_height}

    @staticmethod
    def display_state(screen, left_items=None, right_items=None,
                      left_title="", right_title="",
                      left_selected=0, right_selected=0,
                      split_ratio=0.5, focused_panel="left",
                      focused=False, min_height=7, flex=1):
        return {
            "left_items": list(left_items or []),
            "right_items": list(right_items or []),
            "left_title": left_title,
            "right_title": right_title,
            "left_selected": int(left_selected),
            "right_selected": int(right_selected),
            "split_ratio": float(split_ratio),
            "focused_panel": str(focused_panel),
            "focused": bool(focused),
            "layout": SplitView.layout(min_height, flex),
            "line_generator": partial(make_split_view, screen),
            "input_handler": handle_split_view_input,
        }
