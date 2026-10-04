"""Tests for the HelpActivity."""

import pytest

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.help_overlay import (
    HelpActivity,
    build_help_lines,
    HELP_SECTIONS,
)


class TestBuildHelpLines:
    def test_dashboard_context(self):
        lines = build_help_lines("dashboard")
        text = "\n".join(lines)
        assert "Dashboard" in text
        assert "TAB" in text
        assert "ENTER" in text
        assert "ESC" in text

    def test_channels_context(self):
        lines = build_help_lines("channels")
        text = "\n".join(lines)
        assert "Channel Browser" in text
        assert "TAB" in text
        assert "ENTER" in text

    def test_port_select_context(self):
        lines = build_help_lines("port_select")
        text = "\n".join(lines)
        assert "Port Selection" in text
        assert "ENTER" in text
        assert "refresh" in text.lower() or "Refresh" in text

    def test_debug_log_context(self):
        lines = build_help_lines("debug_log")
        text = "\n".join(lines)
        assert "Debug Log" in text
        assert "clear" in text.lower() or "Clear" in text

    def test_unknown_context_uses_default(self):
        lines = build_help_lines("nonexistent_screen")
        text = "\n".join(lines)
        assert "General" in text
        assert "ESC" in text

    def test_all_contexts_have_esc(self):
        for context in HELP_SECTIONS:
            lines = build_help_lines(context)
            text = "\n".join(lines)
            assert "ESC" in text, f"Context '{context}' missing ESC key"

    def test_lines_contain_close_instruction(self):
        lines = build_help_lines("dashboard")
        text = "\n".join(lines)
        assert "close" in text.lower()


class TestHelpActivity:
    def test_shows_title(self, app, mock_screen):
        app.start_activity(HelpActivity(context="dashboard"))
        mock_screen.assert_text_on_screen("Help")

    def test_shows_context_name(self, app, mock_screen):
        app.start_activity(HelpActivity(context="dashboard"))
        mock_screen.assert_text_on_screen("Dashboard")

    def test_shows_keybindings(self, app, mock_screen):
        app.start_activity(HelpActivity(context="dashboard"))
        mock_screen.assert_text_on_screen("TAB")
        mock_screen.assert_text_on_screen("ENTER")

    def test_esc_pops_activity(self, app, mock_screen):
        app.start_activity(HelpActivity(context="dashboard"))
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_question_mark_also_closes(self, app, mock_screen):
        """Pressing ? again should close the help."""
        app.start_activity(HelpActivity(context="dashboard"))
        assert app.activity_stack_depth() == 1
        app.send_key(ord("?"))
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    def test_channels_context(self, app, mock_screen):
        app.start_activity(HelpActivity(context="channels"))
        mock_screen.assert_text_on_screen("Channel Browser")

    def test_port_select_context(self, app, mock_screen):
        app.start_activity(HelpActivity(context="port_select"))
        mock_screen.assert_text_on_screen("Port Selection")

    def test_debug_log_context(self, app, mock_screen):
        app.start_activity(HelpActivity(context="debug_log"))
        mock_screen.assert_text_on_screen("Debug Log")

    def test_scroll_works(self, app, mock_screen):
        import curses
        activity = HelpActivity(context="dashboard")
        app.start_activity(activity)
        initial = activity.display_state["content"]["selected_index"]
        app.send_key(curses.KEY_DOWN)
        assert activity.display_state["content"]["selected_index"] == initial + 1

    def test_shows_bottom_bar(self, app, mock_screen):
        app.start_activity(HelpActivity(context="dashboard"))
        mock_screen.assert_text_on_screen("ESC")
