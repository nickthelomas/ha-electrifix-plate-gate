"""Tests for the thin Frigate HTTP client."""
import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.electrifix_plate_gate.frigate_api import (
    FrigateAuthRequired,
    FrigateCannotConnect,
    FrigateClient,
)

CONFIG = {
    "mqtt": {"topic_prefix": "frigate"},
    "lpr": {"enabled": False},
    "cameras": {
        "driveway": {
            "detect": {"width": 2304, "height": 1296},
            "zones": {"driveway_approach": {}, "detect": {}},
            "lpr": {"enabled": True},
        },
        "yard": {"detect": {"width": 1280, "height": 720}, "zones": {}},
    },
}


async def test_info(hass, aioclient_mock):
    aioclient_mock.get("http://f:5000/api/version", text="0.18.0-abc")
    aioclient_mock.get("http://f:5000/api/config", json=CONFIG)
    info = await FrigateClient(async_get_clientsession(hass), "http://f:5000/").async_info()
    assert info.version == "0.18.0-abc" and info.topic_prefix == "frigate"
    assert info.lpr_enabled is False
    assert info.cameras["driveway"].zones == ["driveway_approach", "detect"]
    assert info.cameras["driveway"].detect_width == 2304
    assert info.cameras["driveway"].detect_height == 1296
    assert info.cameras["driveway"].lpr_enabled is True
    assert info.cameras["yard"].lpr_enabled is False


async def test_missing_mqtt_block_defaults_prefix(hass, aioclient_mock):
    aioclient_mock.get("http://f:5000/api/version", text="0.16.1")
    aioclient_mock.get("http://f:5000/api/config", json={"cameras": {"c": {}}})
    info = await FrigateClient(async_get_clientsession(hass), "http://f:5000").async_info()
    assert info.topic_prefix == "frigate" and info.cameras["c"].detect_width == 0


async def test_auth_required(hass, aioclient_mock):
    aioclient_mock.get("http://f:8971/api/version", status=401)
    with pytest.raises(FrigateAuthRequired):
        await FrigateClient(async_get_clientsession(hass), "http://f:8971").async_info()


async def test_login_then_config(hass, aioclient_mock):
    aioclient_mock.get("http://f:8971/api/version", status=401)
    aioclient_mock.post("http://f:8971/api/login", status=200)
    aioclient_mock.get("http://f:8971/api/config", json=CONFIG)
    client = FrigateClient(async_get_clientsession(hass), "http://f:8971", "u", "p")
    info = await client.async_info()
    assert info.version == "unknown" and "driveway" in info.cameras
    assert aioclient_mock.mock_calls[1][2] == {"user": "u", "password": "p"}


async def test_bad_login_is_auth_required(hass, aioclient_mock):
    aioclient_mock.get("http://f:8971/api/version", status=401)
    aioclient_mock.post("http://f:8971/api/login", status=401)
    with pytest.raises(FrigateAuthRequired):
        await FrigateClient(async_get_clientsession(hass), "http://f:8971", "u", "wrong").async_info()


async def test_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get("http://f:5000/api/version", exc=aiohttp.ClientConnectionError())
    with pytest.raises(FrigateCannotConnect):
        await FrigateClient(async_get_clientsession(hass), "http://f:5000").async_info()


async def test_non_json_config_is_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get("http://f:5000/api/version", text="0.18.0")
    aioclient_mock.get("http://f:5000/api/config", text="<html>not frigate</html>")
    with pytest.raises(FrigateCannotConnect):
        await FrigateClient(async_get_clientsession(hass), "http://f:5000").async_info()


# ---- Phase 2: write + stats endpoints ------------------------------------------------
from custom_components.electrifix_plate_gate.frigate_api import FrigateAdminRequired, FrigateConfigInvalid  # noqa: E402

RAW = "mqtt:\n  host: broker\ncameras:\n  driveway: {}\n"


async def test_raw_config_returns_text(hass, aioclient_mock):
    aioclient_mock.get("http://f:5000/api/config/raw", text=RAW)
    assert await FrigateClient(async_get_clientsession(hass), "http://f:5000").async_raw_config() == RAW


async def test_save_config_posts_text_with_restart_flag(hass, aioclient_mock):
    aioclient_mock.post("http://f:5000/api/config/save?save_option=restart", json={"success": True, "message": "saved"})
    aioclient_mock.post("http://f:5000/api/config/save?save_option=saveonly", json={"success": True, "message": "saved"})
    c = FrigateClient(async_get_clientsession(hass), "http://f:5000")
    await c.async_save_config(RAW, restart=True)
    await c.async_save_config(RAW, restart=False)
    assert aioclient_mock.call_count == 2
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert str(url).endswith("save_option=restart") and data == RAW
    assert headers["Content-Type"].startswith("text/plain")
    assert str(aioclient_mock.mock_calls[1][1]).endswith("save_option=saveonly")


async def test_save_config_invalid_raises_with_message(hass, aioclient_mock):
    aioclient_mock.post(
        "http://f:5000/api/config/save?save_option=restart",
        status=400, json={"success": False, "message": "Your configuration is invalid.\nLine 12: extra field"},
    )
    with pytest.raises(FrigateConfigInvalid) as e:
        await FrigateClient(async_get_clientsession(hass), "http://f:5000").async_save_config(RAW, restart=True)
    assert "Line 12" in str(e.value)


async def test_admin_routes_need_admin(hass, aioclient_mock):
    aioclient_mock.get("http://f:8971/api/config/raw", status=401)
    with pytest.raises(FrigateAdminRequired):
        await FrigateClient(async_get_clientsession(hass), "http://f:8971").async_raw_config()
    assert issubclass(FrigateAdminRequired, FrigateAuthRequired)


async def test_admin_route_logs_in_then_retries(hass, aioclient_mock):
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    calls = {"n": 0}

    async def raw(method, url, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return AiohttpClientMockResponse(method, url, status=401)
        return AiohttpClientMockResponse(method, url, text=RAW)

    aioclient_mock.get("http://f:8971/api/config/raw", side_effect=raw)
    aioclient_mock.post("http://f:8971/api/login", status=200)
    c = FrigateClient(async_get_clientsession(hass), "http://f:8971", "admin", "pw")
    assert await c.async_raw_config() == RAW
    assert calls["n"] == 2


async def test_restart_and_stats_and_config(hass, aioclient_mock):
    aioclient_mock.post("http://f:5000/api/restart", json={"success": True, "message": "Restarting"})
    aioclient_mock.get("http://f:5000/api/stats", json={"detectors": {"ov": {"inference_speed": 42.4}}, "service": {"uptime": 10, "version": "0.18.0"}})
    aioclient_mock.get("http://f:5000/api/config", json=CONFIG)
    c = FrigateClient(async_get_clientsession(hass), "http://f:5000")
    await c.async_restart()
    assert (await c.async_stats())["detectors"]["ov"]["inference_speed"] == 42.4
    assert "driveway" in (await c.async_config())["cameras"]


# ---- version policy: tested on 0.18 only, warn (never block) on anything else ----------
from custom_components.electrifix_plate_gate.frigate_api import TESTED_MINOR, version_note  # noqa: E402


@pytest.mark.parametrize("version,expect_note", [
    ("0.18.0-abc123", False), ("0.18.1", False), ("0.17.2-def", True), ("0.19.0-beta1", True), ("unknown", True), ("", True),
])
def test_version_note(version, expect_note):
    note = version_note(version)
    assert (note != "") is expect_note
    if expect_note and version:
        assert "0.18" in note and version.split("-")[0] in note or "unknown" in note.lower()
    assert TESTED_MINOR == "0.18"
