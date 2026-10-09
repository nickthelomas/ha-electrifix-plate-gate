"""Tests for the pure decision engine."""
import pytest

from custom_components.electrifix_plate_gate import engine as e
from custom_components.electrifix_plate_gate.plates import parse_people


def payload(**after):
    base = {
        "id": "1700000000.1-abc", "camera": "driveway", "label": "car", "sub_label": None,
        "recognized_license_plate": None, "recognized_license_plate_score": None,
        "stationary": False, "current_zones": ["driveway_approach"], "entered_zones": ["driveway_approach"],
        "start_time": 1000.0,
    }
    base.update(after)
    return {"type": "update", "before": {}, "after": base}


def settings(**kw):
    s = e.Settings(camera="driveway", people=parse_people("Alex: XO520"), dry_run=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


CLOSED = e.DeviceState("cover", "closed", 0.0)
OPEN = e.DeviceState("cover", "open", 0.0)


def test_parse_event_handles_tuple_sub_label_and_missing_fields():
    ev = e.parse_frigate_event(payload(sub_label=["Alex", 0.93], recognized_license_plate="XO ·520"))
    assert ev.sub_label == "Alex" and ev.raw_plate == "XO ·520" and ev.type == "update"
    assert e.parse_frigate_event({"type": "end"}) is None
    assert e.parse_frigate_event("garbage") is None
    assert e.parse_frigate_event(payload(sub_label="Alex")).sub_label == "Alex"
    assert e.parse_frigate_event(payload(sub_label=[])).sub_label is None


def test_xo520_with_dot_opens_closed_door():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO ·520"))
    d = e.decide(ev, CLOSED, settings(), e.EngineMemory(), now=1010.0)
    assert (d.action, d.reason, d.person, d.plate) == ("open", "go", "Alex", "XO520")
    assert d.raw_plate == "XO ·520" and d.pattern


def test_open_door_closes_on_plate():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    assert e.decide(ev, OPEN, settings(), e.EngineMemory(), now=1010.0).action == "close"


def test_reopen_bug_blocked_by_cooldown():
    # 2026-09-28: door closed at t=1000 by us; Frigate keeps tracking; update at t=1090 must NOT open
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    mem = e.EngineMemory(last_action_at=1000.0)
    d = e.decide(ev, e.DeviceState("cover", "closed", 1005.0), settings(), mem, now=1090.0)
    assert d.action is None and d.reason == "cooldown"
    d2 = e.decide(ev, e.DeviceState("cover", "closed", 1005.0), settings(), mem, now=1190.0)
    assert d2.action == "open"


def test_cooldown_counts_device_last_changed_too():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    d = e.decide(ev, e.DeviceState("cover", "closed", 1000.0), settings(), e.EngineMemory(), now=1100.0)
    assert d.reason == "cooldown"


def test_cooldown_with_unknown_last_changed_does_not_block():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    d = e.decide(ev, e.DeviceState("cover", "closed", None), settings(), e.EngineMemory(), now=1010.0)
    assert d.action == "open"


def test_sub_label_alone_accepted_but_raw_plate_wins():
    only_label = e.parse_frigate_event(payload(sub_label=["Alex", 0.9]))
    assert e.decide(only_label, CLOSED, settings(), e.EngineMemory(), 1010.0).action == "open"
    conflict = e.parse_frigate_event(payload(sub_label=["Alex", 0.9], recognized_license_plate="LO120"))
    d = e.decide(conflict, CLOSED, settings(), e.EngineMemory(), 1010.0)
    assert d.action is None and d.reason == "no_match" and d.raw_plate == "LO120"


@pytest.mark.parametrize("state", ["opening", "closing", "unavailable", "unknown"])
def test_device_busy(state):
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    d = e.decide(ev, e.DeviceState("cover", state, 0.0), settings(), e.EngineMemory(), 1010.0)
    assert d.reason == "device_busy" and d.action is None


def test_filters_in_order():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    mem = e.EngineMemory()
    assert e.decide(ev, CLOSED, settings(enabled=False), mem, 1010.0).reason == "disabled"
    assert e.decide(ev, CLOSED, settings(camera="front"), mem, 1010.0).reason == "other_camera"
    person_ev = e.parse_frigate_event(payload(label="person", recognized_license_plate="XO520"))
    assert e.decide(person_ev, CLOSED, settings(), mem, 1010.0).reason == "not_vehicle"
    ended = payload(recognized_license_plate="XO520")
    ended["type"] = "end"
    assert e.decide(e.parse_frigate_event(ended), CLOSED, settings(), mem, 1010.0).reason == "event_ended"
    assert e.decide(e.parse_frigate_event(payload()), CLOSED, settings(), mem, 1010.0).reason == "no_plate"
    parked = e.parse_frigate_event(payload(recognized_license_plate="XO520", stationary=True))
    assert e.decide(parked, CLOSED, settings(require_moving=True), mem, 1010.0).reason == "stationary"
    assert e.decide(parked, CLOSED, settings(), mem, 1010.0).action == "open"
    assert e.decide(ev, CLOSED, settings(zones=["garage_apron"]), mem, 1010.0).reason == "outside_zone"
    assert e.decide(ev, CLOSED, settings(zones=["driveway_approach"]), mem, 1010.0).action == "open"
    assert e.decide(ev, CLOSED, settings(open_on_arrival=False), mem, 1010.0).reason == "no_transition"
    assert e.decide(ev, OPEN, settings(close_on_leaving=False), mem, 1010.0).reason == "no_transition"


def test_dry_run_never_acts():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    d = e.decide(ev, CLOSED, settings(dry_run=True), e.EngineMemory(), 1010.0)
    assert d.action == "open" and d.reason == "dry_run" and d.dry_run
    assert d.would_act and not d.acts
    go = e.decide(ev, CLOSED, settings(), e.EngineMemory(), 1010.0)
    assert go.would_act and go.acts


def test_switch_and_script_trigger():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    mem = e.EngineMemory()
    d = e.decide(ev, e.DeviceState("switch", "off", 0.0), settings(device_domain="switch"), mem, 1010.0)
    assert d.action == "trigger"
    d = e.decide(ev, e.DeviceState("script", "off", 0.0), settings(device_domain="script"), mem, 1010.0)
    assert d.action == "trigger"
    d = e.decide(ev, e.DeviceState("switch", "unavailable", 0.0), settings(device_domain="switch"), mem, 1010.0)
    assert d.reason == "device_busy"
    d = e.decide(ev, e.DeviceState("switch", "off", 0.0), settings(device_domain="switch", open_on_arrival=False), mem, 1010.0)
    assert d.reason == "no_transition"


def test_match_distance_setting_reaches_matcher():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO540"))
    assert e.decide(ev, CLOSED, settings(), e.EngineMemory(), 1010.0).reason == "no_match"
    assert e.decide(ev, CLOSED, settings(match_distance=1), e.EngineMemory(), 1010.0).action == "open"


def test_once_per_event_blocks_second_action_from_same_event_only():
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    mem = e.EngineMemory(acted_events={"1700000000.1-abc": "open"})
    d = e.decide(ev, OPEN, settings(once_per_event=True, cooldown_seconds=0), mem, 2000.0)
    assert d.action is None and d.reason == "same_event"
    # cooldown-only mode ignores the event history
    assert e.decide(ev, OPEN, settings(cooldown_seconds=0), mem, 2000.0).action == "close"
    # a NEW event is allowed
    ev2 = e.parse_frigate_event(payload(id="new-event", recognized_license_plate="XO520"))
    assert e.decide(ev2, OPEN, settings(once_per_event=True, cooldown_seconds=0), mem, 2000.0).action == "close"


@pytest.mark.parametrize("state,expected", [("locked", "unlock"), ("unlocked", "lock")])
def test_lock_maps_locked_to_unlock_and_unlocked_to_lock(state, expected):
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    d = e.decide(ev, e.DeviceState("lock", state, 0.0), settings(device_domain="lock"), e.EngineMemory(), 1010.0)
    assert d.action == expected


@pytest.mark.parametrize("state", ["locking", "unlocking", "jammed", "unavailable"])
def test_lock_busy_states(state):
    ev = e.parse_frigate_event(payload(recognized_license_plate="XO520"))
    d = e.decide(ev, e.DeviceState("lock", state, 0.0), settings(device_domain="lock"), e.EngineMemory(), 1010.0)
    assert d.reason == "device_busy"
