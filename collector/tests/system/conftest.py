"""
System test fixtures — real hardware via CollectorCore.

Requires MESHCORE_PORT env var (e.g. /dev/cu.usbserial-0001).
Tests are auto-skipped when no device is connected.
"""

import os
import tempfile
import threading
import time
import uuid

import pytest

from collector.core import CollectorCore
from collector.crypto import Channel


# ---------------------------------------------------------------------------
# Auto-skip when no device is present
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    if os.environ.get("MESHCORE_PORT"):
        return
    skip = pytest.mark.skip(reason="MESHCORE_PORT not set — no hardware")
    for item in items:
        if "system" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Frame collector — thread-safe accumulator
# ---------------------------------------------------------------------------

class FrameCollector:
    """Thread-safe frame and channel-message accumulator with wait helpers."""

    def __init__(self):
        self._frames = []
        self._channel_messages = []
        self._lock = threading.Lock()
        self._frame_event = threading.Event()
        self._msg_event = threading.Event()

    def on_frame(self, frame):
        with self._lock:
            self._frames.append(frame)
        self._frame_event.set()

    def on_channel_message(self, msg):
        with self._lock:
            self._channel_messages.append(msg)
        self._msg_event.set()

    @property
    def frames(self):
        with self._lock:
            return list(self._frames)

    @property
    def channel_messages(self):
        with self._lock:
            return list(self._channel_messages)

    def clear(self):
        with self._lock:
            self._frames.clear()
            self._channel_messages.clear()
        self._frame_event.clear()
        self._msg_event.clear()

    def wait_for_frame(self, predicate, timeout=10):
        """Block until a frame matching *predicate* arrives. Returns it or None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for f in self._frames:
                    if predicate(f):
                        return f
            self._frame_event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._frame_event.wait(timeout=min(remaining, 0.25))
        return None

    def wait_for_channel_message(self, predicate, timeout=10):
        """Block until a channel message matching *predicate* arrives. Returns it or None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for m in self._channel_messages:
                    if predicate(m):
                        return m
            self._msg_event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._msg_event.wait(timeout=min(remaining, 0.25))
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def hw_session():
    """Session-scoped: connect to hardware once, yield (core, collector), tear down."""
    port = os.environ.get("MESHCORE_PORT")
    if not port:
        pytest.skip("MESHCORE_PORT not set")

    db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="systest_")
    os.close(db_fd)

    fc = FrameCollector()
    connected = threading.Event()

    core = CollectorCore(port=port, db_path=db_path)
    core.on_frame = fc.on_frame
    core.on_channel_message = fc.on_channel_message
    core.on_connected = lambda: connected.set()

    core.start()

    if not connected.wait(timeout=10):
        core.stop()
        os.unlink(db_path)
        pytest.fail("Hardware handshake timed out")

    yield core, fc

    core.stop()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture()
def hw(hw_session):
    """Function-scoped: resets core state and clears frame buffer between tests."""
    core, fc = hw_session

    # Reset core state safely between tests (no frames in flight right now)
    core._channels = []
    core._cracker = None
    time.sleep(0.1)

    fc.clear()

    if not core.is_running:
        pytest.fail("CollectorCore died during a previous test")

    return core, fc


@pytest.fixture()
def unique_tag():
    """Return a short unique string for test isolation."""
    return uuid.uuid4().hex[:12]
