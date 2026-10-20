"""Plate Gate side of the companion: hub discovery, command round trip, sensor, install step, stream host."""
import asyncio
import json
from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from custom_components.electrifix_plate_gate.companion import CompanionError, CompanionHub
from custom_components.electrifix_plate_gate.const import DOMAIN
from custom_components.electrifix_plate_gate.hardware import stream_host

from .fake_frigate import FakeFrigate

RAW = (
    "mqtt:\n  host: broker\n"
    "detectors:\n  ov:\n    type: openvino\n    device: CPU\n"
    "lpr:\n  enabled: false\n"
    "cameras:\n  driveway:\n    ffmpeg:\n      inputs:\n        - path: rtsp://user:secretpw@192.0.2.40:554/stream1\n          roles: [detect]\n    detect:\n      fps: 10\n"
)
DATA = {"url": "http://f:5000", "username": "", "password": "", "camera": "driveway", "topic_prefix": "frigate"}
OPTS = {
    "people_text": "Alex: XO520", "near_misses": "", "match_distance": 0, "device_entity": "cover.garage",
    "open_on_arrival": True, "close_on_leaving": True, "cooldown_seconds": 180, "require_moving": False,
    "auto_close_minutes": 5, "zones": [], "enabled": True, "dry_run": True,
}
STATUS = {
    "id": "box", "version": "0.1.0", "online": True, "deployment": "docker", "frigate_config_dir": "/frigate_config",
    "writable": True, "models": [{"filename": "yolov9-t-320.onnx", "size": 8142046, "sha256": "x"}],
    "hardware": {"cpu_model": "AMD Ryzen 7 8845HS", "cores": 16, "mem_total_mb": 63898, "coral_usb": True, "coral_pci": False,
                 "hailo": False, "memryx": False, "usb": [{"vendor": "1a6e", "product": "089a", "name": "Coral USB (unflashed)"}],
                 "pci": [], "render_nodes": ["renderD128"], "config_dir_writable": True, "disk_free_mb": 50000},
    "updated": 1.0,
}
T = "electrifix_plate_gate/companion/box"


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
    return entry, fake, rt


def _published(mqtt):
    return [(c.args[0], json.loads(c.args[1])) for c in mqtt.async_publish.call_args_list]


async def _answer_probe(hass, mqtt):
    """The install path sends a quick probe first; answer it so the install command follows."""
    topic, cmd = _published(mqtt)[-1]
    assert cmd["action"] == "probe", cmd
    async_fire_mqtt_message(hass, f"{T}/result/{cmd['req_id']}", json.dumps({"req_id": cmd["req_id"], "ok": True, "done": True, "data": {}}))
    for _ in range(5):
        await asyncio.sleep(0)


async def test_hub_discovers_companion_from_retained_status(hass, env):
    entry, fake, rt = env
    assert rt.companion.best is None
    assert hass.states.get("sensor.plate_gate_driveway_companion").state == "none"
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    best = rt.companion.best
    assert best and best.id == "box" and best.online and best.hardware["coral_usb"] is True
    assert rt.companion.has_model("yolov9-t-320.onnx") and not rt.companion.has_model("yolov9-m-640.onnx")
    st = hass.states.get("sensor.plate_gate_driveway_companion")
    assert st.state == "online" and st.attributes["deployment"] == "docker" and st.attributes["models"][0]["filename"] == "yolov9-t-320.onnx"
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps({"id": "box", "online": False}))
    await hass.async_block_till_done()
    assert rt.companion.best is None and hass.states.get("sensor.plate_gate_driveway_companion").state == "offline"


async def test_command_round_trip_with_progress(hass, env, mqtt):
    entry, fake, rt = env
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    seen = []
    task = hass.async_create_task(rt.companion.async_command("box", "install_model", {"url": "u", "filename": "f.onnx"}, timeout=5, on_progress=lambda p: seen.append(p)))
    await asyncio.sleep(0)
    topic, cmd = _published(mqtt)[-1]
    assert topic == f"{T}/cmd" and cmd["action"] == "install_model" and cmd["url"] == "u" and cmd["req_id"]
    rid = cmd["req_id"]
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": False, "progress": {"stage": "downloading", "pct": 40}}))
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": True, "message": "installed", "data": {"filename": "f.onnx"}}))
    result = await task
    assert result["data"]["filename"] == "f.onnx" and seen == [{"stage": "downloading", "pct": 40}]


async def test_command_refused_when_offline_and_times_out(hass, env, mqtt):
    entry, fake, rt = env
    with pytest.raises(CompanionError, match="No companion"):
        await rt.companion.async_command("box", "probe", timeout=1)
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps({**STATUS, "online": False}))
    await hass.async_block_till_done()
    with pytest.raises(CompanionError, match="offline"):
        await rt.companion.async_command("box", "probe", timeout=1)
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    with pytest.raises(CompanionError, match="did not answer"):
        await rt.companion.async_command("box", "probe", timeout=0.05)


async def test_command_failure_message_surfaces(hass, env, mqtt):
    entry, fake, rt = env
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    task = hass.async_create_task(rt.companion.async_command("box", "install_model", {"url": "u", "filename": "f.onnx"}, timeout=5))
    await asyncio.sleep(0)
    rid = _published(mqtt)[-1][1]["req_id"]
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": False, "done": True, "message": "Download refused: not allowed"}))
    with pytest.raises(CompanionError, match="not allowed"):
        await task


async def test_install_model_step(hass, env, mqtt):
    entry, fake, rt = env
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert "install_model" in r["menu_options"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "install_model"})
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "no_companion"
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "install_model"})
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "install_model"
    labels = {o["value"]: o["label"] for o in r["data_schema"].schema[[k for k in r["data_schema"].schema if k.schema == "model"][0]].config["options"]}
    assert "installed" in labels["yolov9-t-320"] and "not on the box" in labels["yolov9-m-640"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"model": "yolov9-m-640"})
    assert r["type"] == FlowResultType.SHOW_PROGRESS
    await asyncio.sleep(0)
    await _answer_probe(hass, mqtt)
    topic, cmd = _published(mqtt)[-1]
    assert cmd["action"] == "install_model" and cmd["filename"] == "yolov9-m-640.onnx" and cmd["url"].endswith("/yolov9-m-640.onnx") and cmd["sha256"]
    rid = cmd["req_id"]
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": False, "progress": {"stage": "downloading", "pct": 55}}))
    for _ in range(5):
        await asyncio.sleep(0)  # not block_till_done: the install task is (rightly) still waiting for "done"
    assert "55" in hass.states.get("sensor.plate_gate_driveway_frigate_status").attributes["message"]
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": True, "message": "yolov9-m-640.onnx installed (76 MB).", "data": {"filename": "yolov9-m-640.onnx"}}))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_configure(r["flow_id"])
    assert r["step_id"] == "install_model_done" and "installed" in r["description_placeholders"]["result"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY


async def test_install_uses_models_base_url_option(hass, env, mqtt):
    entry, fake, rt = env
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "install_model"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"model": "yolov9-s-320", "source": "http://frigate:5000/models/"})
    await asyncio.sleep(0)
    await _answer_probe(hass, mqtt)
    cmd = _published(mqtt)[-1][1]
    assert cmd["url"] == "http://frigate:5000/models/yolov9-s-320.onnx"
    async_fire_mqtt_message(hass, f"{T}/result/{cmd['req_id']}", json.dumps({"req_id": cmd["req_id"], "ok": True, "done": True, "message": "installed", "data": {}}))
    await hass.async_block_till_done()
    hass.config_entries.options.async_abort(r["flow_id"])
    await hass.async_block_till_done()


async def test_hardware_screen_shows_companion_and_stream_host(hass, env):
    entry, fake, rt = env
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "hardware"})
    report = r["description_placeholders"]["report"]
    assert "192.0.2.40:554" in report and "secretpw" not in report and "user:" not in report
    assert "Coral" in report and "USB" in report and "not used by Frigate" in report
    assert "AMD Ryzen 7 8845HS" in report and "yolov9-t-320.onnx" in report
    assert "Install model" in report and "curl" not in report  # companion present → no curl line


def test_stream_host_strips_credentials():
    cfg = {"cameras": {"c": {"ffmpeg": {"inputs": [{"path": "rtsp://admin:p%40ss@10.0.0.5:554/h264"}]}}}}
    assert stream_host(cfg, "c") == "10.0.0.5:554"
    assert stream_host({"cameras": {"c": {"ffmpeg": {"inputs": [{"path": "/dev/video0"}]}}}}, "c") == "/dev/video0"
    assert stream_host({}, "c") is None


async def test_benchmark_labels_show_installed(hass, env):
    entry, fake, rt = env
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "benchmark"})
    labels = {o["value"]: o["label"] for o in r["data_schema"].schema[[k for k in r["data_schema"].schema if k.schema == "candidates"][0]].config["options"]}
    assert "(installed)" in labels["yolov9-t-320"] and "(not on the box)" in labels["yolov9-m-640"]


# ---- review fixes -------------------------------------------------------------------
import time as _time  # noqa: E402

from custom_components.electrifix_plate_gate.hardware import stream_host as _sh  # noqa: E402


async def test_closing_install_dialog_keeps_waiting_and_updates_status(hass, env, mqtt):
    entry, fake, rt = env
    rt.companion.probe_timeout = 0.5
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "install_model"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"model": "yolov9-m-640"})
    await asyncio.sleep(0)
    probe_rid = _published(mqtt)[-1][1]["req_id"]  # a quick pre-probe first
    assert _published(mqtt)[-1][1]["action"] == "probe"
    async_fire_mqtt_message(hass, f"{T}/result/{probe_rid}", json.dumps({"req_id": probe_rid, "ok": True, "done": True, "data": {}}))
    for _ in range(5):
        await asyncio.sleep(0)
    topic, cmd = _published(mqtt)[-1]
    assert cmd["action"] == "install_model"
    rid = cmd["req_id"]
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": False, "progress": {"stage": "downloading", "pct": 43}}))
    for _ in range(5):
        await asyncio.sleep(0)
    hass.config_entries.options.async_abort(r["flow_id"])  # user closes the dialog
    for _ in range(5):
        await asyncio.sleep(0)
    assert rt.ops.status == "applying"  # still waiting: the companion is still downloading
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": True, "message": "yolov9-m-640.onnx installed (76 MB).", "data": {}}))
    await hass.async_block_till_done()
    assert rt.ops.status == "ok" and "installed" in rt.ops.message


async def test_stale_online_status_is_treated_as_offline(hass, env):
    entry, fake, rt = env
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    assert rt.companion.best is not None
    rt.companion.companions["box"].last_seen = _time.time() - 3000
    assert rt.companion.best is None
    with pytest.raises(CompanionError, match="offline|stale"):
        await rt.companion.async_command("box", "probe", timeout=1)
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))  # the periodic staleness check
    await hass.async_block_till_done()
    assert hass.states.get("sensor.plate_gate_driveway_companion").state == "offline"


async def test_install_preprobe_fails_fast_when_companion_silent(hass, env, mqtt):
    entry, fake, rt = env
    rt.companion.probe_timeout = 0.05
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    result = await rt.async_install_model("yolov9-m-640")
    assert result["ok"] is False and "did not answer" in result["message"]
    assert all(cmd["action"] == "probe" for _, cmd in _published(mqtt))
    assert rt.ops.status == "failed"


async def test_empty_or_list_status_payloads(hass, env):
    entry, fake, rt = env
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, f"{T}/status", "")  # retained topic cleared
    await hass.async_block_till_done()
    assert rt.companion.best is None
    async_fire_mqtt_message(hass, f"{T}/status", "[1,2,3]")  # nonsense: ignored, no crash
    await hass.async_block_till_done()


def test_stream_host_edge_cases():
    assert _sh({"cameras": {"c": {"ffmpeg": {"inputs": [{"path": "rtsp://[2001:db8::10]:554/x"}]}}}}, "c") == "[2001:db8::10]:554"
    assert _sh({"cameras": {"c": {"ffmpeg": {"inputs": [{"path": "rtsp://u:p@cam.local/x"}]}}}}, "c") == "cam.local"
    assert _sh({"cameras": {"c": {"ffmpeg": {"inputs": [{"path": "rtsp://cam:99999/x"}]}}}}, "c") in ("cam", "cam:99999")
    cfg = {"go2rtc": {"streams": {"driveway": ["rtsp://admin:pw@192.0.2.40:554/stream1", "ffmpeg:driveway#audio=opus"]}},
           "cameras": {"driveway": {"ffmpeg": {"inputs": [{"path": "rtsp://127.0.0.1:8554/driveway"}]}}}}
    assert _sh(cfg, "driveway") == "192.0.2.40:554 (via go2rtc)"
    assert _sh({"cameras": {"c": {"ffmpeg": {"inputs": [{"path": "rtsp://127.0.0.1:8554/unknown"}]}}}}, "c") == "127.0.0.1:8554 (a local restream)"


# ---- accuracy benchmark screen ------------------------------------------------------------
async def test_accuracy_aborts_without_companion_or_models(hass, env):
    entry, fake, rt = env
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert "accuracy" in r["menu_options"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "accuracy"})
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "no_companion"
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps({**STATUS, "models": []}))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "accuracy"})
    assert r["type"] == FlowResultType.ABORT and r["reason"] == "no_models"


async def test_accuracy_step_runs_and_reports(hass, env, mqtt):
    entry, fake, rt = env
    rt.companion.probe_timeout = 0.5
    status = {**STATUS, "models": [{"filename": "yolov9-t-320.onnx", "size": 1, "sha256": "a"}, {"filename": "yolov9-s-320.onnx", "size": 1, "sha256": "b"}]}
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(status))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "accuracy"})
    assert r["type"] == FlowResultType.FORM and r["step_id"] == "accuracy"
    field = [k for k in r["data_schema"].schema if k.schema == "models"][0]
    assert [o["value"] for o in r["data_schema"].schema[field].config["options"]] == ["yolov9-t-320.onnx", "yolov9-s-320.onnx"]
    assert "favours" in r["description_placeholders"]["note"] or "not ground truth" in r["description_placeholders"]["note"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"models": ["yolov9-t-320.onnx"], "max_images": 40, "ack": False})
    assert r["errors"] == {"ack": "confirm_required"}
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"models": ["yolov9-t-320.onnx", "yolov9-s-320.onnx"], "max_images": 40, "ack": True})
    assert r["type"] == FlowResultType.SHOW_PROGRESS
    await asyncio.sleep(0)
    await _answer_probe(hass, mqtt)
    topic, cmd = _published(mqtt)[-1]
    assert cmd["action"] == "accuracy_bench" and cmd["camera"] == "driveway" and cmd["models"] == ["yolov9-t-320.onnx", "yolov9-s-320.onnx"] and cmd["max_images"] == 40
    rid = cmd["req_id"]
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": False, "progress": {"stage": "yolov9-t-320.onnx: 3/80", "pct": 4}}))
    for _ in range(5):
        await asyncio.sleep(0)
    assert hass.states.get("sensor.plate_gate_driveway_accuracy").state == "running"
    data = {"camera": "driveway", "images_used": 40, "skipped": 1, "rows": [
        {"filename": "yolov9-t-320.onnx", "ok": True, "imgsz": 320, "images_with_vehicle": 31, "vehicles_total": 35, "mean_top_score": 0.81, "mean_ms": 9.2, "note": ""},
        {"filename": "yolov9-s-320.onnx", "ok": True, "imgsz": 320, "images_with_vehicle": 36, "vehicles_total": 41, "mean_top_score": 0.86, "mean_ms": 14.1, "note": ""},
    ]}
    async_fire_mqtt_message(hass, f"{T}/result/{rid}", json.dumps({"req_id": rid, "ok": True, "done": True, "message": "Ran 2 model(s) over 40 snapshot(s).", "data": data}))
    await hass.async_block_till_done()
    r = await hass.config_entries.options.async_configure(r["flow_id"])
    assert r["step_id"] == "accuracy_done"
    table = r["description_placeholders"]["table"]
    assert "yolov9-s-320.onnx" in table and "36" in table and "40" in table and "31" in table and "skipped" in table.lower()
    r = await hass.config_entries.options.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY
    st = hass.states.get("sensor.plate_gate_driveway_accuracy")
    assert st.state == "done" and st.attributes["images_used"] == 40 and st.attributes["rows"][1]["images_with_vehicle"] == 36 and st.attributes["camera"] == "driveway"


async def test_accuracy_failure_reported(hass, env, mqtt):
    entry, fake, rt = env
    rt.companion.probe_timeout = 0.05
    async_fire_mqtt_message(hass, f"{T}/status", json.dumps(STATUS))
    await hass.async_block_till_done()
    result = await rt.async_accuracy_bench(["yolov9-t-320.onnx"], 20)
    assert result["ok"] is False and "did not answer" in result["message"]
    assert hass.states.get("sensor.plate_gate_driveway_accuracy").state == "failed"
