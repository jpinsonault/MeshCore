"""Entry point: python -m collector.webapp [--db PATH | --port PORT]."""

from .server import main

if __name__ == "__main__":
    main()
