"""Tests for the PortSelectActivity."""

import curses
import pytest
from unittest.mock import patch

from pyos import Keys
from pyos.testing import MockScreen, HarnessApplication

from collector.activities.port_select import PortSelectActivity


FAKE_PORTS = [
    {"device": "/dev/ttyUSB0", "description": "USB Serial", "hwid": "USB VID:1234", "name": "ttyUSB0"},
    {"device": "/dev/ttyUSB1", "description": "Heltec V3", "hwid": "USB VID:5678", "name": "ttyUSB1"},
    {"device": "/dev/ttyACM0", "description": "n/a", "hwid": "USB VID:9ABC", "name": "ttyACM0"},
]

FAKE_CONFIG = {"port": "/dev/ttyUSB1", "baud": 115200, "db_path": "collector.db"}


class TestPortSelectRendering:
    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_shows_title(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        mock_screen.assert_text_on_screen("MeshCore Collector")
        mock_screen.assert_text_on_screen("Serial Port Selection")

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_shows_ports(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        mock_screen.assert_text_on_screen("/dev/ttyUSB0")
        mock_screen.assert_text_on_screen("/dev/ttyUSB1")
        mock_screen.assert_text_on_screen("/dev/ttyACM0")

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_shows_descriptions(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        mock_screen.assert_text_on_screen("USB Serial")
        mock_screen.assert_text_on_screen("Heltec V3")

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_marks_saved_port(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        # The saved port /dev/ttyUSB1 should have a * marker
        mock_screen.assert_text_on_screen("*")

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_preselects_saved_port(self, mock_save, mock_config, mock_ports, app, mock_screen):
        activity = PortSelectActivity()
        app.start_activity(activity)
        # ttyUSB1 is index 1 (saved port), should be pre-selected
        idx = activity.display_state["ports"]["selected_index"]
        assert idx == 1

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_shows_port_count(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        mock_screen.assert_text_on_screen("3 port(s) found")


class TestPortSelectNoDevices:
    @patch("collector.activities.port_select.list_serial_ports", return_value=[])
    @patch("collector.activities.port_select.load_config", return_value={"port": None, "baud": 115200})
    @patch("collector.activities.port_select.save_config")
    def test_shows_no_ports_message(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        mock_screen.assert_text_on_screen("no serial ports found")

    @patch("collector.activities.port_select.list_serial_ports", return_value=[])
    @patch("collector.activities.port_select.load_config", return_value={"port": None, "baud": 115200})
    @patch("collector.activities.port_select.save_config")
    def test_shows_refresh_hint(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        mock_screen.assert_text_on_screen("refresh")


class TestPortSelectNavigation:
    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_scroll_down(self, mock_save, mock_config, mock_ports, app, mock_screen):
        activity = PortSelectActivity()
        app.start_activity(activity)
        # Pre-selected at index 1 (ttyUSB1)
        app.send_key(curses.KEY_DOWN)
        assert activity.display_state["ports"]["selected_index"] == 2

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_scroll_up(self, mock_save, mock_config, mock_ports, app, mock_screen):
        activity = PortSelectActivity()
        app.start_activity(activity)
        app.send_key(curses.KEY_UP)
        assert activity.display_state["ports"]["selected_index"] == 0

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_esc_pops_activity(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        assert app.activity_stack_depth() == 1
        app.send_key(Keys.ESC)
        app.flush_stop_events()
        assert app.activity_stack_depth() == 0

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_refresh_rescans_ports(self, mock_save, mock_config, mock_ports, app, mock_screen):
        activity = PortSelectActivity()
        app.start_activity(activity)
        # Change the mock to return different ports
        mock_ports.return_value = [
            {"device": "/dev/ttyNEW", "description": "New Device", "hwid": "USB", "name": "ttyNEW"},
        ]
        app.send_key(ord("r"))
        mock_screen.assert_text_on_screen("/dev/ttyNEW")
        mock_screen.assert_text_on_screen("1 port(s) found")

    @patch("collector.activities.port_select.list_serial_ports", return_value=FAKE_PORTS)
    @patch("collector.activities.port_select.load_config", return_value=FAKE_CONFIG)
    @patch("collector.activities.port_select.save_config")
    def test_bottom_bar_updates_on_scroll(self, mock_save, mock_config, mock_ports, app, mock_screen):
        app.start_activity(PortSelectActivity())
        app.send_key(curses.KEY_UP)  # Move to ttyUSB0
        # Bottom bar should show hwid info
        mock_screen.assert_text_on_screen("VID:1234")
