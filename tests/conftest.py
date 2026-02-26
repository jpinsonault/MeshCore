import pytest
from pyos.testing import MockScreen, HarnessApplication


@pytest.fixture
def mock_screen():
    return MockScreen(24, 80)


@pytest.fixture
def app(mock_screen):
    application = HarnessApplication(mock_screen)
    application.setup()
    yield application
    application.teardown()
