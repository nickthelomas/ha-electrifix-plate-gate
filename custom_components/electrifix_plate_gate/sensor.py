"""Last plate, last action and timing sensors."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PlateGateEntity
from .runtime import PlateGateRuntime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: PlateGateRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LastPlateSensor(runtime), LastActionSensor(runtime), TimingSensor(runtime), FrigateStatusSensor(runtime), HardwareSensor(runtime), BenchmarkSensor(runtime), CompanionSensor(runtime), AccuracySensor(runtime)])


class LastPlateSensor(PlateGateEntity, SensorEntity):
    _attr_icon = "mdi:card-text-outline"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "last_plate")

    @property
    def native_value(self) -> str | None:
        lp = self.runtime.last_plate
        return lp["plate"] if lp else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        lp = self.runtime.last_plate or {}
        return {k: lp.get(k) for k in ("raw", "person", "pattern", "score", "event_id", "camera", "seen_at")}


class LastActionSensor(PlateGateEntity, SensorEntity):
    _attr_icon = "mdi:garage-variant"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "last_action")

    @property
    def native_value(self) -> str | None:
        la = self.runtime.last_action
        return la["state"] if la else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        la = self.runtime.last_action or {}
        return {k: la.get(k) for k in ("reason", "person", "plate", "raw_plate", "event_id", "at", "message")}


class TimingSensor(PlateGateEntity, SensorEntity):
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "timing")

    @property
    def native_value(self) -> float | None:
        t = self.runtime.timing
        return t["total"] if t else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self.runtime.timing or {})


class FrigateStatusSensor(PlateGateEntity, SensorEntity):
    _attr_icon = "mdi:file-cog-outline"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "frigate_status")

    @property
    def native_value(self) -> str:
        return self.runtime.ops.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ops = self.runtime.ops
        return {
            "stage": ops.stage, "message": ops.message, "last_backup": ops.last_backup,
            "last_applied_at": ops.last_applied_at, "last_diff_summary": list(ops.last_diff_summary),
        }


class HardwareSensor(PlateGateEntity, SensorEntity):
    """The last hardware check: state = the recommended model (or 'basic'), attributes = the report."""

    _attr_icon = "mdi:chip"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "hardware")

    @property
    def native_value(self) -> str | None:
        recs = self.runtime.hardware_recs
        if not recs:
            return None
        top = recs[0]
        return top.model_key or top.kind

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self.runtime.hardware_report
        if report is None:
            return {}
        return {
            "frigate_version": report.frigate_version,
            "detectors": [asdict(d) for d in report.detectors],
            "detection_fps": report.detection_fps,
            "skipped_fps": report.skipped_fps,
            "busy_fraction": round(report.busy_fraction, 3),
            "cameras": {n: asdict(c) for n, c in report.cameras.items()},
            "coral_present": report.coral_present,
            "current_model": report.current_model_path,
            "lpr": asdict(report.lpr),
            "recommendations": [asdict(r) for r in self.runtime.hardware_recs],
        }


class BenchmarkSensor(PlateGateEntity, SensorEntity):
    _attr_icon = "mdi:speedometer"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "benchmark")

    @property
    def native_value(self) -> str:
        return self.runtime.ops.benchmark_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ops = self.runtime.ops
        return {"rows": [asdict(r) for r in ops.benchmark_rows], "ran_at": ops.benchmark_ran_at, "current": ops.benchmark_current}


class CompanionSensor(PlateGateEntity, SensorEntity):
    """none / online / offline, with what the companion last reported."""

    _attr_icon = "mdi:server-network"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "companion")

    @property
    def native_value(self) -> str:
        c = self.runtime.companion.any
        if c is None:
            return "none"
        return "online" if self.runtime.companion.is_live(c) else "offline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.runtime.companion.any
        if c is None:
            return {}
        return {"id": c.id, "version": c.version, "deployment": c.deployment, "frigate_config_dir": c.frigate_config_dir,
                "writable": c.writable, "models": list(c.models), "hardware": dict(c.hardware), "last_seen": c.last_seen}


class AccuracySensor(PlateGateEntity, SensorEntity):
    """The last accuracy benchmark (found-rate per model on the user's own snapshots)."""

    _attr_icon = "mdi:target"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "accuracy")

    @property
    def native_value(self) -> str:
        return self.runtime.accuracy_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self.runtime.accuracy or {}
        return {"camera": a.get("camera"), "images_used": a.get("images_used"), "skipped": a.get("skipped"),
                "rows": list(a.get("rows") or []), "ran_at": self.runtime.accuracy_ran_at}
