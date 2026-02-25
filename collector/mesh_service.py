"""
MeshCore Collector — pyos Service wrapper.

Wraps CollectorCore as a pyos Service, bridging callbacks into pyos events
so Activities can subscribe to CollectorFrame, CollectorConnected, etc.
"""

import sys
import os
sys.path.insert(0, os.path.expanduser("~/repos/pyos"))

from pyos.Service import Service

from .core import CollectorCore
from .events import (
    ChannelDiscovered,
    ChannelMessage,
    CollectorConnected,
    CollectorDisconnected,
    CollectorError,
    CollectorFrame,
    CollectorText,
)


class MeshCollectorService(Service):
    """pyos Service that runs the collector on a background thread.

    Register with:
        app.register_service("collector", MeshCollectorService(port, baud, db_path))
    """

    def __init__(self, port=None, baud=115200, db_path="collector.db"):
        super().__init__()
        self._core = CollectorCore(port=port, baud=baud, db_path=db_path)

    @property
    def core(self):
        return self._core

    @property
    def port(self):
        return self._core.port

    @port.setter
    def port(self, value):
        self._core.port = value

    @property
    def store(self):
        return self._core.store

    def on_start(self):
        """Called on a background thread by the pyos framework.

        Must return quickly so pyos can transition the service to RUNNING.
        The core runs its blocking loop on its own daemon thread.
        """
        self._core.on_frame = self._on_frame
        self._core.on_text = self._on_text
        self._core.on_connected = self._on_connected
        self._core.on_disconnected = self._on_disconnected
        self._core.on_error = self._on_error
        self._core.on_channel_message = self._on_channel_message
        self._core.on_channel_discovered = self._on_channel_discovered

        # Load channels from config
        try:
            from .config import load_config, load_channels
            config = load_config()
            self._core.set_channels(load_channels(config))
        except Exception:
            pass

        # Start core on its own daemon thread (non-blocking)
        self._core.start()

    def on_stop(self):
        """Called on a background thread by the pyos framework."""
        self._core.stop()

    def _on_frame(self, frame):
        self.dispatch_event(CollectorFrame(frame))

    def _on_text(self, line):
        self.dispatch_event(CollectorText(line))

    def _on_connected(self):
        self.dispatch_event(CollectorConnected())

    def _on_disconnected(self, reason):
        self.dispatch_event(CollectorDisconnected(reason))

    def _on_channel_message(self, msg):
        self.dispatch_event(ChannelMessage(msg))

    def _on_channel_discovered(self, name, decoded_count):
        self.dispatch_event(ChannelDiscovered(name, decoded_count))

    def _on_error(self, msg):
        self.dispatch_event(CollectorError(msg))
