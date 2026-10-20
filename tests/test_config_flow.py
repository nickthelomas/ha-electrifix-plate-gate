"""Tests for the guided setup (config flow) and the options flow."""
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrifix_plate_gate.const import DOMAIN
from custom_components.electrifix_plate_gate.frigate_api import (
    CameraInfo,
    FrigateAuthRequired,
    FrigateCannotConnect,
    FrigateInfo,
)

INFO = FrigateInfo(
    "0.18.0", "frigate", False,
    {"driveway": CameraInfo("driveway", 2304, 1296, ["driveway_approach"], False)},
)
INFO_PATH = "custom_components.electrifix_plate_gate.config_flow.FrigateClient.async_info"
PLATES_OK = {"people_text": "Alex: XO520", "near_misses": "LO120", "match_distance": 0, "confirmed": True}
DEVICE_OK = {
    "device_entity": "cover.garage", "open_on_arrival": True, "close_on_leaving": True,
    "cooldown_seconds": 180, "require_moving": False, "auto_close_minutes": 5, "zones": [],
}


async def _start(hass):
    MockConfigEntry(domain="mqtt", data={}).add_to_hass(hass)
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def _to_plates(hass):
    r = await _start(hass)
    assert r["step_id"] == "user"
    with patch(INFO_PATH, return_value=INFO):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": "http://f:5000"})
    assert r["step_id"] == "camera"
    assert "2304x1296" in r["description_placeholders"]["cameras"]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"camera": "driveway"})
    assert r["step_id"] == "plates"
    return r


async def test_full_flow(hass):
    r = await _to_plates(hass)
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"],
        {"people_text": "Alex: XO520", "near_misses": "LO120, XO540", "match_distance": 1, "confirmed": False},
    )
    assert r["step_id"] == "plates" and r["errors"] == {"base": "confirm_verdicts"}
    assert "XO540 would ALSO" in r["description_placeholders"]["verdicts"]
    # changing the values and ticking the box in one go re-shows the verdicts for the NEW values
    r = await hass.config_entries.flow.async_configure(r["flow_id"], PLATES_OK)
    assert r["step_id"] == "plates" and r["errors"] == {"base": "confirm_verdicts"}
    assert "XO540" not in r["description_placeholders"]["verdicts"]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], PLATES_OK)
    assert r["step_id"] == "device"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], DEVICE_OK)
    assert r["step_id"] == "frigate_yaml"
    assert "known_plates" in r["description_placeholders"]["yaml"]
    assert r["description_placeholders"]["detect"] == "2304x1296"
    with patch("custom_components.electrifix_plate_gate.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY
    assert r["title"] == "Plate Gate: driveway"
    assert r["data"] == {"url": "http://f:5000", "username": "", "password": "", "camera": "driveway", "topic_prefix": "frigate"}
    assert r["options"]["dry_run"] is True and r["options"]["enabled"] is True
    assert r["options"]["people_text"] == "Alex: XO520"
    assert r["options"]["device_entity"] == "cover.garage"
    assert r["options"]["cooldown_seconds"] == 180


async def test_url_prefilled_from_frigate_integration(hass):
    MockConfigEntry(domain="frigate", data={"url": "http://k8:5000"}).add_to_hass(hass)
    r = await _start(hass)
    schema_defaults = {k.schema: k.default() for k in r["data_schema"].schema if k.default is not None and hasattr(k, "default") and callable(k.default)}
    assert schema_defaults["url"] == "http://k8:5000"


async def test_bad_plates_error(hass):
    r = await _to_plates(hass)
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {"people_text": "Alex", "near_misses": "", "match_distance": 0, "confirmed": True}
    )
    assert r["step_id"] == "plates" and r["errors"] == {"people_text": "bad_plates"}
    assert "line 1" in r["description_placeholders"]["detail"]


async def test_cannot_connect_and_auth_required(hass):
    r = await _start(hass)
    with patch(INFO_PATH, side_effect=FrigateCannotConnect("boom")):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": "http://f:5000"})
    assert r["step_id"] == "user" and r["errors"] == {"base": "cannot_connect"}
    with patch(INFO_PATH, side_effect=FrigateAuthRequired()):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": "http://f:8971"})
    assert r["errors"] == {"base": "auth_required"}


async def test_no_cameras(hass):
    r = await _start(hass)
    with patch(INFO_PATH, return_value=FrigateInfo("0.18.0", "frigate", False, {})):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": "http://f:5000"})
    assert r["errors"] == {"base": "no_cameras"}


async def test_mqtt_required(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "mqtt_required"


async def test_duplicate_camera_aborts(hass):
    MockConfigEntry(domain=DOMAIN, data={"url": "http://f:5000", "camera": "driveway"}, unique_id="http://f:5000::driveway").add_to_hass(hass)
    r = await _start(hass)
    with patch(INFO_PATH, return_value=INFO):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": "http://f:5000"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"camera": "driveway"})
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "already_configured"


async def test_options_flow_edits_plates_and_device(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://f:5000", "username": "", "password": "", "camera": "driveway", "topic_prefix": "frigate"},
        options={**PLATES_OK, **DEVICE_OK, "enabled": True, "dry_run": True},
        unique_id="http://f:5000::driveway",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.electrifix_plate_gate.async_setup_entry", return_value=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] == FlowResultType.MENU
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "plates"})
    assert r["step_id"] == "plates"
    new_plates = {**PLATES_OK, "people_text": "Alex: XO520\nSam: LO120"}
    r = await hass.config_entries.options.async_configure(r["flow_id"], new_plates)
    assert r["step_id"] == "plates" and r["errors"] == {"base": "confirm_verdicts"}  # verdicts first
    with patch(INFO_PATH, return_value=INFO):
        r = await hass.config_entries.options.async_configure(r["flow_id"], new_plates)
    assert r["step_id"] == "device"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {**DEVICE_OK, "cooldown_seconds": 60})
    assert r["step_id"] == "frigate_yaml"
    assert "Sam" in r["description_placeholders"]["yaml"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY
    assert r["data"]["cooldown_seconds"] == 60 and r["data"]["people_text"] == "Alex: XO520\nSam: LO120"
    assert r["data"]["dry_run"] is True  # untouched by the options flow


async def test_confirm_only_counts_after_verdicts_were_shown(hass):
    r = await _to_plates(hass)
    risky = {"people_text": "Alex: XO520", "near_misses": "XO540", "match_distance": 2, "confirmed": True}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], risky)
    assert r["step_id"] == "plates" and r["errors"] == {"base": "confirm_verdicts"}
    assert "XO540 would ALSO" in r["description_placeholders"]["verdicts"]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], risky)
    assert r["step_id"] == "device"


async def _to_device(hass):
    r = await _to_plates(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], PLATES_OK)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], PLATES_OK)
    assert r["step_id"] == "device"
    return r


async def test_lock_requires_the_disclaimer_box(hass):
    r = await _to_device(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**DEVICE_OK, "device_entity": "lock.front_door"})
    assert r["step_id"] == "device" and r["errors"] == {"lock_acknowledged": "lock_ack_required"}
    r = await hass.config_entries.flow.async_configure(
        r["flow_id"], {**DEVICE_OK, "device_entity": "lock.front_door", "lock_acknowledged": True}
    )
    assert r["step_id"] == "frigate_yaml"


async def test_repeat_mode_is_stored(hass):
    r = await _to_device(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**DEVICE_OK, "repeat_mode": "cooldown_and_event"})
    assert r["step_id"] == "frigate_yaml"
    with patch("custom_components.electrifix_plate_gate.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["options"]["repeat_mode"] == "cooldown_and_event"
    assert r["options"]["lock_acknowledged"] is False


async def test_untested_frigate_version_is_noted_not_blocked(hass):
    r = await _start(hass)
    older = FrigateInfo("0.17.2-abc", "frigate", False, {"driveway": CameraInfo("driveway", 1920, 1080, [], False)})
    with patch(INFO_PATH, return_value=older):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": "http://f:5000"})
    assert r["step_id"] == "camera"
    assert "0.17.2" in r["description_placeholders"]["version_note"] and "0.18" in r["description_placeholders"]["version_note"]
    with patch(INFO_PATH, return_value=INFO):
        r2 = await _start(hass)
        r2 = await hass.config_entries.flow.async_configure(r2["flow_id"], {"url": "http://f:5001"})
    assert r2["description_placeholders"]["version_note"] == ""
