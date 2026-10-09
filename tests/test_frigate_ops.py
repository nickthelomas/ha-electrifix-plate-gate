"""Apply / verify / rollback / restore against a scripted fake Frigate."""
from unittest.mock import patch

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electrifix_plate_gate import backups
from custom_components.electrifix_plate_gate.config_writer import Desired, plan_change
from custom_components.electrifix_plate_gate.const import DOMAIN
from custom_components.electrifix_plate_gate.frigate_api import FrigateClient
from custom_components.electrifix_plate_gate.frigate_ops import FrigateOps
from custom_components.electrifix_plate_gate.plates import parse_people

from .fake_frigate import FakeFrigate

RAW = "mqtt:\n  host: broker\nlpr:\n  enabled: false\ncameras:\n  driveway:\n    detect:\n      width: 1920\n      height: 1080\n"
DATA = {"url": "http://f:5000", "username": "", "password": "", "camera": "driveway", "topic_prefix": "frigate"}


@pytest.fixture
def cfg(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def make_ops(hass, aioclient_mock, fake: FakeFrigate, notified: list):
    fake.install(aioclient_mock)
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options={}, title="Plate Gate: driveway", unique_id="x")
    entry.add_to_hass(hass)
    client = FrigateClient(async_get_clientsession(hass), "http://f:5000")
    ops = FrigateOps(hass, entry, client, "driveway", notify=lambda: notified.append(ops.status))
    ops.poll_s = 0.001
    return ops


def change_for(raw: str):
    return plan_change(raw, Desired(people=parse_people("Alex: XO520"), camera="driveway", detect_native=True))


async def test_apply_success_backs_up_saves_and_verifies(hass, aioclient_mock, cfg):
    fake, notified = FakeFrigate(RAW), []
    ops = make_ops(hass, aioclient_mock, fake, notified)
    result = await ops.async_apply(change_for(RAW), timeout=1.0)
    assert result.ok and result.stage == "done" and not result.rolled_back
    assert fake.saves and fake.saves[0][0] == "restart" and "known_plates" in fake.saves[0][1]
    assert fake.restarts == 1
    assert ops.status == "ok" and "applying" in notified and "verifying" in notified
    assert result.backup_path and (cfg / "electrifix_plate_gate" / "backups").exists()
    assert (await backups.async_read_backup(hass, result.backup_path)) == RAW
    assert ops.last_backup == result.backup_path and ops.last_diff_summary


async def test_invalid_config_changes_nothing(hass, aioclient_mock, cfg):
    fake, notified = FakeFrigate(RAW), []
    fake.reject_containing = "known_plates"
    ops = make_ops(hass, aioclient_mock, fake, notified)
    result = await ops.async_apply(change_for(RAW), timeout=1.0)
    assert not result.ok and result.stage == "validate" and "broken" in result.message
    assert fake.saves == [] and fake.restarts == 0 and fake.raw == RAW
    assert ops.status == "failed"


async def test_verify_timeout_rolls_back_to_old_text(hass, aioclient_mock, cfg):
    fake, notified = FakeFrigate(RAW), []
    ops = make_ops(hass, aioclient_mock, fake, notified)
    fake.on_save = lambda body: setattr(fake, "stay_down", "known_plates" in body)  # new config never comes back; old one does
    result = await ops.async_apply(change_for(RAW), timeout=0.05)
    assert not result.ok and result.stage == "verify" and result.rolled_back
    assert fake.raw == RAW and len(fake.saves) == 2 and fake.saves[1][1] == RAW
    assert ops.status == "rolled_back" and result.backup_path


async def test_rollback_failure_is_reported_with_backup_path(hass, aioclient_mock, cfg):
    fake, notified = FakeFrigate(RAW), []
    ops = make_ops(hass, aioclient_mock, fake, notified)
    fake.on_save = lambda body: setattr(fake, "stay_down", True)  # nothing brings it back
    with patch("custom_components.electrifix_plate_gate.frigate_ops.persistent_notification.async_create") as note:
        result = await ops.async_apply(change_for(RAW), timeout=0.05)
    assert not result.ok and result.stage == "rollback" and not result.rolled_back
    assert ops.status == "failed" and result.backup_path in result.message
    assert note.called and result.backup_path in str(note.call_args)


async def test_stale_frigate_cannot_pass_verify(hass, aioclient_mock, cfg):
    """If Frigate never actually restarts (uptime keeps climbing, no gap) verify must not pass."""
    fake, notified = FakeFrigate(RAW), []
    ops = make_ops(hass, aioclient_mock, fake, notified)
    fake.restart_on_save = False  # saved to disk, but the old process keeps running
    result = await ops.async_apply(change_for(RAW), timeout=0.05)
    assert not result.ok and result.stage in ("verify", "rollback") and fake.raw == RAW


async def test_restore_latest_and_busy(hass, aioclient_mock, cfg):
    fake, notified = FakeFrigate(RAW), []
    ops = make_ops(hass, aioclient_mock, fake, notified)
    first = await ops.async_apply(change_for(RAW), timeout=1.0)
    assert first.ok and "known_plates" in fake.raw
    result = await ops.async_restore_latest()
    assert result.ok and fake.raw == RAW and fake.restarts == 2
    assert not ops.busy
    assert await ops.async_restore_latest() is not None  # restores the same backup again, harmless


async def test_noop_change_is_refused_without_touching_frigate(hass, aioclient_mock, cfg):
    fake, notified = FakeFrigate(RAW), []
    ops = make_ops(hass, aioclient_mock, fake, notified)
    ch = change_for(RAW)
    noop = plan_change(ch.new_yaml, Desired(people=parse_people("Alex: XO520"), camera="driveway", detect_native=True))
    result = await ops.async_apply(noop, timeout=1.0)
    assert not result.ok and result.stage == "noop" and fake.saves == []


async def test_backups_keep_twenty_and_list_newest_first(hass, cfg):
    paths = []
    for i in range(23):
        paths.append(await backups.async_save_backup(hass, "http://f:5000", f"n: {i}\n", [f"change {i}"]))
    listed = await backups.async_list_backups(hass, "http://f:5000")
    assert len(listed) == 20 and listed[0].path == str(paths[-1]) and listed[0].summary == ["change 22"]
    assert await backups.async_read_backup(hass, listed[0].path) == "n: 22\n"


# ---- benchmark ----------------------------------------------------------------------
import asyncio  # noqa: E402

from custom_components.electrifix_plate_gate.frigate_ops import Candidate  # noqa: E402
from custom_components.electrifix_plate_gate.models import MODEL_CATALOGUE  # noqa: E402

RAW_DET = "mqtt:\n  host: broker\ndetectors:\n  ov:\n    type: openvino\n    device: CPU\nlpr:\n  enabled: true\ncameras:\n  driveway:\n    detect:\n      fps: 5\n"


def bench_ops(hass, aioclient_mock, fake):
    ops = make_ops(hass, aioclient_mock, fake, [])
    ops.timeout_s = 0.05
    ops.sample_s = 0.001
    ops.samples_per_minute = 3
    return ops


async def test_benchmark_runs_candidates_and_restores_original_even_on_failure(hass, aioclient_mock, cfg):
    fake = FakeFrigate(RAW_DET)
    fake.inference_ms = {"s-320": 5.6, "m-640": 42.4, "default": 20.0}
    fake.on_save = lambda body: setattr(fake, "stay_down", "m-640" in body)  # the m-640 file is "missing"
    ops = bench_ops(hass, aioclient_mock, fake)
    rows = await ops.async_benchmark(
        [Candidate("s-320", MODEL_CATALOGUE["yolov9-s-320"], None), Candidate("m-640", MODEL_CATALOGUE["yolov9-m-640"], None),
         Candidate("current + 2 detectors", None, 2)],
        minutes=1,
    )
    assert [r.ok for r in rows] == [True, False, True]
    assert abs(rows[0].inference_ms - 5.6) < 0.01 and rows[0].detection_fps > 0
    assert "did not come back" in rows[1].note.lower() or "rolled" in rows[1].note.lower()
    assert rows[2].detectors == 2
    assert fake.raw == RAW_DET  # original restored
    assert ops.status == "ok" and ops.benchmark_rows == rows and ops.benchmark_state == "done"
    assert fake.saves[-1][1] == RAW_DET


async def test_benchmark_cancelled_restores_original(hass, aioclient_mock, cfg):
    fake = FakeFrigate(RAW_DET)
    ops = bench_ops(hass, aioclient_mock, fake)
    ops.sample_s = 0.05
    ops.samples_per_minute = 40
    task = hass.async_create_task(ops.async_benchmark([Candidate("s-320", MODEL_CATALOGUE["yolov9-s-320"], None)], minutes=1))
    await asyncio.sleep(0.2)
    assert "s-320" in fake.raw
    # closing the dialog does NOT stop a benchmark (it finishes in the background); HA shutdown cancels the operation itself
    ops._current.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.raw == RAW_DET and ops.benchmark_state == "failed" and not ops.busy


async def test_benchmark_refused_while_busy(hass, aioclient_mock, cfg):
    fake = FakeFrigate(RAW_DET)
    ops = bench_ops(hass, aioclient_mock, fake)
    await ops._lock.acquire()
    try:
        rows = await ops.async_benchmark([Candidate("s-320", MODEL_CATALOGUE["yolov9-s-320"], None)], minutes=1)
    finally:
        ops._lock.release()
    assert rows == [] and fake.saves == []


async def test_restore_latest_skips_backups_identical_to_current(hass, aioclient_mock, cfg):
    """After a rolled-back write the newest backup equals the running config; the button must go one further back."""
    fake = FakeFrigate(RAW)
    ops = make_ops(hass, aioclient_mock, fake, [])
    first = await ops.async_apply(change_for(RAW), timeout=1.0)          # backup #1 = RAW, Frigate now has known_plates
    assert first.ok
    written = fake.raw
    fake.on_save = lambda body: setattr(fake, "stay_down", "debug_save_plates" in body)
    from custom_components.electrifix_plate_gate.config_writer import Desired, plan_change
    from custom_components.electrifix_plate_gate.plates import parse_people
    second = await ops.async_apply(plan_change(written, Desired(people=parse_people("Alex: XO520"), camera="driveway", debug_save_plates=True)), timeout=0.05)
    assert second.rolled_back and fake.raw == written                     # backup #2 = written (== current)
    fake.on_save = None
    result = await ops.async_restore_latest()
    assert result.ok and fake.raw == RAW
