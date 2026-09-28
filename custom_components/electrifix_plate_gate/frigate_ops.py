"""Frigate write operations: backup → save(restart) → verify → roll back; restore; benchmark.

The operations run in tasks that FrigateOps owns. A caller (an options-flow dialog) that goes away
mid-way does NOT stop them: the write always reaches its verify/rollback end. One operation at a time.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import backups
from .config_writer import CPU_DETECTOR_TYPES, Change, Desired, ModelChoice, frigate_model_block, load_yaml, plan_change
from .const import CONF_URL, NAME
from .frigate_api import FrigateClient, FrigateConfigInvalid, FrigateError

_LOGGER = logging.getLogger(__name__)
POLL_S = 3.0
GOOD_POLLS = 2
BACKGROUND_MSG = "The dialog was closed, but the operation is continuing in the background. Watch the Frigate status sensor."


@dataclass
class Candidate:
    label: str
    model: ModelChoice | None
    detector_count: int | None


@dataclass
class Row:
    label: str
    ok: bool
    inference_ms: float | None = None
    detection_fps: float | None = None
    skipped_fps: float | None = None
    cpu_pct: float | None = None
    detectors: int | None = None
    note: str = ""


@dataclass
class WriteResult:
    ok: bool
    stage: str  # noop | busy | precheck | stale | validate | save | verify | rollback | done | none | restore | background
    message: str
    backup_path: str | None = None
    rolled_back: bool = False
    seconds: float = 0.0


class FrigateOps:
    """Owns the write state machine for one Plate Gate."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: FrigateClient, camera: str, notify: Callable[[], None]) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.camera = camera
        self._notify = notify
        self.poll_s = POLL_S
        self.timeout_s = 120.0
        self.sample_s = 10.0
        self.samples_per_minute = 6
        self.status = "idle"
        self.stage = ""
        self.message = ""
        self.last_backup: str | None = None
        self.last_applied_at: float | None = None
        self.last_diff_summary: list[str] = []
        self.benchmark_rows: list[Row] = []
        self.benchmark_state = "idle"
        self.benchmark_current: str | None = None
        self.benchmark_ran_at: float | None = None
        self._lock = asyncio.Lock()
        self._current: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return str(self.entry.data.get(CONF_URL, ""))

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _set(self, status: str, stage: str = "", message: str = "") -> None:
        self.status, self.stage, self.message = status, stage, message
        _LOGGER.info("Plate Gate %s Frigate ops: %s/%s %s", self.entry.title, status, stage, message)
        self._notify()

    def _alert(self, msg: str) -> None:
        persistent_notification.async_create(self.hass, msg, title=f"{NAME}: Frigate needs attention", notification_id=f"{self.entry.entry_id}_frigate_failed")

    async def _run(self, coro: Coroutine[Any, Any, Any], background_value: Any):
        """Run an operation in a task WE own; a cancelled caller does not cancel the operation."""
        self._current = self.hass.async_create_task(coro)
        try:
            return await asyncio.shield(self._current)
        except asyncio.CancelledError:
            if self._current.done() or self._current.cancelling():
                raise  # the operation itself was cancelled (HA shutdown)
            _LOGGER.warning("Plate Gate %s: caller went away; the Frigate operation continues", self.entry.title)
            return background_value

    # ---- health ------------------------------------------------------------------

    async def _uptime(self) -> float | None:
        try:
            stats = await self.client.async_stats()
            return float((stats.get("service") or {}).get("uptime"))
        except (FrigateError, TypeError, ValueError):
            return None

    async def _camera_fps(self) -> float | None:
        """Current camera fps, or None when Frigate does not answer."""
        try:
            stats = await self.client.async_stats()
        except FrigateError:
            return None
        return float(((stats.get("cameras") or {}).get(self.camera) or {}).get("camera_fps") or 0)

    async def async_wait_healthy(
        self, *, expect_lpr: bool, expect_model_path: str | None, timeout: float, pre_uptime: float | None
    ) -> bool:
        """True once Frigate has visibly restarted (a gap or a lower uptime) AND the camera is
        streaming AND the config shows what we wrote, for GOOD_POLLS polls in a row."""
        deadline = time.monotonic() + timeout
        max_polls = max(GOOD_POLLS, int(timeout / self.poll_s)) if self.poll_s > 0 else 50
        saw_gap = False
        good = 0
        polls = 0
        while polls < max_polls and time.monotonic() <= deadline:
            polls += 1
            try:
                stats = await self.client.async_stats()
                uptime = (stats.get("service") or {}).get("uptime")
                fps = ((stats.get("cameras") or {}).get(self.camera) or {}).get("camera_fps") or 0
                restarted = saw_gap or (pre_uptime is not None and uptime is not None and float(uptime) < pre_uptime)
                healthy = float(fps) > 0
                if healthy and (expect_lpr or expect_model_path):
                    cfg = await self.client.async_config()
                    if expect_lpr:
                        cam_lpr = ((cfg.get("cameras") or {}).get(self.camera) or {}).get("lpr")
                        if not (cfg.get("lpr") or {}).get("enabled"):
                            healthy = False
                        if isinstance(cam_lpr, dict) and cam_lpr.get("enabled") is False:
                            healthy = False
                    if expect_model_path and (cfg.get("model") or {}).get("path") != expect_model_path:
                        healthy = False
                good = good + 1 if (restarted and healthy) else 0
                if good >= GOOD_POLLS:
                    return True
            except FrigateError:
                saw_gap = True
                good = 0
            await asyncio.sleep(self.poll_s)
        return False

    async def _answers_with(self, text: str) -> bool:
        """Frigate is up and its config file is ``text`` (used when verify fails but the camera is the problem)."""
        try:
            return (await self.client.async_raw_config()) == text and (await self.client.async_stats()) is not None
        except FrigateError:
            return False

    # ---- apply ------------------------------------------------------------------

    async def async_apply(self, change: Change, *, timeout: float | None = None, expect_model_path: str | None = None) -> WriteResult:
        if change.is_noop:
            return WriteResult(False, "noop", "Nothing to change: Frigate already has these settings.")
        if self.busy:
            return WriteResult(False, "busy", "Another Frigate operation is still running.")
        timeout = self.timeout_s if timeout is None else timeout
        return await self._run(self._locked_apply(change, timeout, expect_model_path), WriteResult(False, "background", BACKGROUND_MSG))

    async def _locked_apply(self, change: Change, timeout: float, expect_model_path: str | None) -> WriteResult:
        async with self._lock:
            return await self._apply(change, timeout, expect_model_path)

    async def _apply(
        self, change: Change, timeout: float, expect_model_path: str | None, *,
        expect_lpr: bool = True, kind: str = "before_write", save_backup: bool = True, expect_live: str | None = None,
    ) -> WriteResult:
        started = time.monotonic()
        # 1. pre-checks: the file must still be what the user saw, and the camera must be streaming
        try:
            live = await self.client.async_raw_config()
        except FrigateError as err:
            self._set("refused", "precheck", f"Frigate isn't answering: {err}")
            return WriteResult(False, "precheck", f"Frigate isn't answering ({err}); nothing was changed.")
        if live != (change.old_yaml if expect_live is None else expect_live):
            msg = "Frigate's config changed since the preview (someone or something edited it). Nothing was written; open the screen again to see the new diff."
            self._set("refused", "stale", msg)
            return WriteResult(False, "stale", msg)
        fps = await self._camera_fps()
        if fps is None or fps <= 0:
            msg = f"Camera {self.camera} isn't streaming right now (0 fps), so a restart could not be verified. Fix the camera first; nothing was changed."
            self._set("refused", "precheck", msg)
            return WriteResult(False, "precheck", msg)
        # 2. backup
        backup: str | None = None
        if save_backup:
            backup = str(await backups.async_save_backup(self.hass, self.url, change.old_yaml, change.summary, kind=kind))
            self.last_backup = backup
            self.last_diff_summary = list(change.summary)
        pre_uptime = await self._uptime()
        # 3. save (+ restart)
        self._set("applying", "save", "Sending the new config to Frigate")
        try:
            await self.client.async_save_config(change.new_yaml, restart=True)
        except FrigateConfigInvalid as err:
            self._set("failed", "validate", f"Frigate rejected the config, nothing was changed: {err.message}")
            return WriteResult(False, "validate", f"Frigate rejected the config, so nothing was changed: {err.message}", backup)
        except FrigateError as err:
            # the request failed, but Frigate may have stored the file and started restarting
            stored = False
            try:
                stored = (await self.client.async_raw_config()) == change.new_yaml
            except FrigateError:
                pass
            if not stored:
                msg = f"Could not send the config ({err}); Frigate still has the previous config."
                self._set("failed", "save", msg)
                return WriteResult(False, "save", msg, backup)
            _LOGGER.warning("Plate Gate %s: save request failed (%s) but Frigate stored the file; verifying", self.entry.title, err)
        # 4. verify
        self._set("verifying", "restart", "Frigate is restarting; waiting for the camera to come back")
        try:
            healthy = await self.async_wait_healthy(expect_lpr=expect_lpr, expect_model_path=expect_model_path, timeout=timeout, pre_uptime=pre_uptime)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - anything unexpected here means: roll back
            _LOGGER.exception("Plate Gate %s: verify crashed: %s", self.entry.title, err)
            healthy = False
        if healthy:
            seconds = round(time.monotonic() - started, 1)
            self.last_applied_at = time.time()
            self._set("ok", "done", f"Applied and verified in {seconds} s")
            return WriteResult(True, "done", f"Frigate restarted with the new settings and the camera is back ({seconds} s).", backup, seconds=seconds)
        # 5. roll back
        self._set("rolling_back", "rollback", "Frigate did not come back healthy; restoring the previous config")
        rolled = False
        try:
            pre_uptime = await self._uptime()
            await self.client.async_save_config(change.old_yaml, restart=True)
            rolled = await self.async_wait_healthy(expect_lpr=False, expect_model_path=None, timeout=timeout, pre_uptime=pre_uptime)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Plate Gate %s: rollback save failed: %s", self.entry.title, err)
        if rolled:
            msg = "Frigate did not come back healthy with the new settings, so the previous config was put back and Frigate is running again. Nothing changed."
            self._set("rolled_back", "verify", msg)
            return WriteResult(False, "verify", msg, backup, rolled_back=True)
        if await self._answers_with(change.old_yaml):
            msg = (f"Frigate did not come back healthy with the new settings. The previous config is back and Frigate answers, "
                   f"but camera {self.camera} isn't streaming. Check the camera; nothing else changed.")
            self._set("rolled_back", "verify", msg)
            return WriteResult(False, "verify", msg, backup, rolled_back=True)
        msg = (
            "Frigate did not come back healthy, and putting the previous config back did not bring it back either. "
            f"Your original config is saved at {backup or self.last_backup}. Check Frigate's logs; use the Restore button or the options menu once Frigate answers again."
        )
        self._set("failed", "rollback", msg)
        self._alert(msg)
        return WriteResult(False, "rollback", msg, backup, rolled_back=False)

    # ---- restore ----------------------------------------------------------------

    async def async_restore(self, backup_path: str, *, timeout: float | None = None, mark: bool = False) -> WriteResult:
        if self.busy:
            return WriteResult(False, "busy", "Another Frigate operation is still running.")
        timeout = self.timeout_s if timeout is None else timeout
        return await self._run(self._locked_restore(backup_path, timeout, mark), WriteResult(False, "background", BACKGROUND_MSG))

    async def _locked_restore(self, backup_path: str, timeout: float, mark: bool) -> WriteResult:
        async with self._lock:
            started = time.monotonic()
            try:
                text = await backups.async_read_backup(self.hass, backup_path)
            except OSError as err:
                self._set("failed", "restore", f"Backup unreadable: {err}")
                return WriteResult(False, "restore", str(err), backup_path)
            try:
                current = await self.client.async_raw_config()
                if current != text:
                    await backups.async_save_backup(self.hass, self.url, current, ["before restore"], kind="before_restore")
            except FrigateError:
                pass
            pre_uptime = await self._uptime()
            self._set("applying", "restore", "Sending the previous config to Frigate")
            try:
                await self.client.async_save_config(text, restart=True)
            except FrigateError as err:
                self._set("failed", "restore", f"Could not send the previous config: {err}")
                return WriteResult(False, "restore", f"Could not send the previous config: {err}", backup_path)
            if mark:
                await backups.async_mark_restored(self.hass, backup_path)
            self._set("verifying", "restart", "Frigate is restarting")
            if await self.async_wait_healthy(expect_lpr=False, expect_model_path=None, timeout=timeout, pre_uptime=pre_uptime):
                seconds = round(time.monotonic() - started, 1)
                self._set("ok", "done", f"Previous config restored in {seconds} s")
                return WriteResult(True, "done", f"Previous config restored; Frigate is back ({seconds} s).", backup_path, seconds=seconds)
            if await self._answers_with(text):
                msg = f"Previous config restored and Frigate answers, but camera {self.camera} isn't streaming. Check the camera."
                self._set("ok", "done", msg)
                return WriteResult(True, "done", msg, backup_path)
            msg = f"The previous config was sent but Frigate has not come back. Check Frigate's logs. The file is {backup_path}."
            self._set("failed", "restore", msg)
            self._alert(msg)
            return WriteResult(False, "restore", msg, backup_path)

    async def async_restore_latest(self) -> WriteResult:
        """One click: step back through the writes that actually landed, never forward.

        Eligible = 'before_write' backups not yet restored, whose content differs from what Frigate runs,
        and that sit outside the span already walked (newer than the newest restored one = a new write since;
        or older than the oldest restored one = the next step back)."""
        if self.busy:
            return WriteResult(False, "busy", "Another Frigate operation is still running.")
        try:
            current = await self.client.async_raw_config()
        except FrigateError:
            current = None
        infos = [i for i in await backups.async_list_backups(self.hass, self.url) if i.kind == "before_write"]
        if not infos:
            return WriteResult(False, "none", "No backup yet: Plate Gate has not written to this Frigate.")
        restored = [i for i in infos if i.restored_at]
        newest_r = max((i.at for i in restored), default=None)
        oldest_r = min((i.at for i in restored), default=None)
        for info in infos:  # newest first
            if info.restored_at:
                continue
            if restored and not (info.at > newest_r or info.at < oldest_r):
                continue
            try:
                text = await backups.async_read_backup(self.hass, info.path)
            except OSError:
                continue
            if current is not None and text == current:
                continue
            return await self.async_restore(info.path, mark=True)
        return WriteResult(False, "none", "Already back at the oldest config Plate Gate has; nothing further to restore. (A new write re-arms this button.)")

    # ---- benchmark ----------------------------------------------------------------

    async def _sample(self, minutes: float) -> dict:
        n = max(1, int(minutes * self.samples_per_minute))
        inf: list[float] = []
        fps: list[float] = []
        skipped: list[float] = []
        cpu: list[float] = []
        for i in range(n):
            try:
                stats = await self.client.async_stats()
                dets = [d for d in (stats.get("detectors") or {}).values() if isinstance(d, dict)]
                speeds = [float(d["inference_speed"]) for d in dets if d.get("inference_speed") is not None]
                if speeds:
                    inf.append(sum(speeds) / len(speeds))
                cpus = [float(d["cpu"]) for d in dets if d.get("cpu") is not None]
                if cpus:
                    cpu.append(sum(cpus))
                fps.append(float(stats.get("detection_fps") or 0.0))
                skipped.append(sum(float((c or {}).get("skipped_fps") or 0.0) for c in (stats.get("cameras") or {}).values()))
            except FrigateError:
                pass
            if i < n - 1:
                await asyncio.sleep(self.sample_s)
        mean = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
        return {"inference_ms": mean(inf), "detection_fps": mean(fps), "skipped_fps": mean(skipped), "cpu_pct": mean(cpu)}

    async def _restore_text(self, text: str, *, expect_lpr: bool) -> bool:
        try:
            if await self.client.async_raw_config() == text:
                return True
        except FrigateError:
            pass
        self._set("rolling_back", "benchmark", "Putting the original config back")
        try:
            pre_uptime = await self._uptime()
            await self.client.async_save_config(text, restart=True)
            ok = await self.async_wait_healthy(expect_lpr=expect_lpr, expect_model_path=None, timeout=self.timeout_s, pre_uptime=pre_uptime)
        except FrigateError as err:
            _LOGGER.error("Plate Gate %s: restoring the original config failed: %s", self.entry.title, err)
            ok = False
        if ok:
            self._set("ok", "done", "Original config restored")
        else:
            msg = "The benchmark finished but Frigate did not come back with the original config. Check Frigate's logs and use Restore from the options menu."
            self._set("failed", "benchmark", msg)
            self._alert(msg)
        return ok

    async def async_benchmark(self, candidates: list[Candidate], minutes: float) -> list[Row]:
        if self.busy:
            return []
        return await self._run(self._locked_benchmark(candidates, minutes), [])

    async def _locked_benchmark(self, candidates: list[Candidate], minutes: float) -> list[Row]:
        async with self._lock:
            self.benchmark_rows = []
            self.benchmark_state = "running"
            self.benchmark_current = None
            self._set("applying", "benchmark", "Reading the current config")
            try:
                original = await self.client.async_raw_config()
            except FrigateError as err:
                self.benchmark_state = "failed"
                self._set("failed", "benchmark", f"Could not read Frigate's config: {err}")
                return []
            await backups.async_save_backup(self.hass, self.url, original, [f"benchmark of {len(candidates)} candidate(s)"], kind="benchmark")
            expect_lpr = bool(((load_yaml(original).get("lpr") or {}).get("enabled")))
            rows: list[Row] = []
            live = original  # what Frigate is running right now (candidates are planned from the original)
            try:
                for cand in candidates:
                    self.benchmark_current = cand.label
                    self._notify()
                    try:
                        change = plan_change(original, Desired(people=[], camera=self.camera, model=cand.model, detector_count=cand.detector_count, touch_lpr=False))
                        n_det = len([d for d in (load_yaml(change.new_yaml).get("detectors") or {}).values() if isinstance(d, dict) and d.get("type") in CPU_DETECTOR_TYPES])
                        expect = frigate_model_block(cand.model)["path"] if cand.model else None
                        if change.is_noop:
                            samples = await self._sample(minutes)
                            rows.append(Row(cand.label, True, detectors=n_det, note="current config", **samples))
                        else:
                            result = await self._apply(change, self.timeout_s, expect, expect_lpr=expect_lpr, kind="benchmark", save_backup=False, expect_live=live)
                            if result.ok:
                                live = change.new_yaml
                                samples = await self._sample(minutes)
                                rows.append(Row(cand.label, True, detectors=n_det, **samples))
                            else:
                                if result.rolled_back or result.stage in ("verify", "rollback"):
                                    live = original
                                rows.append(Row(cand.label, False, detectors=n_det, note=result.message))
                    except FrigateError as err:
                        rows.append(Row(cand.label, False, note=str(err)))
                    self.benchmark_rows = list(rows)
                    self._notify()
                self.benchmark_state = "done"
            except BaseException:
                self.benchmark_state = "failed"
                raise
            finally:
                self.benchmark_current = None
                self.benchmark_ran_at = time.time()
                await self._restore_text(original, expect_lpr=expect_lpr)
            return rows


def render_benchmark_markdown(rows: list[Row], minutes: float, status_message: str = "") -> str:
    if not rows:
        return f"The benchmark did not run. {status_message}".strip()
    lines = [f"Each candidate ran for {minutes:g} minute(s) on your live cameras. Lower ms is faster; watch skipped frames.", "",
             "| Candidate | Result | Per check | Detections/s | Skipped/s | Detector CPU | Detectors |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.ok and r.inference_ms is not None:
            lines.append(f"| {r.label} | ok | {r.inference_ms} ms | {r.detection_fps} | {r.skipped_fps} | {r.cpu_pct}% | {r.detectors} |")
        elif r.ok:
            lines.append(f"| {r.label} | ran, no stats | – | – | – | – | {r.detectors} |")
        else:
            lines.append(f"| {r.label} | failed | – | – | – | – | {r.detectors or '–'} |")
    failed = [r for r in rows if not r.ok]
    if failed:
        lines.append("")
        for r in failed:
            lines.append(f"• {r.label}: {r.note}")
        lines.append("")
        lines.append("A failed candidate usually means the model file is not on the Frigate machine yet (see the hardware screen for the install command).")
    lines.append("")
    lines.append(status_message or "Your original config is back in place.")
    return "\n".join(lines)
