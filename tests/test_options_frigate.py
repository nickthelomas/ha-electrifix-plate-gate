"""Options menu: write to Frigate (diff → confirm → progress → done) and restore."""
import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrifix_plate_gate.const import DOMAIN

from .fake_frigate import FakeFrigate

RAW = (
    "mqtt:\n  host: broker\n"
    "detectors:\n  ov:\n    type: openvino\n    device: CPU\n"
    "lpr:\n  enabled: false\n"
    "cameras:\n  driveway:\n    detect:\n      width: 1920\n      height: 1080\n      fps: 10\n"
)
DATA = {"url": "http://f:5000", "username": "", "password": "", "camera": "driveway", "topic_prefix": "frigate"}
OPTS = {
    "people_text": "Alex: XO520", "near_misses": "", "match_distance": 0, "device_entity": "cover.garage",
    "open_on_arrival": True, "close_on_leaving": True, "cooldown_seconds": 180, "require_moving": False,
    "auto_close_minutes": 5, "zones": [], "enabled": True, "dry_run": True,
}


@pytest.fixture
async def env(hass, mqtt, aioclient_mock, tmp_path):
    hass.config.config_dir = str(tmp_path)
    fake = FakeFrigate(RAW)
    fake.install(aioclient_mock)
    hass.states.async_set("cover.garage", "closed")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTS, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rt = hass.data[DOMAIN][entry.entry_id]
    rt.ops.poll_s = 0.001
    rt.ops.timeout_s = 0.05
    return entry, fake, rt


async def _menu(hass, entry, choice):
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] == FlowResultType.MENU
    assert set(r["menu_options"]) == {"plates", "frigate_write", "frigate_restore", "hardware", "install_model", "benchmark", "accuracy"}
    return await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": choice})


async def _finish_progress(hass, r):
    assert r["type"] == FlowResultType.SHOW_PROGRESS, r
    await hass.async_block_till_done()
    return await hass.config_entries.options.async_configure(r["flow_id"])


async def test_write_shows_diff_requires_confirm_then_applies(hass, env):
    entry, fake, rt = env
    r = await _menu(hass, entry, "frigate_write")
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "frigate_write"
    ph = r["description_placeholders"]
    assert "+  enabled: true" in ph["diff"] and "Known plates for Alex" in ph["summary"]
    assert "native" in ph["summary"]  # detect_native defaults on: the camera has an explicit size
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": False, "confirm": False})
    assert r["step_id"] == "frigate_write" and r["errors"] == {"confirm": "confirm_required"}
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": True, "confirm": True})
    # values changed (debug on) → the diff is re-shown for the new values, not applied
    assert r["step_id"] == "frigate_write" and r["errors"] == {"confirm": "confirm_required"}
    assert "debug_save_plates" in r["description_placeholders"]["diff"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": True, "confirm": True})
    r = await _finish_progress(hass, r)
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "frigate_write_done"
    assert "camera is back" in r["description_placeholders"]["result"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY
    assert "known_plates" in fake.raw and "width: 1920" not in fake.raw and "debug_save_plates: true" in fake.raw
    assert entry.options["people_text"] == "Alex: XO520"  # options untouched
    assert hass.states.get("sensor.plate_gate_driveway_frigate_status").state == "ok"


async def test_write_rejected_config_is_reported_and_nothing_changes(hass, env):
    entry, fake, rt = env
    fake.reject_containing = "known_plates"
    r = await _menu(hass, entry, "frigate_write")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": False, "confirm": True})
    r = await _finish_progress(hass, r)
    assert r["step_id"] == "frigate_write_done" and "rejected" in r["description_placeholders"]["result"].lower()
    assert fake.raw == RAW and fake.restarts == 0


async def test_write_rolls_back_and_says_so(hass, env):
    entry, fake, rt = env
    fake.on_save = lambda body: setattr(fake, "stay_down", "known_plates" in body)
    r = await _menu(hass, entry, "frigate_write")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": False, "confirm": True})
    r = await _finish_progress(hass, r)
    assert "previous config was put back" in r["description_placeholders"]["result"]
    assert fake.raw == RAW


async def test_write_noop_when_frigate_already_matches(hass, env):
    entry, fake, rt = env
    r = await _menu(hass, entry, "frigate_write")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": False, "confirm": True})
    await _finish_progress(hass, r)
    r = await _menu(hass, entry, "frigate_write")
    assert r["type"] == FlowResultType.FORM and "(nothing to change)" in r["description_placeholders"]["diff"]
    # the camera is already native now, so that option defaults off; confirming the defaults is a no-op
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": False, "debug_save_plates": False, "confirm": True})
    assert r["step_id"] == "frigate_write" and r["errors"] == {"base": "nothing_to_change"}
    # turning the crops option on IS a change, and can be applied from the same screen
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": False, "debug_save_plates": True, "confirm": True})
    assert r["errors"] == {"confirm": "confirm_required"} and "debug_save_plates" in r["description_placeholders"]["diff"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": False, "debug_save_plates": True, "confirm": True})
    r = await _finish_progress(hass, r)
    assert r["step_id"] == "frigate_write_done" and "debug_save_plates: true" in fake.raw


async def test_write_refused_when_busy(hass, env):
    entry, fake, rt = env
    await rt.ops._lock.acquire()
    try:
        r = await _menu(hass, entry, "frigate_write")
        assert r["type"] == FlowResultType.ABORT and r["reason"] == "busy"
    finally:
        rt.ops._lock.release()


async def test_write_needs_admin(hass, mqtt, aioclient_mock, tmp_path):
    hass.config.config_dir = str(tmp_path)
    aioclient_mock.get("http://f:5000/api/config/raw", status=401)  # registered first → wins
    fake = FakeFrigate(RAW)
    fake.install(aioclient_mock)
    hass.states.async_set("cover.garage", "closed")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTS, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    r = await _menu(hass, entry, "frigate_write")
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "admin_required"


async def test_restore_lists_backups_and_restores(hass, env):
    entry, fake, rt = env
    r = await _menu(hass, entry, "frigate_write")
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": False, "confirm": True})
    r = await _finish_progress(hass, r)
    await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert "known_plates" in fake.raw
    r = await _menu(hass, entry, "frigate_restore")
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "frigate_restore"
    options = [f for f in r["data_schema"].schema if getattr(f, "schema", None) == "backup"]
    assert options, "backup selector missing"
    backup_paths = [o["value"] for o in r["data_schema"].schema[options[0]].config["options"]]
    assert len(backup_paths) == 1
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"backup": backup_paths[0], "confirm": True})
    r = await _finish_progress(hass, r)
    assert r["step_id"] == "frigate_restore_done" and "restored" in r["description_placeholders"]["result"].lower()
    assert fake.raw == RAW


async def test_restore_with_no_backups_aborts(hass, env):
    entry, fake, rt = env
    r = await _menu(hass, entry, "frigate_restore")
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "no_backups"


async def test_menu_plates_path_still_works(hass, env):
    entry, fake, rt = env
    r = await _menu(hass, entry, "plates")
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "plates"


async def test_hardware_step_shows_report_and_sensor_updates(hass, env):
    entry, fake, rt = env
    fake.inference_ms = {"default": 42.4}
    r = await _menu(hass, entry, "hardware")
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "hardware"
    report = r["description_placeholders"]["report"]
    assert "0.18.0-fake" in report and "openvino" in report and "42.4" in report and "Recommendation" in report
    assert "curl -L" in report  # the install command for the recommended model
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY
    state = hass.states.get("sensor.plate_gate_driveway_hardware")
    assert state.state.startswith("yolov9-") and state.attributes["frigate_version"] == "0.18.0-fake"
    assert state.attributes["detectors"][0]["name"] == "ov"


async def test_hardware_step_when_frigate_down(hass, env):
    entry, fake, rt = env
    fake.stay_down = True
    r = await _menu(hass, entry, "hardware")
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "cannot_connect"


async def test_benchmark_step_runs_and_reports(hass, env):
    entry, fake, rt = env
    fake.inference_ms = {"s-320": 5.6, "s-640": 16.8, "default": 20.0}
    rt.ops.sample_s = 0.001
    rt.ops.samples_per_minute = 2
    r = await _menu(hass, entry, "benchmark")
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "benchmark"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"candidates": ["yolov9-s-320"], "minutes": 1, "ack": False})
    assert r["step_id"] == "benchmark" and r["errors"] == {"ack": "confirm_required"}
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"candidates": ["yolov9-s-320", "yolov9-s-640"], "minutes": 1, "ack": True})
    r = await _finish_progress(hass, r)
    assert r["step_id"] == "benchmark_done"
    table = r["description_placeholders"]["table"]
    assert "yolov9-s-320" in table and "5.6" in table and "16.8" in table
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY
    assert fake.raw == RAW  # original config back
    state = hass.states.get("sensor.plate_gate_driveway_benchmark")
    assert state.state == "done" and len(state.attributes["rows"]) == 2 and state.attributes["rows"][0]["ok"] is True
