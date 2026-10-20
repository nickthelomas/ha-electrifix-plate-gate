"""Runtime glue: Frigate MQTT in → engine → device service call, timing, auto-close."""
from __future__ import annotations

import asyncio
import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    CONF_AUTO_CLOSE,
    CONF_CAMERA,
    CONF_CLOSE_ON_LEAVING,
    CONF_COOLDOWN,
    CONF_DEVICE_ENTITY,
    CONF_DRY_RUN,
    CONF_ENABLED,
    CONF_LAST_ACTION_AT,
    CONF_MATCH_DISTANCE,
    CONF_MODELS_BASE_URL,
    CONF_OPEN_ON_ARRIVAL,
    CONF_PEOPLE_TEXT,
    CONF_REPEAT_MODE,
    CONF_REQUIRE_MOVING,
    CONF_PASSWORD,
    CONF_TOPIC_PREFIX,
    CONF_URL,
    CONF_USERNAME,
    CONF_ZONES,
    DEFAULT_AUTO_CLOSE,
    DEFAULT_CLOSE_ON_LEAVING,
    DEFAULT_COOLDOWN,
    DEFAULT_DRY_RUN,
    DEFAULT_ENABLED,
    DEFAULT_MATCH_DISTANCE,
    DEFAULT_OPEN_ON_ARRIVAL,
    DEFAULT_REPEAT_MODE,
    DEFAULT_REQUIRE_MOVING,
    DEFAULT_TOPIC_PREFIX,
    DEVICE_MOVE_WINDOW,
    DOMAIN,
    REPEAT_COOLDOWN_AND_EVENT,
)
from .engine import (
    VEHICLE_LABELS,
    Decision,
    DeviceState,
    EngineMemory,
    FrigateEvent,
    Settings,
    decide,
    parse_frigate_event,
)
from .companion import CompanionError, CompanionHub
from .frigate_api import FrigateClient
from .frigate_ops import FrigateOps
from .plates import PlateParseError, normalise, parse_people

_LOGGER = logging.getLogger(__name__)

VOLATILE_OPTIONS = frozenset({CONF_ENABLED, CONF_DRY_RUN, CONF_COOLDOWN, CONF_LAST_ACTION_AT})
SILENT_REASONS = frozenset({"disabled", "other_camera", "not_vehicle", "event_ended", "no_plate"})
SERVICE_FOR = {
    ("cover", "open"): ("cover", "open_cover"),
    ("cover", "close"): ("cover", "close_cover"),
    ("switch", "trigger"): ("switch", "turn_on"),
    ("script", "trigger"): ("script", "turn_on"),
    ("lock", "unlock"): ("lock", "unlock"),
    ("lock", "lock"): ("lock", "lock"),
}
OPENING_ACTIONS = {"open", "unlock"}  # actions the auto-close timer undoes
RESTING_STATES = {"cover": ("open", "closed"), "lock": ("unlocked", "locked")}  # (still open?, closed again)
MAX_TRACKED_EVENTS = 50
UNKNOWN_STATES = frozenset({"unavailable", "unknown"})


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """One HA device per Plate Gate."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="ElectriFix",
        model="Plate Gate",
        configuration_url=entry.data.get(CONF_URL),
    )


def structural_options(options: dict[str, Any]) -> dict[str, Any]:
    """Options that need a reload when changed (everything but the live toggles)."""
    return {k: v for k, v in options.items() if k not in VOLATILE_OPTIONS}


class PlateGateRuntime:
    """Owns the MQTT subscription and the engine memory for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = entry.options
        self.enabled: bool = bool(opts.get(CONF_ENABLED, DEFAULT_ENABLED))
        self.dry_run: bool = bool(opts.get(CONF_DRY_RUN, DEFAULT_DRY_RUN))
        self.cooldown_seconds: int = int(opts.get(CONF_COOLDOWN, DEFAULT_COOLDOWN))
        self.device_entity: str = opts[CONF_DEVICE_ENTITY]
        self.camera: str = entry.data[CONF_CAMERA]
        try:
            self.people = parse_people(opts.get(CONF_PEOPLE_TEXT, ""))
        except PlateParseError as err:
            _LOGGER.error("Plate list could not be read, nothing will match: %s", err)
            self.people = []
        self.memory = EngineMemory(last_action_at=opts.get(CONF_LAST_ACTION_AT))  # survives restarts
        self._lock = asyncio.Lock()  # one Frigate update at a time: decide + act is atomic
        # The device's own last_changed is trusted only for moves after HA finished starting.
        # During start-up every entity's last_changed is simply "now", which would silence
        # Plate Gate for a whole cooldown after each restart (found in the sandbox).
        self._trust_last_changed_after: float | None = 0.0 if hass.state is CoreState.running else None
        self._availability_flap_at: float | None = None  # last_changed of an unavailable→real flip
        self.last_plate: dict[str, Any] | None = None
        self.last_action: dict[str, Any] | None = None
        self.timing: dict[str, Any] | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._events: dict[str, dict[str, float | None]] = {}
        self._pending_move: dict[str, Any] | None = None
        self._device_last_moved_at: float | None = None  # moves WE observed; never the entity's last_changed
        self._auto_close_cancel: Callable[[], None] | None = None
        self.structural = structural_options(dict(opts))
        self.companion = CompanionHub(hass, self._notify)
        self.hardware_report = None
        self.accuracy_state = "idle"
        self.accuracy: dict | None = None
        self.accuracy_ran_at: float | None = None
        self.hardware_recs: list = []
        self.ops = FrigateOps(
            hass, entry,
            FrigateClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data.get(CONF_USERNAME), entry.data.get(CONF_PASSWORD)),
            self.camera, notify=self._notify,
        )

    # ---- settings / state -------------------------------------------------

    @property
    def device_domain(self) -> str:
        return self.device_entity.split(".", 1)[0]

    def settings(self, *, force_dry_run: bool = False) -> Settings:
        opts = self.entry.options
        return Settings(
            camera=self.camera,
            people=self.people,
            match_distance=int(opts.get(CONF_MATCH_DISTANCE, DEFAULT_MATCH_DISTANCE)),
            device_domain=self.device_domain,
            open_on_arrival=bool(opts.get(CONF_OPEN_ON_ARRIVAL, DEFAULT_OPEN_ON_ARRIVAL)),
            close_on_leaving=bool(opts.get(CONF_CLOSE_ON_LEAVING, DEFAULT_CLOSE_ON_LEAVING)),
            cooldown_seconds=self.cooldown_seconds,
            require_moving=bool(opts.get(CONF_REQUIRE_MOVING, DEFAULT_REQUIRE_MOVING)),
            zones=list(opts.get(CONF_ZONES) or []),
            enabled=self.enabled,
            dry_run=self.dry_run or force_dry_run,
            once_per_event=opts.get(CONF_REPEAT_MODE, DEFAULT_REPEAT_MODE) == REPEAT_COOLDOWN_AND_EVENT,
        )

    def device_state(self) -> DeviceState:
        state = self.hass.states.get(self.device_entity)
        if state is None:
            return DeviceState(self.device_domain, "unavailable", None)
        last = self._device_last_moved_at
        if self._trust_last_changed_after is not None:
            lc = state.last_changed.timestamp()
            if lc > self._trust_last_changed_after and lc != self._availability_flap_at:
                last = lc if last is None else max(last, lc)
        return DeviceState(self.device_domain, state.state, last)

    def sync_volatile(self) -> None:
        """Pick up enabled/dry_run/cooldown from entry.options (after an external edit)."""
        opts = self.entry.options
        self.enabled = bool(opts.get(CONF_ENABLED, DEFAULT_ENABLED))
        self.dry_run = bool(opts.get(CONF_DRY_RUN, DEFAULT_DRY_RUN))
        self.cooldown_seconds = int(opts.get(CONF_COOLDOWN, DEFAULT_COOLDOWN))
        self._notify()

    async def _write_option(self, key: str, value: Any) -> None:
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, key: value})
        self._notify()

    async def async_set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)
        await self._write_option(CONF_ENABLED, self.enabled)

    async def async_set_dry_run(self, value: bool) -> None:
        self.dry_run = bool(value)
        await self._write_option(CONF_DRY_RUN, self.dry_run)

    async def async_set_cooldown(self, seconds: int) -> None:
        self.cooldown_seconds = int(seconds)
        await self._write_option(CONF_COOLDOWN, self.cooldown_seconds)

    @property
    def models_base_url(self) -> str:
        from .models import MODELS_BASE_URL  # local import: models imports config_writer

        return str(self.entry.options.get(CONF_MODELS_BASE_URL) or MODELS_BASE_URL).rstrip("/")

    async def async_install_model(self, key: str, base_url: str | None = None) -> dict:
        """Ask the companion to fetch a catalogue model. Runs in a task the ops object owns, so a
        closed dialog does not stop the wait and the status sensor always ends on the truth."""
        background = {"ok": False, "message": "The dialog was closed; the Companion carries on. Watch the Frigate status and Companion sensors."}
        return await self.ops._run(self._install_model(key, base_url), background)

    async def _install_model(self, key: str, base_url: str | None) -> dict:
        from .models import MODEL_CATALOGUE

        choice = MODEL_CATALOGUE[key]
        best = self.companion.best
        if best is None:
            return {"ok": False, "message": "No companion is online."}
        url = f"{(base_url or self.models_base_url).rstrip('/')}/{choice.filename}"

        def progress(p: dict) -> None:
            self.ops._set("applying", "install", f"Downloading {choice.filename}: {p.get('pct', 0)}% ({p.get('stage', '')})")

        try:
            self.ops._set("applying", "install", f"Checking the companion is awake")
            await self.companion.async_command(best.id, "probe", timeout=self.companion.probe_timeout)
            self.ops._set("applying", "install", f"Asking the companion to fetch {choice.filename}")
            result = await self.companion.async_command(
                best.id, "install_model",
                {"key": key, "url": url, "filename": choice.filename, "sha256": choice.sha256}, timeout=1800, on_progress=progress,
            )
        except CompanionError as err:
            self.ops._set("failed", "install", str(err))
            return {"ok": False, "message": str(err)}
        except asyncio.CancelledError:
            self.ops._set("failed", "install", f"The install of {choice.filename} was interrupted (Home Assistant stopping). Check the Companion sensor.")
            raise
        msg = str(result.get("message") or f"{choice.filename} installed.")
        self.ops._set("ok", "install", msg)
        return {"ok": True, "message": msg, "data": result.get("data")}

    async def async_accuracy_bench(self, filenames: list[str], max_images: int) -> dict:
        """Ask the companion to run the installed models over recent snapshots. Owned task (see install)."""
        background = {"ok": False, "message": "The dialog was closed; the Companion carries on. The Accuracy sensor fills in when it finishes."}
        return await self.ops._run(self._accuracy_bench(filenames, max_images), background)

    async def _accuracy_bench(self, filenames: list[str], max_images: int) -> dict:
        best = self.companion.best
        if best is None:
            return {"ok": False, "message": "No companion is online."}
        self.accuracy_state = "running"
        self._notify()

        def progress(p: dict) -> None:
            self.ops._set("applying", "accuracy", f"Accuracy benchmark: {p.get('stage', '')} ({p.get('pct', 0)}%)")

        try:
            await self.companion.async_command(best.id, "probe", timeout=self.companion.probe_timeout)
            self.ops._set("applying", "accuracy", "Accuracy benchmark starting on the Frigate machine")
            result = await self.companion.async_command(
                best.id, "accuracy_bench", {"camera": self.camera, "models": list(filenames), "max_images": int(max_images)},
                timeout=3600, on_progress=progress,
            )
        except CompanionError as err:
            self.accuracy_state = "failed"
            self.ops._set("failed", "accuracy", str(err))
            return {"ok": False, "message": str(err)}
        except asyncio.CancelledError:
            self.accuracy_state = "failed"
            self.ops._set("failed", "accuracy", "The accuracy benchmark was interrupted (Home Assistant stopping).")
            raise
        self.accuracy = dict(result.get("data") or {})
        self.accuracy_ran_at = time.time()
        self.accuracy_state = "done"
        msg = str(result.get("message") or "Accuracy benchmark finished.")
        self.ops._set("ok", "accuracy", msg)
        return {"ok": True, "message": msg, "data": self.accuracy}

    def set_hardware(self, report, recs) -> None:
        """Remember the last hardware check for the sensor."""
        self.hardware_report = report
        self.hardware_recs = list(recs)
        self._notify()

    # ---- listeners --------------------------------------------------------

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return _remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # ---- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        topic = f"{self.entry.data.get(CONF_TOPIC_PREFIX, DEFAULT_TOPIC_PREFIX)}/events"
        self._unsubs.append(await mqtt.async_subscribe(self.hass, topic, self._on_mqtt, 0))
        self._unsubs.append(
            async_track_state_change_event(self.hass, [self.device_entity], self._on_device_change)
        )
        self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop))
        await self.companion.async_start()
        if self._trust_last_changed_after is None:
            self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started))
        _LOGGER.debug("Plate Gate %s listening on %s for %s", self.entry.title, topic, self.device_entity)

    async def async_stop(self) -> None:
        await self.companion.async_stop()
        self._cancel_auto_close()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._listeners.clear()

    @callback
    def _on_stop(self, _event: Event) -> None:
        self._cancel_auto_close()

    @callback
    def _on_started(self, _event: Event) -> None:
        self._trust_last_changed_after = time.time()

    # ---- Frigate events ---------------------------------------------------

    @callback
    def _on_mqtt(self, msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except (TypeError, ValueError):
            _LOGGER.debug("Ignoring non-JSON payload on %s", msg.topic)
            return
        self.hass.async_create_task(self.async_handle_payload(payload))

    def _track(self, event: FrigateEvent, now: float) -> dict[str, float | None]:
        if event.id not in self._events and len(self._events) >= MAX_TRACKED_EVENTS:
            del self._events[next(iter(self._events))]
        t = self._events.setdefault(
            event.id, {"car_seen_at": event.start_time or now, "plate_at": None, "action_at": None, "device_moved_at": None}
        )
        if event.raw_plate and t["plate_at"] is None:
            t["plate_at"] = now
        return t

    @staticmethod
    def _timing_from(t: dict[str, float | None]) -> dict[str, Any]:
        seen, plate, action, moved = t["car_seen_at"], t["plate_at"], t["action_at"], t["device_moved_at"]
        out: dict[str, Any] = {"car_seen_at": seen, "plate_at": plate, "action_at": action, "device_moved_at": moved}
        out["plate_delay"] = round(plate - seen, 2) if plate is not None and seen is not None else None
        out["action_delay"] = round(action - plate, 2) if action is not None and plate is not None else None
        out["device_delay"] = round(moved - action, 2) if moved is not None and action is not None else None
        out["total"] = round(action - seen, 2) if action is not None and seen is not None else None
        return out

    async def async_handle_payload(self, payload: Any, *, force_dry_run: bool = False) -> Decision | None:
        event = parse_frigate_event(payload)
        if event is None:
            return None
        async with self._lock:
            return await self._async_handle_event(event, force_dry_run)

    def _claim_action(self, now: float) -> None:
        """Start the cooldown BEFORE the service call, and remember it across restarts.
        A second update arriving while the call is in flight, or after it failed, must see it."""
        self.memory.last_action_at = now
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_LAST_ACTION_AT: now}
        )

    async def _async_handle_event(self, event: FrigateEvent, force_dry_run: bool) -> Decision:
        now = time.time()
        decision = decide(event, self.device_state(), self.settings(force_dry_run=force_dry_run), self.memory, now)
        if event.camera != self.camera or event.label not in VEHICLE_LABELS:
            return decision
        t = self._track(event, now)
        if event.raw_plate or decision.person:
            self.last_plate = {
                "plate": normalise(event.raw_plate) if event.raw_plate else decision.plate,
                "raw": event.raw_plate,
                "person": decision.person,
                "pattern": decision.pattern,
                "score": event.plate_score,
                "event_id": event.id,
                "camera": event.camera,
                "seen_at": now,
            }
        base = {
            "reason": decision.reason, "person": decision.person, "plate": decision.plate,
            "raw_plate": decision.raw_plate, "event_id": event.id, "at": now,
        }
        if decision.would_act:
            if decision.acts:
                self._claim_action(now)
                self._pending_move = {"event_id": event.id, "action_at": now}
                if len(self.memory.acted_events) >= MAX_TRACKED_EVENTS:
                    del self.memory.acted_events[next(iter(self.memory.acted_events))]
                self.memory.acted_events[event.id] = decision.action
                try:
                    await self._actuate(decision.action)
                except Exception as exc:  # noqa: BLE001 - report, never retry a door
                    _LOGGER.error("Plate Gate %s: %s failed: %s", self.entry.title, decision.action, exc)
                    self.last_action = {**base, "state": "error", "message": str(exc)}
                    self._pending_move = None
                    self._notify()
                    return decision
                if decision.action in OPENING_ACTIONS:
                    self._schedule_auto_close()
                self.last_action = {**base, "state": decision.action}
                _LOGGER.info("Plate Gate %s: %s for %s (%s)", self.entry.title, decision.action, decision.person, event.raw_plate)
            else:
                self.last_action = {**base, "state": f"would_{decision.action}"}
                _LOGGER.info("Plate Gate %s DRY RUN: would %s for %s (%s)", self.entry.title, decision.action, decision.person, event.raw_plate)
            t["action_at"] = now
            self.timing = self._timing_from(t)
        elif decision.reason not in SILENT_REASONS:
            self.last_action = {**base, "state": "skipped"}
        self._notify()
        return decision

    async def _actuate(self, action: str) -> None:
        domain, service = SERVICE_FOR[(self.device_domain, action)]
        await self.hass.services.async_call(domain, service, {"entity_id": self.device_entity}, blocking=True)

    async def async_test_match(self) -> Decision | None:
        """Run the engine on a made-up read of the first configured plate. Never actuates."""
        if not self.people:
            return None
        now = time.time()
        zones = list(self.entry.options.get(CONF_ZONES) or [])
        payload = {
            "type": "update",
            "before": {},
            "after": {
                "id": f"test-{int(now)}", "camera": self.camera, "label": "car", "sub_label": None,
                "recognized_license_plate": self.people[0].plates[0], "recognized_license_plate_score": 1.0,
                "stationary": False, "current_zones": zones, "entered_zones": zones, "start_time": now,
            },
        }
        return await self.async_handle_payload(payload, force_dry_run=True)

    # ---- device feedback ----------------------------------------------------

    @callback
    def _on_device_change(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or (old is not None and old.state == new.state):
            return
        now = time.time()
        if old is not None and old.state not in UNKNOWN_STATES and new.state not in UNKNOWN_STATES:
            self._device_last_moved_at = now
        elif new.state not in UNKNOWN_STATES:
            # (re)appearing is not a move: remember this last_changed so device_state() ignores it
            self._availability_flap_at = new.last_changed.timestamp()
        if self._pending_move and now - self._pending_move["action_at"] <= DEVICE_MOVE_WINDOW:
            t = self._events.get(self._pending_move["event_id"])
            if t is not None:
                t["device_moved_at"] = now
                self.timing = self._timing_from(t)
            self._pending_move = None
        elif self._pending_move:
            self._pending_move = None
        if new.state == RESTING_STATES.get(self.device_domain, ("", ""))[1]:
            self._cancel_auto_close()
        self._notify()

    # ---- auto-close -----------------------------------------------------------

    def _schedule_auto_close(self) -> None:
        minutes = int(self.entry.options.get(CONF_AUTO_CLOSE, DEFAULT_AUTO_CLOSE))
        self._cancel_auto_close()
        if minutes <= 0 or self.device_domain not in RESTING_STATES:
            return
        self._auto_close_cancel = async_call_later(self.hass, minutes * 60, self._auto_close)

    def _cancel_auto_close(self) -> None:
        if self._auto_close_cancel:
            self._auto_close_cancel()
            self._auto_close_cancel = None

    @callback
    def _auto_close(self, _now: Any) -> None:
        self._auto_close_cancel = None
        state = self.hass.states.get(self.device_entity)
        if state is None or state.state != RESTING_STATES[self.device_domain][0]:
            return
        if not self.enabled or self.dry_run:
            _LOGGER.info("Plate Gate %s: auto-close skipped (disabled or dry run); the door stays as it is", self.entry.title)
            self.last_action = {"state": "skipped", "reason": "auto_close_cancelled", "person": None, "plate": None,
                                "raw_plate": None, "event_id": None, "at": time.time()}
            self._notify()
            return
        self.hass.async_create_task(self._async_auto_close())

    async def _async_auto_close(self) -> None:
        now = time.time()
        async with self._lock:
            self._claim_action(now)
            closing = "lock" if self.device_domain == "lock" else "close"
            try:
                await self._actuate(closing)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("Plate Gate %s: auto-close failed: %s", self.entry.title, exc)
                self.last_action = {"state": "error", "reason": "auto_close", "message": str(exc), "at": now}
                self._notify()
                return
        self.last_action = {"state": closing, "reason": "auto_close", "person": None, "plate": None,
                            "raw_plate": None, "event_id": None, "at": now}
        _LOGGER.info("Plate Gate %s: auto-closed after the timer", self.entry.title)
        self._notify()
