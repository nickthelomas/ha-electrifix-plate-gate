"""Shared fixtures."""
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    yield


@pytest.fixture(autouse=True, scope="session")
def _warm_pycares_shutdown_thread():
    """pycares (aiohttp's DNS resolver) lazily starts a daemon thread the first time a
    channel is destroyed; HA's cleanup check would flag it on the first test. Start it
    before any test so it is part of the baseline."""
    try:
        import pycares

        pycares.Channel().close()
    except Exception:  # pragma: no cover - only matters for the check above
        pass
    yield


@pytest.fixture
async def mqtt(hass, mqtt_mock, mqtt_client_mock):
    """MQTT mock whose teardown closes the fake socket.

    HA's MQTT client cancels its 1-second housekeeping timer only on socket close, which
    the mocked paho client never sends, so fast tests would end with a lingering timer.
    """
    from unittest.mock import Mock

    yield mqtt_mock
    mqtt_client_mock.on_socket_close(mqtt_client_mock, None, Mock(fileno=Mock(return_value=-1)))
