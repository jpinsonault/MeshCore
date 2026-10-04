"""Tests for the SplitView component."""

import curses
import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication
from pyos.EventTypes import KeyStroke, ScrollChange
from pyos.Activity import Activity

from collector.split_view import (
    SplitView,
    make_split_view,
    handle_split_view_input,
    _start_stop,
)


# --- _start_stop ---

class TestStartStop:
    def test_small_list(self):
        assert _start_stop(0, 10, 3) == (0, 3)

    def test_exact_fit(self):
        assert _start_stop(0, 5, 5) == (0, 5)

    def test_topped_out(self):
        assert _start_stop(1, 10, 20) == (0, 10)

    def test_bottomed_out(self):
        assert _start_stop(18, 10, 20) == (10, 20)

    def test_middle(self):
        start, stop = _start_stop(10, 10, 20)
        assert start >= 0
        assert stop <= 20
        assert stop - start == 10
        assert start <= 10 < stop


# --- SplitView.display_state ---

class TestSplitViewDisplayState:
    def test_creates_valid_context(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(
            screen,
            left_items=["a", "b"],
            right_items=["x", "y", "z"],
            left_title="Left",
            right_title="Right",
        )
        assert ctx["left_items"] == ["a", "b"]
        assert ctx["right_items"] == ["x", "y", "z"]
        assert ctx["left_title"] == "Left"
        assert ctx["right_title"] == "Right"
        assert ctx["left_selected"] == 0
        assert ctx["right_selected"] == 0
        assert ctx["split_ratio"] == 0.5
        assert ctx["focused_panel"] == "left"
        assert ctx["focused"] is False
        assert "line_generator" in ctx
        assert "input_handler" in ctx
        assert "layout" in ctx

    def test_custom_split_ratio(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(screen, split_ratio=0.3)
        assert ctx["split_ratio"] == 0.3

    def test_custom_focused_panel(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(screen, focused_panel="right")
        assert ctx["focused_panel"] == "right"


# --- make_split_view ---

class TestMakeSplitView:
    def test_produces_correct_line_count(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(
            screen,
            left_items=["a", "b", "c"],
            right_items=["x", "y"],
        )
        printers = make_split_view(screen, ctx, 10)
        # 10 lines total: 1 header + 8 content + 1 footer
        assert len(printers) == 10

    def test_minimum_height(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(screen)
        printers = make_split_view(screen, ctx, 3)
        # 3 lines: 1 header + 1 content + 1 footer
        assert len(printers) == 3

    def test_renders_without_crash(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(
            screen,
            left_items=["packet 1", "packet 2"],
            right_items=["node A"],
            left_title="Packets",
            right_title="Nodes",
            focused=True,
        )
        printers = make_split_view(screen, ctx, 8)
        for i, printer in enumerate(printers):
            printer(screen, i)

    def test_renders_content(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(
            screen,
            left_items=["hello left"],
            right_items=["hello right"],
            left_title="Left",
            right_title="Right",
            focused=True,
        )
        printers = make_split_view(screen, ctx, 5)
        for i, printer in enumerate(printers):
            printer(screen, i)
        screen.assert_text_on_screen("hello left")
        screen.assert_text_on_screen("hello right")
        screen.assert_text_on_screen("Left")
        screen.assert_text_on_screen("Right")

    def test_empty_items(self):
        screen = MockScreen(24, 80)
        ctx = SplitView.display_state(screen, left_items=[], right_items=[])
        printers = make_split_view(screen, ctx, 5)
        assert len(printers) == 5
        for i, printer in enumerate(printers):
            printer(screen, i)


# --- handle_split_view_input ---

class TestSplitViewInput:
    def _make_ctx(self, **kwargs):
        screen = MockScreen(24, 80)
        defaults = {
            "left_items": ["a", "b", "c", "d", "e"],
            "right_items": ["x", "y", "z"],
            "left_selected": 0,
            "right_selected": 0,
            "split_ratio": 0.5,
            "focused_panel": "left",
            "focused": True,
        }
        defaults.update(kwargs)
        return defaults

    def test_down_scrolls_left(self):
        from queue import Queue
        ctx = self._make_ctx()
        q = Queue()
        event = KeyStroke(curses.KEY_DOWN)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["left_selected"] == 1

    def test_up_scrolls_left(self):
        from queue import Queue
        ctx = self._make_ctx(left_selected=2)
        q = Queue()
        event = KeyStroke(curses.KEY_UP)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["left_selected"] == 1

    def test_down_scrolls_right_when_focused(self):
        from queue import Queue
        ctx = self._make_ctx(focused_panel="right")
        q = Queue()
        event = KeyStroke(curses.KEY_DOWN)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["right_selected"] == 1
        assert ctx["left_selected"] == 0  # unchanged

    def test_up_clamps_to_zero(self):
        from queue import Queue
        ctx = self._make_ctx(left_selected=0)
        q = Queue()
        event = KeyStroke(curses.KEY_UP)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["left_selected"] == 0

    def test_down_clamps_to_max(self):
        from queue import Queue
        ctx = self._make_ctx(left_selected=4)  # last item
        q = Queue()
        event = KeyStroke(curses.KEY_DOWN)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["left_selected"] == 4

    def test_right_increases_ratio(self):
        from queue import Queue
        ctx = self._make_ctx(split_ratio=0.5)
        q = Queue()
        event = KeyStroke(curses.KEY_RIGHT)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["split_ratio"] == pytest.approx(0.55, abs=0.01)

    def test_left_decreases_ratio(self):
        from queue import Queue
        ctx = self._make_ctx(split_ratio=0.5)
        q = Queue()
        event = KeyStroke(curses.KEY_LEFT)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["split_ratio"] == pytest.approx(0.45, abs=0.01)

    def test_ratio_clamps_minimum(self):
        from queue import Queue
        ctx = self._make_ctx(split_ratio=0.15)
        q = Queue()
        event = KeyStroke(curses.KEY_LEFT)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["split_ratio"] == pytest.approx(0.15, abs=0.01)

    def test_ratio_clamps_maximum(self):
        from queue import Queue
        ctx = self._make_ctx(split_ratio=0.85)
        q = Queue()
        event = KeyStroke(curses.KEY_RIGHT)
        handle_split_view_input("split", ctx, event, q)
        assert ctx["split_ratio"] == pytest.approx(0.85, abs=0.01)

    def test_emits_scroll_change(self):
        from queue import Queue
        ctx = self._make_ctx()
        q = Queue()
        event = KeyStroke(curses.KEY_DOWN)
        handle_split_view_input("split", ctx, event, q)
        assert not q.empty()
        ev = q.get()
        assert isinstance(ev, ScrollChange)


# --- Integration test with Activity ---

class _SplitTestActivity(Activity):
    """Minimal activity that uses SplitView for integration testing."""

    def __init__(self):
        super().__init__()
        self.tab_order = ["split"]
        self.focus = "split"

    def on_start(self):
        self.application.subscribe(KeyStroke, self, self.on_key)
        self.application.subscribe(ScrollChange, self, self.on_scroll)
        self.display_state = {
            "split": SplitView.display_state(
                self.screen,
                left_items=["L1", "L2", "L3"],
                right_items=["R1", "R2"],
                left_title="Left",
                right_title="Right",
                focused=True,
            ),
        }

    def on_key(self, event):
        if event.key == Keys.TAB:
            panel = self.display_state["split"]["focused_panel"]
            self.display_state["split"]["focused_panel"] = "right" if panel == "left" else "left"
            self.refresh_screen()
            return
        self.delegate_to_focused(event)
        self.refresh_screen()

    def on_scroll(self, event):
        self.refresh_screen()


class TestSplitViewIntegration:
    def test_renders_in_activity(self, app, mock_screen):
        activity = _SplitTestActivity()
        app.start_activity(activity)
        mock_screen.assert_text_on_screen("Left")
        mock_screen.assert_text_on_screen("Right")
        mock_screen.assert_text_on_screen("L1")

    def test_tab_toggles_panel(self, app, mock_screen):
        activity = _SplitTestActivity()
        app.start_activity(activity)
        assert activity.display_state["split"]["focused_panel"] == "left"
        app.send_key(Keys.TAB)
        assert activity.display_state["split"]["focused_panel"] == "right"
        app.send_key(Keys.TAB)
        assert activity.display_state["split"]["focused_panel"] == "left"

    def test_scroll_in_activity(self, app, mock_screen):
        activity = _SplitTestActivity()
        app.start_activity(activity)
        assert activity.display_state["split"]["left_selected"] == 0
        app.send_key(curses.KEY_DOWN)
        assert activity.display_state["split"]["left_selected"] == 1
