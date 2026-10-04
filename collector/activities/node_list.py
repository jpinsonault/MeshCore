"""
Node List Activity — scrollable list of mesh nodes filtered by type.

Reusable screen for viewing repeaters, room servers, or all nodes.
ENTER opens NodeDetailActivity for the selected node. Subscribes to
CollectorFrame for live updates when new advertisements arrive.
"""

import time

from pyos.Activity import Activity
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.input_handlers import handle_scroll_list_input
from pyos import Keys
from pyos.printers.TopBar import TopBar
from pyos.printers.BottomBar import BottomBar
from pyos.printers.ScrollList import ScrollList

from ..events import CollectorFrame
from ..protocol import FRAME_TYPE_ADVERTISEMENT


def _fmt_ago(ts):
    """Format a timestamp as relative time."""
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


def build_node_list_items(nodes):
    """Build display lines for a list of node dicts."""
    if not nodes:
        return ["  (no nodes found)"]
    items = []
    for n in nodes:
        name = (n.get("name") or "?")[:16]
        atype = (n.get("adv_type_name") or "?")[:10]
        count = n.get("advert_count", 0)
        last_seen = n.get("last_seen")
        ago = _fmt_ago(last_seen)
        pk = n.get("pub_key_hex", "")[:8]
        lat = n.get("lat")
        lon = n.get("lon")
        loc = f"{lat:.2f},{lon:.2f}" if lat is not None and lon is not None else ""
        items.append(f"  {name:<16s} {atype:<10s} {loc:<14s} x{count:<3d} {ago:<8s} [{pk}..]")
    return items


class NodeListActivity(Activity):
    """Scrollable list of mesh nodes, optionally filtered by adv_type."""

    def __init__(self, adv_type=None, title=None):
        super().__init__()
        self._adv_type = adv_type
        if title is None:
            if adv_type == 2:
                title = "Repeaters"
            elif adv_type == 3:
                title = "Room Servers"
            else:
                title = "All Nodes"
        self._title = title
        self._nodes = []
        self.tab_order = ["content"]
        self.focus = "content"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key_stroke)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.application.subscribe(CollectorFrame, self, self._on_frame)
        self._load_nodes()
        self._build_display()

    def _get_store(self):
        try:
            svc = self.application.service("collector")
            return svc.store
        except (KeyError, RuntimeError):
            return None

    def _load_nodes(self):
        store = self._get_store()
        if not store:
            self._nodes = []
            return
        if self._adv_type is not None:
            self._nodes = store.get_nodes_by_type(self._adv_type)
        else:
            self._nodes = store.get_nodes()

    def _build_display(self):
        items = build_node_list_items(self._nodes)
        self.display_state = {
            "top": TopBar.display_state(items={
                "title": self._title,
                "help": f"{len(self._nodes)} nodes",
            }),
            "content": ScrollList.display_state(
                self.screen,
                items=items,
                selected_index=0,
                focused=True,
                input_handler=handle_scroll_list_input,
                min_height=5,
                flex=1,
            ),
            "bottom": BottomBar.display_state(items={
                "status": f"{len(self._nodes)} nodes",
                "help": "ENTER:detail  r:refresh  ESC:back",
            }),
        }

    def _update_display(self):
        items = build_node_list_items(self._nodes)
        content = self.display_state["content"]
        content["items"] = items
        # Clamp selection
        max_idx = max(0, len(self._nodes) - 1)
        if content["selected_index"] > max_idx:
            content["selected_index"] = max_idx
        self.display_state["top"]["items"]["help"] = f"{len(self._nodes)} nodes"
        self.display_state["bottom"]["items"]["status"] = f"{len(self._nodes)} nodes"
        self.refresh_screen()

    def on_key_stroke(self, event: KeyStroke):
        if event.key == Keys.ESC:
            self.application.pop_activity()
            return

        if event.key == ord("r") or event.key == ord("R"):
            self._load_nodes()
            self._update_display()
            return

        if event.key == ord("?"):
            from .help_overlay import HelpActivity
            self.application.segue_to(HelpActivity(context="node_list"))
            return

        if event.key == Keys.ENTER:
            self._open_detail()
            return

        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event: ScrollChange):
        self.refresh_screen()

    def _on_frame(self, event):
        if event.frame["type"] == FRAME_TYPE_ADVERTISEMENT:
            self._load_nodes()
            self._update_display()

    def _open_detail(self):
        if not self._nodes:
            return
        idx = self.display_state["content"]["selected_index"]
        if idx >= len(self._nodes):
            return
        node = self._nodes[idx]
        pk = node.get("pub_key_hex", "")

        # Load SNR history from store
        snr_history = []
        store = self._get_store()
        if store:
            snr_history = store.get_advertisement_snr_history(pk)

        from .node_detail import NodeDetailActivity
        info = {
            "name": node.get("name"),
            "adv_type": node.get("adv_type"),
            "adv_type_name": node.get("adv_type_name"),
            "lat": node.get("lat"),
            "lon": node.get("lon"),
            "first_seen": node.get("first_seen"),
            "last_seen": node.get("last_seen"),
            "count": node.get("advert_count", 0),
        }
        self.application.segue_to(NodeDetailActivity(pk, info, snr_history))
