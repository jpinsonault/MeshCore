"""Shared fixtures for collector tests."""

import tempfile
import pytest

from pyos.testing import MockScreen, HarnessApplication
from collector.store import CollectorStore


@pytest.fixture
def mock_screen():
    return MockScreen(24, 80)


@pytest.fixture
def app(mock_screen):
    application = HarnessApplication(mock_screen)
    application.setup()
    yield application
    application.teardown()


@pytest.fixture
def make_app():
    """Factory: app, screen = make_app(rows=24, cols=80)"""
    apps = []

    def _factory(rows=24, cols=80):
        screen = MockScreen(rows, cols)
        application = HarnessApplication(screen)
        application.setup()
        apps.append(application)
        return application, screen

    yield _factory
    for application in apps:
        application.teardown()


@pytest.fixture
def store():
    """Fresh SQLite store backed by a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        s = CollectorStore(f.name)
        s.open()
        yield s
        s.close()
