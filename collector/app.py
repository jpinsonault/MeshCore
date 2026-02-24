#!/usr/bin/env python3
"""
MeshCore Collector — TUI Application.

A curses-based dashboard for monitoring mesh network traffic from a
collector-enabled repeater node.

Usage:
    python -m collector.app             # interactive port selection
    python -m collector.app --port /dev/cu.usbserial-0001  # skip to dashboard
"""

import argparse
import curses
import sys
import os

sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Application import Application

from .activities.port_select import PortSelectActivity
from .activities.dashboard import DashboardActivity
from .config import load_config


class CollectorApp(Application):
    def __init__(self, curses_screen, port=None, baud=115200):
        super().__init__(curses_screen)
        self.log_filename = "collector_tui.log"
        self._port = port
        self._baud = baud

    def on_start(self):
        pass

    def run(self):
        if self._port:
            # Skip port selection, go directly to dashboard
            self.start(DashboardActivity(port=self._port, baud=self._baud))
        else:
            self.start(PortSelectActivity())


def main():
    parser = argparse.ArgumentParser(description="MeshCore Collector TUI")
    parser.add_argument("--port", default=None, help="Serial port (skip port selection)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    args = parser.parse_args()

    def curses_main(stdscr):
        app = CollectorApp(stdscr, port=args.port, baud=args.baud)
        app.run()

    curses.wrapper(curses_main)


if __name__ == "__main__":
    main()
