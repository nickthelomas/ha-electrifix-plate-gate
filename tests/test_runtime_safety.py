"""Regression tests from the P1 review: paths that must never move the door unintentionally."""
import asyncio
import json
import time
from datetime import timedelta

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
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


def payload(plate="NO ·860", event_id="e1", **kw):
    after = {
        "id": event_id, "camera": "driveway", "label": "car", "sub_label": None,
        "recognized_license_plate": plate, "recognized_license_plate_score": 0.95,
        "stationary": False, "current_zones": [], "entered_zones": [], "start_time": time.time() - 4.0,
    }
    after.update(kw)
    return json.dumps({"type": "update", "before": {}, "after": after})


async def _setup(hass, freezer, options=OPTS, unique_id="x", device_state=("cover.garage", "closed")):
    """Entity set an hour ago (so its last_changed is old), then the entry."""
    freezer.move_to("2026-09-28T02:00:00+00:00")
    hass.states.async_set(*device_state)
    freezer.tick(timedelta(hours=1))
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=options, title="Plate Gate: driveway", unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_two_updates_in_flight_produce_one_call(hass, mqtt, freezer):
    """Frigate sends several updates per second; a slow service must not be pulsed twice."""
    entry = await _setup(hass, freezer, {**OPTS, "device_entity": "script.garage_toggle"}, device_state=("script.garage_toggle", "off"))
    calls = []
    release = asyncio.Event()

    async def slow(call):
        calls.append(call)
        await release.wait()  # the first call is still running when the second update lands

    hass.services.async_register("script", "turn_on", slow)
    async_fire_mqtt_message(hass, "frigate/events", payload())
    async_fire_mqtt_message(hass, "frigate/events", payload())
    for _ in range(20):  # let both MQTT tasks run up to the service call
        await asyncio.sleep(0)
    release.set()
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert hass.data[DOMAIN][entry.entry_id].last_action["reason"] == "cooldown"


async def test_failed_call_is_not_retried_on_next_update(hass, mqtt, freezer):
    entry = await _setup(hass, freezer)
    attempts = []

    async def boom(call):
        attempts.append(call)
        raise RuntimeError("no power")

    hass.services.async_register("cover", "open_cover", boom)
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(attempts) == 1
    la = hass.data[DOMAIN][entry.entry_id].last_action
    assert la["state"] == "skipped" and la["reason"] == "cooldown"


async def test_reload_keeps_cooldown_after_a_door_move(hass, mqtt, freezer):
    """Plate Gate closed the door, the user saved options (reload); the still-tracked car must not re-open it."""
    entry = await _setup(hass, freezer)
    opens = async_mock_service(hass, "cover", "open_cover")
    hass.states.async_set("cover.garage", "open")
    hass.states.async_set("cover.garage", "closed")  # the door moved just now, while HA was running
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={**entry.options, "people_text": "Bonnie: NO860, 1ABC123"})
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert opens == []
    assert hass.data[DOMAIN][entry.entry_id].last_action["reason"] == "cooldown"


async def test_restart_keeps_our_last_action(hass, mqtt, freezer):
    """An HA restart inside the cooldown must not forget that we just moved the door."""
    entry = await _setup(hass, freezer)
    opens = async_mock_service(hass, "cover", "open_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(opens) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)  # a fresh runtime, like after a restart
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "frigate/events", payload(event_id="e2"))
    await hass.async_block_till_done()
    assert len(opens) == 1
    assert hass.data[DOMAIN][entry.entry_id].last_action["reason"] == "cooldown"


async def test_startup_era_last_changed_is_not_a_move(hass, mqtt, freezer):
    """After a restart every entity's last_changed is 'now'; that must not silence Plate Gate."""
    hass.set_state(CoreState.starting)
    hass.states.async_set("cover.garage", "closed")  # set during startup
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTS, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    opens = async_mock_service(hass, "cover", "open_cover")
    freezer.tick(timedelta(seconds=30))
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(opens) == 1


async def test_move_after_startup_counts(hass, mqtt, freezer):
    hass.set_state(CoreState.starting)
    hass.states.async_set("cover.garage", "closed")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTS, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    freezer.tick(timedelta(seconds=1))
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=30))
    hass.states.async_set("cover.garage", "open")  # someone used the remote after start-up
    await hass.async_block_till_done()
    closes = async_mock_service(hass, "cover", "close_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert closes == []
    assert hass.data[DOMAIN][entry.entry_id].last_action["reason"] == "cooldown"


@pytest.mark.parametrize("switch_off", ["enabled", "dry_run"])
async def test_auto_close_respects_enabled_and_dry_run(hass, mqtt, freezer, switch_off):
    entry = await _setup(hass, freezer)
    async_mock_service(hass, "cover", "open_cover")
    closes = async_mock_service(hass, "cover", "close_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    hass.states.async_set("cover.garage", "open")
    rt = hass.data[DOMAIN][entry.entry_id]
    if switch_off == "enabled":
        await rt.async_set_enabled(False)
    else:
        await rt.async_set_dry_run(True)
    freezer.tick(timedelta(minutes=5, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert closes == []
    assert rt.last_action["reason"] == "auto_close_cancelled"


async def test_once_per_event_mode_records_the_event_after_acting(hass, mqtt, freezer):
    entry = await _setup(hass, freezer, {**OPTS, "repeat_mode": "cooldown_and_event", "cooldown_seconds": 0})
    opens = async_mock_service(hass, "cover", "open_cover")
    closes = async_mock_service(hass, "cover", "close_cover")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    hass.states.async_set("cover.garage", "open")
    freezer.tick(timedelta(seconds=5))
    async_fire_mqtt_message(hass, "frigate/events", payload())  # same event, door now open
    await hass.async_block_till_done()
    assert len(opens) == 1 and closes == []
    assert hass.data[DOMAIN][entry.entry_id].last_action["reason"] == "same_event"
    async_fire_mqtt_message(hass, "frigate/events", payload(event_id="e2"))
    await hass.async_block_till_done()
    assert len(closes) == 1


async def test_lock_unlocks_and_relocks_after_timer(hass, mqtt, freezer):
    entry = await _setup(
        hass, freezer, {**OPTS, "device_entity": "lock.front_door", "lock_acknowledged": True, "auto_close_minutes": 2},
        device_state=("lock.front_door", "locked"),
    )
    unlocks = async_mock_service(hass, "lock", "unlock")
    locks = async_mock_service(hass, "lock", "lock")
    async_fire_mqtt_message(hass, "frigate/events", payload())
    await hass.async_block_till_done()
    assert len(unlocks) == 1 and unlocks[0].data["entity_id"] == "lock.front_door"
    assert hass.data[DOMAIN][entry.entry_id].last_action["state"] == "unlock"
    hass.states.async_set("lock.front_door", "unlocked")
    freezer.tick(timedelta(minutes=2, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(locks) == 1
