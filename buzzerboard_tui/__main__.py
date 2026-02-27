"""BuzzerBoard entry point."""

from pyos.Application import Application
from .autoconnect import AutoConnectActivity


def main_cli():
    app = Application(enable_kitty=True)
    app.start(AutoConnectActivity())


if __name__ == "__main__":
    main_cli()
