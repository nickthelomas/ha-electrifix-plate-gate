"""Tests for the switches, sensors, number and button."""
import json
import time
from unittest.mock import patch
from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_mock_service,
)

from custom_components.electrifix_plate_gate.const import DOMAIN

DATA = {"url": "http://f:5000", "username": "", "password": "", "camera": "driveway", "topic_prefix": "frigate"}
OPTS = {
    "people_text": "Alex: XO520", "near_misses": "", "match_distance": 0, "device_entity": "cover.garage",
    "open_on_arrival": True, "close_on_leaving": True, "cooldown_seconds": 180, "require_moving": False,
    "auto_close_minutes": 0, "zones": [], "enabled": True, "dry_run": True,
}
E = "plate_gate_driveway"


def payload(plate="XO ·520"):
    after = {
        "id": "e1", "camera": "driveway", "label": "car", "sub_label": None,
        "recognized_license_plate": plate, "recognized_license_plate_score": 0.95,
        "stationary": False, "current_zones": [], "entered_zones": [], "start_time": time.time() - 4.0,
    }
    return json.dumps({"type": "update", "before": {}, "after": after})


@pytest.fixture
async def setup_dry(hass, mqtt, freezer):
    freezer.move_to("2026-09-28T02:00:00+00:00")
    hass.states.async_set("cover.garage", "closed")
    freezer.tick(timedelta(hours=1))
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTS, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_created_with_defaults(hass, setup_dry):
    assert hass.states.get(f"switch.{E}_dry_run").state == "on"
    assert hass.states.get(f"switch.{E}_enabled").state == "on"
    assert hass.states.get(f"number.{E}_cooldown").state == "180.0"
    assert hass.states.get(f"sensor.{E}_last_plate").state == "unknown"
    assert hass.states.get(f"sensor.{E}_last_action").state == "unknown"
    assert hass.states.get(f"sensor.{E}_timing").state == "unknown"
    assert hass.states.get(f"button.{E}_test_a_plate") is not None


async def test_switch_updates_runtime_and_options_without_reload(hass, setup_dry):
    rt = hass.data[DOMAIN][setup_dry.entry_id]
    await hass.services.async_call("switch", "turn_off", {"entity_id": f"switch.{E}_dry_run"}, blocking=True)
    await hass.async_block_till_done()
    assert rt.dry_run is False and setup_dry.options["dry_run"] is False
    assert hass.states.get(f"switch.{E}_dry_run").state == "off"
    assert hass.data[DOMAIN][setup_dry.entry_id] is rt
    await hass.services.async_call("switch", "turn_off", {"entity_id": f"switch.{E}_enabled"}, blocking=True)
    await hass.async_block_till_done()
    assert rt.enabled is False and hass.states.get(f"switch.{E}_enabled").state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": f"switch.{E}_enabled"}, blocking=True)
    await hass.async_block_till_done()
    assert rt.enabled is True


async def test_number_sets_cooldown(hass, setup_dry):
    await hass.services.async_call("number", "set_value", {"entity_id": f"number.{E}_cooldown", "value": 60}, blocking=True)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][setup_dry.entry_id].cooldown_seconds == 60
    assert setup_dry.options["cooldown_seconds"] == 60
    assert hass.states.get(f"number.{E}_cooldown").state == "60.0"


async def test_test_button_never_actuates_even_with_dry_run_off(hass, setup_dry):
    opens = async_mock_service(hass, "cover", "open_cover")
    await hass.services.async_call("switch", "turn_off", {"entity_id": f"switch.{E}_dry_run"}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": f"button.{E}_test_a_plate"}, blocking=True)
    await hass.async_block_till_done()
    assert opens == []
    assert hass.states.get(f"sensor.{E}_last_action").state == "would_open"
    assert hass.states.get(f"sensor.{E}_last_plate").state == "XO520"
    assert hass.states.get(f"sensor.{E}_last_plate").attributes["person"] == "Alex"


async def test_sensors_follow_runtime(hass, setup_dry):
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    plate = hass.states.get(f"sensor.{E}_last_plate")
    assert plate.state == "XO520" and plate.attributes["raw"] == "XO ·520" and plate.attributes["event_id"] == "e1"
    action = hass.states.get(f"sensor.{E}_last_action")
    assert action.state == "would_open" and action.attributes["reason"] == "dry_run" and action.attributes["person"] == "Alex"
    timing = hass.states.get(f"sensor.{E}_timing")
    assert float(timing.state) >= 3.0 and timing.attributes["unit_of_measurement"] == "s"
    assert timing.attributes["plate_delay"] is not None and timing.attributes["device_moved_at"] is None


async def test_entities_share_one_device(hass, setup_dry):
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    ent_reg = er.async_get(hass)
    dev_ids = {ent_reg.async_get(eid).device_id for eid in (f"switch.{E}_dry_run", f"sensor.{E}_timing", f"button.{E}_test_a_plate")}
    assert len(dev_ids) == 1
    device = dr.async_get(hass).async_get(dev_ids.pop())
    assert device.manufacturer == "ElectriFix" and device.name == "Plate Gate: driveway"


async def test_frigate_status_sensor_and_restore_button(hass, setup_dry):
    assert hass.states.get(f"sensor.{E}_frigate_status").state == "idle"
    rt = hass.data[DOMAIN][setup_dry.entry_id]
    with patch.object(rt.ops, "async_restore_latest") as restore:
        await hass.services.async_call("button", "press", {"entity_id": f"button.{E}_restore_frigate"}, blocking=True)
        await hass.async_block_till_done()
    assert restore.called
