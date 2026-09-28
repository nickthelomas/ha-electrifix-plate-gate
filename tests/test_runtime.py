"""Tests for the runtime: MQTT in, service call out, timing, auto-close."""
import json
import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.electrifix_plate_gate.const import DOMAIN

DATA = {"url": "http://f:5000", "username": "", "password": "", "camera": "driveway", "topic_prefix": "frigate"}
OPTS = {
    "people_text": "Bonnie: NO860", "near_misses": "", "match_distance": 0, "device_entity": "cover.garage",
    "open_on_arrival": True, "close_on_leaving": True, "cooldown_seconds": 180, "require_moving": False,
    "auto_close_minutes": 5, "zones": [], "enabled": True, "dry_run": False,
}


def payload(plate="NO ·860", **kw):
    after = {
        "id": "e1", "camera": "driveway", "label": "car", "sub_label": None,
        "recognized_license_plate": plate, "recognized_license_plate_score": 0.95,
        "stationary": False, "current_zones": [], "entered_zones": [], "start_time": time.time() - 4.0,
    }
    after.update(kw)
    return json.dumps({"type": "update", "before": {}, "after": after})


async def _setup(hass, freezer, options=OPTS, unique_id="x"):
    freezer.move_to("2026-09-28T02:00:00+00:00")
    hass.states.async_set("cover.garage", "closed")  # an hour before the entry: an old, trusted last_changed
    freezer.tick(timedelta(hours=1))
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=options, title="Plate Gate: driveway", unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def setup(hass, mqtt, freezer):
    return await _setup(hass, freezer)


async def test_opens_on_plate_and_records_timing(hass, setup):
    opens = async_mock_service(hass, "cover", "open_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(opens) == 1 and opens[0].data["entity_id"] == "cover.garage"
    rt = hass.data[DOMAIN][setup.entry_id]
    assert rt.last_plate["plate"] == "NO860" and rt.last_plate["person"] == "Bonnie"
    assert rt.last_plate["raw"] == "NO ·860" and rt.last_plate["event_id"] == "e1"
    assert rt.last_action["state"] == "open" and rt.last_action["reason"] == "go"
    assert rt.timing["total"] >= 3.0 and rt.timing["device_moved_at"] is None
    hass.states.async_set("cover.garage", "opening")
    await hass.async_block_till_done()
    assert rt.timing["device_moved_at"] is not None and rt.timing["device_delay"] >= 0


async def test_dry_run_default_blocks_service(hass, mqtt, freezer):
    entry = await _setup(hass, freezer, {**OPTS, "dry_run": True}, "y")
    opens = async_mock_service(hass, "cover", "open_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert opens == []
    assert hass.data[DOMAIN][entry.entry_id].last_action["state"] == "would_open"


async def test_second_update_within_cooldown_is_skipped(hass, setup):
    opens = async_mock_service(hass, "cover", "open_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(opens) == 1
    la = hass.data[DOMAIN][setup.entry_id].last_action
    assert la["state"] == "skipped" and la["reason"] == "cooldown"


async def test_real_device_state_busy_blocks(hass, setup):
    opens = async_mock_service(hass, "cover", "open_cover")
    hass.states.async_set("cover.garage", "opening")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert opens == [] and hass.data[DOMAIN][setup.entry_id].last_action["reason"] == "device_busy"


async def test_unmatched_plate_and_other_camera(hass, setup):
    rt = hass.data[DOMAIN][setup.entry_id]
    async_fire_mqtt_message(hass, "frigate/events", payload(plate="XYZ123"))
    await hass.async_block_till_done()
    assert rt.last_action["state"] == "skipped" and rt.last_action["reason"] == "no_match"
    assert rt.last_plate["plate"] == "XYZ123" and rt.last_plate["person"] is None
    async_fire_mqtt_message(hass, "frigate/events", payload(plate=None, camera="front"))
    await hass.async_block_till_done()
    assert rt.last_action["reason"] == "no_match"  # other_camera is ignored silently
    async_fire_mqtt_message(hass, "frigate/events", "not json")
    await hass.async_block_till_done()
    assert rt.last_action["reason"] == "no_match"


async def test_auto_close_fires_only_if_still_open(hass, setup):
    closes = async_mock_service(hass, "cover", "close_cover")
    async_mock_service(hass, "cover", "open_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    hass.states.async_set("cover.garage", "open")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()
    assert len(closes) == 1


async def test_auto_close_skipped_when_already_closed(hass, setup):
    closes = async_mock_service(hass, "cover", "close_cover")
    async_mock_service(hass, "cover", "open_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    hass.states.async_set("cover.garage", "open")
    hass.states.async_set("cover.garage", "closed")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()
    assert closes == []


async def test_service_error_is_reported_not_raised(hass, setup):
    async def boom(call):
        raise RuntimeError("no power")

    hass.services.async_register("cover", "open_cover", boom)
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    la = hass.data[DOMAIN][setup.entry_id].last_action
    assert la["state"] == "error" and "no power" in la["message"]


async def test_switch_device_uses_turn_on(hass, mqtt, freezer):
    freezer.move_to("2026-09-28T02:00:00+00:00")
    hass.states.async_set("switch.gate", "off")
    entry = await _setup(hass, freezer, {**OPTS, "device_entity": "switch.gate"}, "z")
    calls = async_mock_service(hass, "switch", "turn_on")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(calls) == 1 and calls[0].data["entity_id"] == "switch.gate"
    assert hass.data[DOMAIN][entry.entry_id].last_action["state"] == "trigger"


async def test_test_match_never_actuates(hass, setup):
    opens = async_mock_service(hass, "cover", "open_cover")
    rt = hass.data[DOMAIN][setup.entry_id]
    d = await rt.async_test_match()
    assert d.action == "open" and d.dry_run and opens == []
    assert rt.last_action["state"] == "would_open" and rt.last_plate["plate"] == "NO860"
    assert rt.last_action["event_id"].startswith("test-")


async def test_setters_persist_to_options_without_reload(hass, setup):
    rt = hass.data[DOMAIN][setup.entry_id]
    with patch("custom_components.electrifix_plate_gate.async_unload_entry") as unload:
        await rt.async_set_dry_run(True)
        await rt.async_set_enabled(False)
        await rt.async_set_cooldown(60)
        await hass.async_block_till_done()
    assert unload.call_count == 0
    assert setup.options["dry_run"] is True and setup.options["enabled"] is False
    assert setup.options["cooldown_seconds"] == 60
    assert rt.dry_run and not rt.enabled and rt.cooldown_seconds == 60
    assert hass.data[DOMAIN][setup.entry_id] is rt


async def test_other_option_change_reloads(hass, setup):
    rt = hass.data[DOMAIN][setup.entry_id]
    hass.config_entries.async_update_entry(setup, options={**setup.options, "cooldown_seconds": 10, "people_text": "Sam: LO160"})
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][setup.entry_id] is not rt
    assert hass.data[DOMAIN][setup.entry_id].settings().people[0].name == "Sam"


async def test_unload(hass, setup):
    assert await hass.config_entries.async_unload(setup.entry_id)
    await hass.async_block_till_done()
    assert setup.entry_id not in hass.data.get(DOMAIN, {})


async def test_startup_recent_last_changed_does_not_block(hass, setup):
    """After an HA restart every entity's last_changed is 'now'; that must not count as a door move."""
    opens = async_mock_service(hass, "cover", "open_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(opens) == 1
    assert hass.data[DOMAIN][setup.entry_id].last_action["state"] == "open"


async def test_manual_door_move_after_start_blocks(hass, setup):
    """Someone used the remote: the door moved, so Plate Gate waits out the cooldown."""
    opens = async_mock_service(hass, "cover", "open_cover")
    hass.states.async_set("cover.garage", "open")
    hass.states.async_set("cover.garage", "closed")
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert opens == []
    assert hass.data[DOMAIN][setup.entry_id].last_action["reason"] == "cooldown"


async def test_unavailable_to_closed_is_not_a_move(hass, setup):
    opens = async_mock_service(hass, "cover", "open_cover")
    hass.states.async_set("cover.garage", "unavailable")
    hass.states.async_set("cover.garage", "closed")
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(opens) == 1
