"""Regression tests from the Phase 2 review (C1, I1–I10)."""
import asyncio
from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.electrifix_plate_gate import backups
from custom_components.electrifix_plate_gate import config_writer as w
from custom_components.electrifix_plate_gate.const import DOMAIN
from custom_components.electrifix_plate_gate.frigate_api import FrigateClient
from custom_components.electrifix_plate_gate.frigate_ops import Candidate, FrigateOps
from custom_components.electrifix_plate_gate.models import MODEL_CATALOGUE
from custom_components.electrifix_plate_gate.plates import parse_people

from .fake_frigate import FakeFrigate

PEOPLE = parse_people("Alex: XO520")
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
def cfgdir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def make_ops(hass, aioclient_mock, fake, url="http://f:5000", user=None, pw=None):
    fake.install(aioclient_mock)
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "url": url}, options={}, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    ops = FrigateOps(hass, entry, FrigateClient(async_get_clientsession(hass), url, user, pw), "driveway", notify=lambda: None)
    ops.poll_s = 0.001
    ops.timeout_s = 0.05
    return ops


def change_for(raw, **kw):
    return w.plan_change(raw, w.Desired(people=PEOPLE, camera="driveway", **kw))


# ---- C1: closing the dialog must not abandon the write -----------------------------
@pytest.fixture
async def env(hass, mqtt, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    fake.install(aioclient_mock)
    hass.states.async_set("cover.garage", "closed")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTS, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rt = hass.data[DOMAIN][entry.entry_id]
    rt.ops.poll_s = 0.01
    rt.ops.timeout_s = 0.3
    return entry, fake, rt


async def test_c1_closing_dialog_mid_write_still_verifies_and_rolls_back(hass, env):
    entry, fake, rt = env
    fake.on_save = lambda body: setattr(fake, "stay_down", "known_plates" in body)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "frigate_write"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"detect_native": True, "debug_save_plates": False, "confirm": True})
    assert r["type"] == FlowResultType.SHOW_PROGRESS
    await asyncio.sleep(0.02)  # the save has gone out, Frigate is "restarting"
    hass.config_entries.options.async_abort(r["flow_id"])  # user closed the dialog → HA cancels the progress task
    await hass.async_block_till_done()
    for _ in range(100):
        if rt.ops.status in ("rolled_back", "failed", "ok"):
            break
        await asyncio.sleep(0.02)
    assert rt.ops.status == "rolled_back" and fake.raw == RAW and len(fake.saves) == 2
    assert not rt.ops.busy


async def test_c1_unexpected_exception_after_save_rolls_back(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake)
    with patch.object(ops, "async_wait_healthy", side_effect=[RuntimeError("boom"), True]):
        result = await ops.async_apply(change_for(RAW))
    assert not result.ok and result.rolled_back and fake.raw == RAW and ops.status == "rolled_back"


# ---- I1: benchmark backups must not evict the write backups ------------------------
async def test_i1_benchmarks_do_not_evict_the_write_backup(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake)
    ops.sample_s = 0.001
    ops.samples_per_minute = 1
    assert (await ops.async_apply(change_for(RAW))).ok
    cands = [Candidate(k, MODEL_CATALOGUE[k], None) for k in ("yolov9-t-320", "yolov9-s-320", "yolov9-s-640", "yolov9-m-640", "yolov9-c-640")]
    for _ in range(5):
        await ops.async_benchmark(cands, minutes=1)
    infos = await backups.async_list_backups(hass, "http://f:5000")
    kinds = {i.kind for i in infos}
    assert "before_write" in kinds
    assert sum(1 for i in infos if i.kind == "benchmark") <= 5  # one per run, not per candidate
    assert (await ops.async_restore_latest()).ok and fake.raw == RAW


# ---- I2: the write must be against the live file -----------------------------------
async def test_i2_config_changed_since_preview_is_refused(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake)
    change = change_for(RAW)
    fake.raw = RAW + "# edited in Frigate's editor meanwhile\n"
    result = await ops.async_apply(change)
    assert not result.ok and result.stage == "stale" and fake.saves == []
    assert "changed since" in result.message


# ---- I3: anchors / merge keys ------------------------------------------------------
ANCHORED = (
    "x-detect: &detect\n  width: 1920\n  height: 1080\n  fps: 5\n"
    "cameras:\n  driveway:\n    detect: *detect\n  yard:\n    detect: *detect\n"
)
MERGED = (
    "x-cam: &cam\n  detect:\n    width: 1920\n    height: 1080\n    fps: 5\n"
    "cameras:\n  driveway:\n    <<: *cam\n  yard:\n    <<: *cam\n"
)


@pytest.mark.parametrize("raw", [ANCHORED, MERGED])
def test_i3_shared_detect_is_not_edited_for_other_cameras(raw):
    ch = w.plan_change(raw, w.Desired(people=PEOPLE, camera="driveway", detect_native=True))
    new = w.load_yaml(ch.new_yaml)
    yard = new["cameras"]["yard"]["detect"]
    assert yard["width"] == 1920 and yard["height"] == 1080
    assert any("shared" in x.lower() or "anchor" in x.lower() for x in ch.warnings)
    assert not any("native" in s for s in ch.summary)


def test_i3_safety_net_refuses_unexpected_changes():
    # simulate a writer bug: an edit that touches something outside the intended keys must be refused
    with patch.object(w, "_apply_detect_native", lambda root, desired, summary, *a: root["mqtt"].__setitem__("host", "oops")):
        with pytest.raises(w.WriterError):
            w.plan_change(RAW, w.Desired(people=PEOPLE, camera="driveway", detect_native=True))


# ---- I4: keep the user's list indentation ---------------------------------------
OFFSET0 = (
    "mqtt:\n  host: broker\n"
    "cameras:\n  driveway:\n    ffmpeg:\n      inputs:\n      - path: rtsp://a\n        roles:\n        - detect\n        - record\n"
    "  yard:\n    ffmpeg:\n      inputs:\n      - path: rtsp://b\n        roles:\n        - detect\n"
    "objects:\n  track:\n  - person\n  - car\n"
)


def test_i4_lpr_only_change_does_not_reindent_lists():
    ch = w.plan_change(OFFSET0, w.Desired(people=PEOPLE, camera="driveway"))
    assert "      - path: rtsp://a" in ch.new_yaml and "  - person" in ch.new_yaml
    changed = [ln for ln in ch.diff.splitlines() if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    assert all("lpr" in ln or "enabled" in ln or "match_distance" in ln or "known_plates" in ln or "Alex" in ln or "X[" in ln for ln in changed), changed


# ---- I5: camera-level lpr override -------------------------------------------------
CAM_LPR_OFF = RAW.replace("      fps: 10\n", "      fps: 10\n    lpr:\n      enabled: false\n")


def test_i5_camera_level_lpr_off_is_turned_on():
    ch = w.plan_change(CAM_LPR_OFF, w.Desired(people=PEOPLE, camera="driveway"))
    new = w.load_yaml(ch.new_yaml)
    assert new["cameras"]["driveway"]["lpr"]["enabled"] is True
    assert any("driveway" in s and "plate reader" in s.lower() for s in ch.summary)


# ---- I6: stats/config on 8971 log in like the admin routes ------------------------
async def test_i6_stats_and_config_login_on_8971(hass, aioclient_mock):
    calls = {"stats": 0, "config": 0}

    async def stats(method, url, data):
        calls["stats"] += 1
        return AiohttpClientMockResponse(method, url, status=401) if calls["stats"] == 1 else AiohttpClientMockResponse(method, url, json={"service": {}})

    async def config(method, url, data):
        calls["config"] += 1
        return AiohttpClientMockResponse(method, url, status=401) if calls["config"] == 1 else AiohttpClientMockResponse(method, url, json={"cameras": {}})

    aioclient_mock.get("http://f:8971/api/stats", side_effect=stats)
    aioclient_mock.get("http://f:8971/api/config", side_effect=config)
    aioclient_mock.post("http://f:8971/api/login", status=200)
    c = FrigateClient(async_get_clientsession(hass), "http://f:8971", "admin", "pw")
    assert await c.async_stats() == {"service": {}}
    assert await c.async_config() == {"cameras": {}}


# ---- I7: an offline camera -------------------------------------------------------
async def test_i7_offline_camera_is_refused_before_writing(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    fake.camera_fps = 0.0
    ops = make_ops(hass, aioclient_mock, fake)
    result = await ops.async_apply(change_for(RAW))
    assert not result.ok and result.stage == "precheck" and fake.saves == [] and "streaming" in result.message


async def test_i7_rollback_with_camera_gone_is_not_reported_as_failed(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake)

    def after_save(body):
        fake.camera_fps = 0.0  # the camera drops out during the restart and stays out

    fake.on_save = after_save
    result = await ops.async_apply(change_for(RAW))
    assert not result.ok and result.rolled_back and fake.raw == RAW and ops.status == "rolled_back"
    assert "streaming" in result.message


# ---- I8: a model nested under a detector ----------------------------------------
NESTED = RAW.replace("    device: CPU\n", "    device: CPU\n    model:\n      path: /old.onnx\n      width: 320\n      height: 320\n")


def test_i8_detector_level_model_is_removed_when_root_model_is_written():
    ch = w.plan_change(NESTED, w.Desired(people=PEOPLE, camera="driveway", model=MODEL_CATALOGUE["yolov9-m-640"], detector_count=2))
    new = w.load_yaml(ch.new_yaml)
    assert "model" not in new["detectors"]["ov"] and "model" not in new["detectors"]["ov_1"]
    assert "&id" not in ch.new_yaml and "*id" not in ch.new_yaml
    assert any("detector" in s.lower() and "model" in s.lower() for s in ch.summary)


# ---- I9: save error after Frigate actually stored the file -------------------------
async def test_i9_send_error_but_file_saved_continues_to_verify(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake)
    original = fake.save_post

    async def save_then_timeout(method, url, data):
        await original(method, url, data)  # Frigate stored it and is restarting …
        raise asyncio.TimeoutError()       # … but our request timed out

    aioclient_mock.clear_requests()
    aioclient_mock.post("http://f:5000/api/config/save?save_option=restart", side_effect=save_then_timeout)
    fake.install(aioclient_mock)
    result = await ops.async_apply(change_for(RAW))
    assert result.ok and "known_plates" in fake.raw


async def test_i9_send_error_message_is_never_empty(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake)
    aioclient_mock.clear_requests()
    aioclient_mock.post("http://f:5000/api/config/save?save_option=restart", status=500, text="unable to restart")
    fake.install(aioclient_mock)
    result = await ops.async_apply(change_for(RAW))
    assert not result.ok and result.message.strip() and "500" in result.message


# ---- I10: the restore button ------------------------------------------------------
async def test_i10_restore_button_walks_back_and_stops(hass, aioclient_mock, cfgdir):
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake)
    assert (await ops.async_apply(change_for(RAW))).ok
    after1 = fake.raw
    assert (await ops.async_apply(change_for(after1, debug_save_plates=True))).ok
    after2 = fake.raw
    assert (await ops.async_restore_latest()).ok and fake.raw == after1
    assert (await ops.async_restore_latest()).ok and fake.raw == RAW
    third = await ops.async_restore_latest()
    assert not third.ok and third.stage == "none" and fake.raw == RAW  # never forward again
    assert (await ops.async_apply(change_for(RAW))).ok  # a new write re-arms the button
    assert (await ops.async_restore_latest()).ok and fake.raw == RAW
    assert after2  # silence unused


async def test_i10_button_reports_non_ok_results(hass, env):
    entry, fake, rt = env
    await rt.ops._lock.acquire()
    try:
        with patch("custom_components.electrifix_plate_gate.button.persistent_notification.async_create") as note:
            await hass.services.async_call("button", "press", {"entity_id": "button.plate_gate_driveway_restore_frigate"}, blocking=True)
            await hass.async_block_till_done()
    finally:
        rt.ops._lock.release()
    assert note.called and "still running" in str(note.call_args).lower()
