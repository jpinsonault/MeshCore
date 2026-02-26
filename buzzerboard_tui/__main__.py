"""BuzzerBoard entry point."""

import curses
import sys
from pyos.Application import Application
from .port_picker import PortPickerActivity


def main(stdscr):
    app = Application(stdscr)
    app.start(PortPickerActivity())


def main_cli():
    curses.wrapper(main)


if __name__ == "__main__":
    main_cli()
