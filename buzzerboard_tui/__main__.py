"""BuzzerBoard entry point."""

from pyos.Application import Application
from .port_picker import PortPickerActivity


def main_cli():
    app = Application()
    app.start(PortPickerActivity())


if __name__ == "__main__":
    main_cli()
