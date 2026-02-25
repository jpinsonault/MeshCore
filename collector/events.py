"""
MeshCore Collector — Custom event types for the TUI.

These events bridge the CollectorCore callbacks into the pyos event system
so Activities can subscribe and update their display.
"""


class CollectorConnected:
    """Fired when the collector successfully handshakes with the device."""
    pass


class CollectorDisconnected:
    """Fired when the serial connection is lost or stopped."""
    def __init__(self, reason=""):
        self.reason = reason


class CollectorFrame:
    """Fired for every frame received from the device."""
    def __init__(self, frame):
        self.frame = frame


class CollectorText:
    """Fired for text CLI output from the device."""
    def __init__(self, line):
        self.line = line


class CollectorError:
    """Fired on non-fatal errors."""
    def __init__(self, message):
        self.message = message


class ChannelMessage:
    """Fired when a group channel message is decoded."""
    def __init__(self, msg):
        self.msg = msg


class ChannelDiscovered:
    """Fired when the cracker discovers a new channel name."""
    def __init__(self, channel_name, decoded_count):
        self.channel_name = channel_name
        self.decoded_count = decoded_count
