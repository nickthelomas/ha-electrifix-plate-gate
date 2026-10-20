"""Frigate's login cookie must stay with Plate Gate: never HA's shared session (hacs/default#11383 review)."""
from unittest.mock import patch

from yarl import URL

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.electrifix_plate_gate.config_flow import _fetch_info
from custom_components.electrifix_plate_gate.const import DOMAIN

from .test_config_flow import INFO
from .test_runtime import OPTS, _setup


async def test_each_entry_has_its_own_session(hass, mqtt, freezer):
    first = await _setup(hass, freezer, unique_id="a")
    second = await _setup(hass, freezer, options={**OPTS, "device_entity": "cover.gate"}, unique_id="b")
    shared = async_get_clientsession(hass)
    s1 = hass.data[DOMAIN][first.entry_id].ops.client._session
    s2 = hass.data[DOMAIN][second.entry_id].ops.client._session
    assert s1 is not shared and s2 is not shared and s1 is not s2
    # Frigate is usually reached by IP address; aiohttp's default jar silently drops those cookies
    s1.cookie_jar.update_cookies({"frigate_token": "t"}, URL("http://192.0.2.5:8971/api/login"))
    assert len(s1.cookie_jar) == 1 and len(shared.cookie_jar) == 0


async def test_config_flow_uses_its_own_session(hass):
    seen = []

    async def fake_info(client):
        seen.append(client._session)
        return INFO

    with patch("custom_components.electrifix_plate_gate.config_flow.FrigateClient.async_info", fake_info):
        assert await _fetch_info(hass, "http://192.0.2.5:8971", "u", "p") is INFO
    assert seen and seen[0] is not async_get_clientsession(hass)
    assert seen[0].closed  # detached once the flow is done with it, HA's connector left alone
