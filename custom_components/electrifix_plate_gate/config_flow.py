"""Guided setup: connect → camera → plates → device → Frigate YAML."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AUTO_CLOSE,
    CONF_CAMERA,
    CONF_CLOSE_ON_LEAVING,
    CONF_CONFIRMED,
    CONF_COOLDOWN,
    CONF_DEVICE_ENTITY,
    CONF_DRY_RUN,
    CONF_ENABLED,
    CONF_MATCH_DISTANCE,
    CONF_NEAR_MISSES,
    CONF_OPEN_ON_ARRIVAL,
    CONF_PASSWORD,
    CONF_PEOPLE_TEXT,
    CONF_REPEAT_MODE,
    CONF_LOCK_ACK,
    CONF_REQUIRE_MOVING,
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
    DEFAULT_URL,
    DEVICE_DOMAINS,
    DOMAIN,
    EXAMPLE_NEAR_MISSES,
    REPEAT_MODES,
)
from . import backups
from .config_writer import Desired, WriterError, plan_change
from .frigate_api import (
    FrigateAdminRequired,
    FrigateAuthRequired,
    FrigateCannotConnect,
    FrigateClient,
    FrigateError,
    FrigateInfo,
    version_note,
)
from .frigate_ops import Candidate, WriteResult, render_benchmark_markdown
from .hardware import render_accuracy_markdown
from .models import MODEL_CATALOGUE, NOTES
from .hardware import HardwareReport, recommend, render_report_markdown, stream_host
from .plates import (
    PlateParseError,
    frigate_lpr_yaml,
    parse_candidates,
    parse_people,
    render_verdicts,
)

PLATE_OPTION_KEYS = (CONF_PEOPLE_TEXT, CONF_NEAR_MISSES, CONF_MATCH_DISTANCE)
DEVICE_OPTION_KEYS = (
    CONF_DEVICE_ENTITY, CONF_OPEN_ON_ARRIVAL, CONF_CLOSE_ON_LEAVING, CONF_COOLDOWN,
    CONF_REQUIRE_MOVING, CONF_AUTO_CLOSE, CONF_ZONES, CONF_REPEAT_MODE, CONF_LOCK_ACK,
)


def _plates_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PEOPLE_TEXT, default=current.get(CONF_PEOPLE_TEXT, "")): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Optional(CONF_NEAR_MISSES, default=current.get(CONF_NEAR_MISSES, EXAMPLE_NEAR_MISSES)): str,
            vol.Required(CONF_MATCH_DISTANCE, default=current.get(CONF_MATCH_DISTANCE, DEFAULT_MATCH_DISTANCE)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=2, step=1, mode=selector.NumberSelectorMode.SLIDER)
            ),
            vol.Required(CONF_CONFIRMED, default=False): selector.BooleanSelector(),
        }
    )


def _device_schema(current: dict[str, Any], zones: list[str]) -> vol.Schema:
    schema: dict[Any, Any] = {
        vol.Required(CONF_DEVICE_ENTITY, default=current.get(CONF_DEVICE_ENTITY, vol.UNDEFINED)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=DEVICE_DOMAINS)
        ),
        vol.Required(CONF_OPEN_ON_ARRIVAL, default=current.get(CONF_OPEN_ON_ARRIVAL, DEFAULT_OPEN_ON_ARRIVAL)): selector.BooleanSelector(),
        vol.Required(CONF_CLOSE_ON_LEAVING, default=current.get(CONF_CLOSE_ON_LEAVING, DEFAULT_CLOSE_ON_LEAVING)): selector.BooleanSelector(),
        vol.Required(CONF_COOLDOWN, default=current.get(CONF_COOLDOWN, DEFAULT_COOLDOWN)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=3600, step=5, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="s")
        ),
        vol.Required(CONF_REPEAT_MODE, default=current.get(CONF_REPEAT_MODE, DEFAULT_REPEAT_MODE)): selector.SelectSelector(
            selector.SelectSelectorConfig(options=REPEAT_MODES, mode=selector.SelectSelectorMode.LIST, translation_key=CONF_REPEAT_MODE)
        ),
        vol.Required(CONF_REQUIRE_MOVING, default=current.get(CONF_REQUIRE_MOVING, DEFAULT_REQUIRE_MOVING)): selector.BooleanSelector(),
        vol.Required(CONF_AUTO_CLOSE, default=current.get(CONF_AUTO_CLOSE, DEFAULT_AUTO_CLOSE)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=60, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min")
        ),
        vol.Optional(CONF_ZONES, default=current.get(CONF_ZONES, [])): selector.SelectSelector(
            selector.SelectSelectorConfig(options=zones, multiple=True, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN)
        ),
        vol.Required(CONF_LOCK_ACK, default=bool(current.get(CONF_LOCK_ACK, False))): selector.BooleanSelector(),
    }
    return vol.Schema(schema)


def _device_errors(cleaned: dict[str, Any]) -> dict[str, str]:
    """A lock needs the disclaimer box ticked."""
    if cleaned[CONF_DEVICE_ENTITY].startswith("lock.") and not cleaned[CONF_LOCK_ACK]:
        return {CONF_LOCK_ACK: "lock_ack_required"}
    return {}


def _clean_plate_input(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_PEOPLE_TEXT: user_input[CONF_PEOPLE_TEXT],
        CONF_NEAR_MISSES: user_input.get(CONF_NEAR_MISSES, ""),
        CONF_MATCH_DISTANCE: int(user_input.get(CONF_MATCH_DISTANCE, DEFAULT_MATCH_DISTANCE)),
    }


def _clean_device_input(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_DEVICE_ENTITY: user_input[CONF_DEVICE_ENTITY],
        CONF_OPEN_ON_ARRIVAL: bool(user_input.get(CONF_OPEN_ON_ARRIVAL, DEFAULT_OPEN_ON_ARRIVAL)),
        CONF_CLOSE_ON_LEAVING: bool(user_input.get(CONF_CLOSE_ON_LEAVING, DEFAULT_CLOSE_ON_LEAVING)),
        CONF_COOLDOWN: int(user_input.get(CONF_COOLDOWN, DEFAULT_COOLDOWN)),
        CONF_REQUIRE_MOVING: bool(user_input.get(CONF_REQUIRE_MOVING, DEFAULT_REQUIRE_MOVING)),
        CONF_AUTO_CLOSE: int(user_input.get(CONF_AUTO_CLOSE, DEFAULT_AUTO_CLOSE)),
        CONF_ZONES: list(user_input.get(CONF_ZONES) or []),
        CONF_REPEAT_MODE: user_input.get(CONF_REPEAT_MODE, DEFAULT_REPEAT_MODE),
        CONF_LOCK_ACK: bool(user_input.get(CONF_LOCK_ACK, False)),
    }


def _yaml_placeholders(people_text: str, camera: str, detect: str) -> dict[str, str]:
    return {
        "yaml": frigate_lpr_yaml(parse_people(people_text)),
        "camera": camera,
        "detect": detect,
    }


class _PlateStepsMixin:
    """The plates + device + yaml steps, shared by setup and options."""

    hass: HomeAssistant
    _plates: dict[str, Any]
    _device: dict[str, Any]

    def _handle_plates(self, user_input: dict[str, Any] | None, current: dict[str, Any]):
        """Return (errors, placeholders, cleaned) — cleaned is set only when the step is done."""
        placeholders = {"verdicts": "Enter the plates above, then submit to see the patterns and near-miss check.", "detail": ""}
        if user_input is None:
            return {}, placeholders, None
        errors: dict[str, str] = {}
        try:
            people = parse_people(user_input[CONF_PEOPLE_TEXT])
        except PlateParseError as err:
            placeholders["detail"] = str(err)
            return {CONF_PEOPLE_TEXT: "bad_plates"}, placeholders, None
        if not people:
            placeholders["detail"] = "no plates entered"
            return {CONF_PEOPLE_TEXT: "bad_plates"}, placeholders, None
        cleaned = _clean_plate_input(user_input)
        candidates = parse_candidates(cleaned[CONF_NEAR_MISSES])
        placeholders["verdicts"] = render_verdicts(people, candidates, cleaned[CONF_MATCH_DISTANCE]).replace("\n", "\n\n")
        key = (tuple((p.name, p.plates) for p in people), tuple(candidates), cleaned[CONF_MATCH_DISTANCE])
        shown_for = getattr(self, "_verdicts_shown_for", None)
        if not user_input.get(CONF_CONFIRMED) or shown_for != key:
            # the box only counts once the verdicts for exactly these values have been on screen
            self._verdicts_shown_for = key
            errors["base"] = "confirm_verdicts"
            return errors, placeholders, None
        return errors, placeholders, cleaned


async def _fetch_info(hass: HomeAssistant, url: str, username: str, password: str) -> FrigateInfo:
    return await FrigateClient(async_get_clientsession(hass), url, username, password).async_info()


def _camera_lines(info: FrigateInfo) -> str:
    return "\n\n".join(
        f"• {c.name} ({c.detect_width}x{c.detect_height} detect{', plate reader on' if c.lpr_enabled else ''})"
        for c in info.cameras.values()
    )


class PlateGateConfigFlow(_PlateStepsMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._connect: dict[str, Any] = {}
        self._info: FrigateInfo | None = None
        self._camera: str = ""
        self._plates: dict[str, Any] = {}
        self._device: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> PlateGateOptionsFlow:
        return PlateGateOptionsFlow()

    def _default_url(self) -> str:
        for entry in self.hass.config_entries.async_entries("frigate"):
            url = entry.data.get("url")
            if url:
                return str(url)
        return DEFAULT_URL

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if not self.hass.config_entries.async_entries("mqtt"):
            return self.async_abort(reason="mqtt_required")
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip()
            username = user_input.get(CONF_USERNAME, "") or ""
            password = user_input.get(CONF_PASSWORD, "") or ""
            try:
                self._info = await _fetch_info(self.hass, url, username, password)
            except FrigateAuthRequired:
                errors["base"] = "auth_required"
            except FrigateCannotConnect:
                errors["base"] = "cannot_connect"
            else:
                if not self._info.cameras:
                    errors["base"] = "no_cameras"
                else:
                    self._connect = {CONF_URL: url, CONF_USERNAME: username, CONF_PASSWORD: password}
                    return await self.async_step_camera()
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, self._default_url())): str,
                vol.Optional(CONF_USERNAME, default=(user_input or {}).get(CONF_USERNAME, "")): str,
                vol.Optional(CONF_PASSWORD, default=(user_input or {}).get(CONF_PASSWORD, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_camera(self, user_input: dict[str, Any] | None = None):
        assert self._info is not None
        if user_input is not None:
            self._camera = user_input[CONF_CAMERA]
            await self.async_set_unique_id(f"{self._connect[CONF_URL]}::{self._camera}")
            self._abort_if_unique_id_configured()
            return await self.async_step_plates()
        schema = vol.Schema(
            {
                vol.Required(CONF_CAMERA): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(self._info.cameras), mode=selector.SelectSelectorMode.DROPDOWN)
                )
            }
        )
        return self.async_show_form(
            step_id="camera", data_schema=schema,
            description_placeholders={"cameras": _camera_lines(self._info), "version_note": version_note(self._info.version)},
        )

    async def async_step_plates(self, user_input: dict[str, Any] | None = None):
        errors, placeholders, cleaned = self._handle_plates(user_input, self._plates)
        if cleaned is not None:
            self._plates = cleaned
            return await self.async_step_device()
        current = _clean_plate_input(user_input) if user_input else self._plates
        return self.async_show_form(
            step_id="plates", data_schema=_plates_schema(current), errors=errors, description_placeholders=placeholders
        )

    async def async_step_device(self, user_input: dict[str, Any] | None = None):
        assert self._info is not None
        errors: dict[str, str] = {}
        current = self._device
        if user_input is not None:
            current = _clean_device_input(user_input)
            errors = _device_errors(current)
            if not errors:
                self._device = current
                return await self.async_step_frigate_yaml()
        zones = self._info.cameras[self._camera].zones
        return self.async_show_form(step_id="device", data_schema=_device_schema(current, zones), errors=errors)

    async def async_step_frigate_yaml(self, user_input: dict[str, Any] | None = None):
        assert self._info is not None
        cam = self._info.cameras[self._camera]
        if user_input is not None:
            data = {**self._connect, CONF_CAMERA: self._camera, CONF_TOPIC_PREFIX: self._info.topic_prefix}
            options = {**self._plates, **self._device, CONF_ENABLED: DEFAULT_ENABLED, CONF_DRY_RUN: DEFAULT_DRY_RUN}
            return self.async_create_entry(title=f"Plate Gate: {self._camera}", data=data, options=options)
        return self.async_show_form(
            step_id="frigate_yaml",
            data_schema=vol.Schema({}),
            description_placeholders=_yaml_placeholders(
                self._plates[CONF_PEOPLE_TEXT], self._camera, f"{cam.detect_width}x{cam.detect_height}"
            ),
        )


class PlateGateOptionsFlow(_PlateStepsMixin, config_entries.OptionsFlow):
    """Options menu: plates/device, write to Frigate, restore, hardware, benchmark."""

    def __init__(self) -> None:
        self._plates: dict[str, Any] = {}
        self._device: dict[str, Any] = {}
        self._info: FrigateInfo | None = None
        self._raw: str | None = None
        self._detect_default = False
        self._write_shown_for: tuple | None = None
        self._change = None
        self._task: asyncio.Task | None = None
        self._result: WriteResult | None = None
        self._restore_path: str | None = None
        self._bench: tuple[list[Candidate], int] | None = None
        self._install_key: str | None = None
        self._accuracy_args: tuple[list[str], int] | None = None
        self._install_source: str | None = None
        self._install_result: dict | None = None

    def _runtime(self):
        return self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init", menu_options=["plates", "frigate_write", "frigate_restore", "hardware", "install_model", "benchmark", "accuracy"]
        )

    # ---- write to Frigate -------------------------------------------------------

    async def async_step_frigate_write(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if rt is None:
            return self.async_abort(reason="not_loaded")
        if rt.ops.busy:
            return self.async_abort(reason="busy")
        if self._raw is None:
            try:
                self._raw = await rt.ops.client.async_raw_config()
                cfg = await rt.ops.client.async_config()
            except FrigateAdminRequired:
                return self.async_abort(reason="admin_required")
            except FrigateAuthRequired:
                return self.async_abort(reason="auth_required")
            except FrigateCannotConnect:
                return self.async_abort(reason="cannot_connect")
            detect = ((cfg.get("cameras") or {}).get(rt.camera) or {}).get("detect") or {}
            self._detect_default = bool(detect.get("width") and detect.get("height"))
        values = {
            "detect_native": bool((user_input or {}).get("detect_native", self._detect_default)),
            "debug_save_plates": bool((user_input or {}).get("debug_save_plates", False)),
        }
        options = self.config_entry.options
        try:
            change = plan_change(
                self._raw,
                Desired(
                    people=rt.people,
                    match_distance=int(options.get(CONF_MATCH_DISTANCE, DEFAULT_MATCH_DISTANCE)),
                    debug_save_plates=values["debug_save_plates"],
                    camera=rt.camera,
                    detect_native=values["detect_native"],
                ),
            )
        except WriterError:
            return self.async_abort(reason="bad_yaml")
        key = (values["detect_native"], values["debug_save_plates"])
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get("confirm") and self._write_shown_for == key:
            if change.is_noop:
                errors["base"] = "nothing_to_change"  # stay on the screen so the options can be changed
            else:
                self._change = change
                return await self.async_step_frigate_apply()
        elif user_input is not None:
            errors["confirm"] = "confirm_required"
        self._write_shown_for = key
        schema = vol.Schema(
            {
                vol.Required("detect_native", default=values["detect_native"]): selector.BooleanSelector(),
                vol.Required("debug_save_plates", default=values["debug_save_plates"]): selector.BooleanSelector(),
                vol.Required("confirm", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="frigate_write",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "camera": rt.camera,
                "diff": f"```diff\n{change.diff}\n```" if change.diff else "(nothing to change)",
                "summary": "\n\n".join(f"• {line}" for line in change.summary) or "Nothing to change.",
                "warnings": "\n\n".join(f"⚠️ {line}" for line in change.warnings),
            },
        )

    async def async_step_frigate_apply(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if self._task is None:
            self._task = self.hass.async_create_task(rt.ops.async_apply(self._change))
        if not self._task.done():
            return self.async_show_progress(step_id="frigate_apply", progress_action="applying", progress_task=self._task)
        self._result = self._task_result()
        self._task = None
        return self.async_show_progress_done(next_step_id="frigate_write_done")

    def _task_result(self) -> WriteResult:
        assert self._task is not None
        if self._task.cancelled():
            return WriteResult(False, "error", "The operation was cancelled.")
        if (exc := self._task.exception()) is not None:
            return WriteResult(False, "error", f"Unexpected error: {exc}")
        return self._task.result()

    async def async_step_frigate_write_done(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        result = self._result or WriteResult(False, "error", "No result.")
        return self.async_show_form(
            step_id="frigate_write_done",
            data_schema=vol.Schema({}),
            description_placeholders={"result": result.message, "backup": result.backup_path or "-"},
        )

    # ---- restore -----------------------------------------------------------------

    async def async_step_frigate_restore(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if rt is None:
            return self.async_abort(reason="not_loaded")
        if rt.ops.busy:
            return self.async_abort(reason="busy")
        infos = await backups.async_list_backups(self.hass, rt.ops.url)
        if not infos:
            return self.async_abort(reason="no_backups")
        if user_input is not None and user_input.get("confirm"):
            self._restore_path = user_input["backup"]
            return await self.async_step_frigate_restoring()
        errors = {"confirm": "confirm_required"} if user_input is not None else {}
        options = [
            {
                "value": info.path,
                "label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.at))
                + " — " + (", ".join(info.summary[:2]) if info.summary else info.kind.replace("_", " ")),
            }
            for info in infos
        ]
        schema = vol.Schema(
            {
                vol.Required("backup", default=(user_input or {}).get("backup", infos[0].path)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Required("confirm", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="frigate_restore", data_schema=schema, errors=errors)

    async def async_step_frigate_restoring(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if self._task is None:
            self._task = self.hass.async_create_task(rt.ops.async_restore(self._restore_path))
        if not self._task.done():
            return self.async_show_progress(step_id="frigate_restoring", progress_action="restoring", progress_task=self._task)
        self._result = self._task_result()
        self._task = None
        return self.async_show_progress_done(next_step_id="frigate_restore_done")

    async def async_step_frigate_restore_done(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        result = self._result or WriteResult(False, "error", "No result.")
        return self.async_show_form(
            step_id="frigate_restore_done", data_schema=vol.Schema({}), description_placeholders={"result": result.message}
        )

    # ---- hardware / benchmark (filled in by later tasks) --------------------------

    async def async_step_hardware(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if rt is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        try:
            cfg = await rt.ops.client.async_config()
            stats = await rt.ops.client.async_stats()
        except FrigateAuthRequired:
            return self.async_abort(reason="auth_required")
        except FrigateCannotConnect:
            return self.async_abort(reason="cannot_connect")
        report = HardwareReport.from_frigate(cfg, stats)
        recs = recommend(report)
        rt.set_hardware(report, recs)
        return self.async_show_form(
            step_id="hardware", data_schema=vol.Schema({}),
            description_placeholders={"report": render_report_markdown(report, recs, companion=rt.companion.any, camera_stream=stream_host(cfg, rt.camera))},
        )

    # ---- install a model through the companion --------------------------------------

    def _model_label(self, rt, key: str) -> str:
        filename = MODEL_CATALOGUE[key].filename
        state = "installed" if rt.companion.has_model(filename) else "not on the box"
        return f"{key}: {NOTES.get(key, '')} ({state})"

    async def async_step_install_model(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if rt is None:
            return self.async_abort(reason="not_loaded")
        if rt.companion.best is None:
            return self.async_abort(reason="no_companion")
        if user_input is not None:
            self._install_key = user_input["model"]
            self._install_source = (user_input.get("source") or "").strip() or None
            return await self.async_step_installing()
        options = [{"value": k, "label": self._model_label(rt, k)} for k in MODEL_CATALOGUE]
        schema = vol.Schema(
            {
                vol.Required("model"): selector.SelectSelector(selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)),
                vol.Optional("source", default=rt.models_base_url): str,
            }
        )
        best = rt.companion.best
        return self.async_show_form(
            step_id="install_model", data_schema=schema,
            description_placeholders={"companion": f"{best.id} ({best.deployment}), folder {best.frigate_config_dir}/model_cache/plate_gate", "source": rt.models_base_url},
        )

    async def async_step_installing(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if self._task is None:
            self._task = self.hass.async_create_task(rt.async_install_model(self._install_key, self._install_source))
        if not self._task.done():
            return self.async_show_progress(step_id="installing", progress_action="installing", progress_task=self._task)
        try:
            self._install_result = self._task.result()
        except Exception as err:  # noqa: BLE001
            self._install_result = {"ok": False, "message": f"Unexpected error: {err}"}
        self._task = None
        return self.async_show_progress_done(next_step_id="install_model_done")

    async def async_step_install_model_done(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        res = getattr(self, "_install_result", None) or {"ok": False, "message": "No result."}
        return self.async_show_form(step_id="install_model_done", data_schema=vol.Schema({}), description_placeholders={"result": res["message"]})

    async def async_step_benchmark(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if rt is None:
            return self.async_abort(reason="not_loaded")
        if rt.ops.busy:
            return self.async_abort(reason="busy")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("ack"):
                errors["ack"] = "confirm_required"
            elif not user_input.get("candidates"):
                errors["candidates"] = "pick_one"
            else:
                cands = []
                for key in user_input["candidates"]:
                    if key == "detectors:2":
                        cands.append(Candidate("current model, 2 detectors", None, 2))
                    elif key in MODEL_CATALOGUE:
                        cands.append(Candidate(key, MODEL_CATALOGUE[key], None))
                self._bench = (cands, int(user_input.get("minutes", 3)))
                return await self.async_step_benchmarking()
        options = [{"value": k, "label": f"{k}: {NOTES.get(k, '')}" + (f" ({'installed' if rt.companion.has_model(MODEL_CATALOGUE[k].filename) else 'not on the box'})" if rt.companion.best else "")} for k in MODEL_CATALOGUE] + [
            {"value": "detectors:2", "label": "Current model with 2 detectors"}
        ]
        schema = vol.Schema(
            {
                vol.Required("candidates", default=(user_input or {}).get("candidates", [])): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, multiple=True, mode=selector.SelectSelectorMode.LIST)
                ),
                vol.Required("minutes", default=(user_input or {}).get("minutes", 3)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min")
                ),
                vol.Required("ack", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="benchmark", data_schema=schema, errors=errors, description_placeholders={"report": ""})

    # ---- accuracy benchmark (companion) ----------------------------------------------

    async def async_step_accuracy(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if rt is None:
            return self.async_abort(reason="not_loaded")
        best = rt.companion.best
        if best is None:
            return self.async_abort(reason="no_companion")
        installed = [m.get("filename") for m in best.models if m.get("filename")]
        if not installed:
            return self.async_abort(reason="no_models")
        if rt.ops.busy:
            return self.async_abort(reason="busy")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("ack"):
                errors["ack"] = "confirm_required"
            elif not user_input.get("models"):
                errors["models"] = "pick_one"
            else:
                self._accuracy_args = (list(user_input["models"]), int(user_input.get("max_images", 60)))
                return await self.async_step_accuracy_running()
        options = [{"value": f, "label": f} for f in installed]
        schema = vol.Schema(
            {
                vol.Required("models", default=(user_input or {}).get("models", installed)): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, multiple=True, mode=selector.SelectSelectorMode.LIST)
                ),
                vol.Required("max_images", default=(user_input or {}).get("max_images", 60)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=200, step=10, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required("ack", default=False): selector.BooleanSelector(),
            }
        )
        note = ("These are frames where Frigate already saw something, so the test favours the model that raised them "
                "and cannot count what nobody saw. Whole frames are letterboxed to the model size, a harder test than Frigate's "
                "motion-region crops, so 640 models gain a little on 320 ones. It compares candidates on YOUR scene; it is not ground truth. "
                "On a small machine it can take a while: fewer snapshots = faster.")
        return self.async_show_form(
            step_id="accuracy", data_schema=schema, errors=errors,
            description_placeholders={"camera": rt.camera, "companion": best.id, "note": note},
        )

    async def async_step_accuracy_running(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if self._task is None:
            models, max_images = self._accuracy_args
            self._task = self.hass.async_create_task(rt.async_accuracy_bench(models, max_images))
        if not self._task.done():
            return self.async_show_progress(step_id="accuracy_running", progress_action="accuracy", progress_task=self._task)
        try:
            self._install_result = self._task.result()
        except Exception as err:  # noqa: BLE001
            self._install_result = {"ok": False, "message": f"Unexpected error: {err}"}
        self._task = None
        return self.async_show_progress_done(next_step_id="accuracy_done")

    async def async_step_accuracy_done(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        res = self._install_result or {"ok": False, "message": "No result."}
        table = render_accuracy_markdown(res.get("data")) if res.get("ok") else res.get("message", "")
        return self.async_show_form(step_id="accuracy_done", data_schema=vol.Schema({}), description_placeholders={"table": table})

    async def async_step_benchmarking(self, user_input: dict[str, Any] | None = None):
        rt = self._runtime()
        if self._task is None:
            cands, minutes = self._bench
            self._task = self.hass.async_create_task(rt.ops.async_benchmark(cands, minutes))
        if not self._task.done():
            return self.async_show_progress(step_id="benchmarking", progress_action="benchmarking", progress_task=self._task)
        self._task = None
        return self.async_show_progress_done(next_step_id="benchmark_done")

    async def async_step_benchmark_done(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        rt = self._runtime()
        minutes = self._bench[1] if self._bench else 0
        table = render_benchmark_markdown(rt.ops.benchmark_rows, minutes, rt.ops.message if rt.ops.status != "ok" else "") if rt else "No result."
        return self.async_show_form(step_id="benchmark_done", data_schema=vol.Schema({}), description_placeholders={"table": table})

    async def async_step_plates(self, user_input: dict[str, Any] | None = None):
        options = self.config_entry.options
        current = {k: options[k] for k in PLATE_OPTION_KEYS if k in options}
        errors, placeholders, cleaned = self._handle_plates(user_input, current)
        if cleaned is not None:
            self._plates = cleaned
            return await self.async_step_device()
        if user_input:
            current = _clean_plate_input(user_input)
        return self.async_show_form(
            step_id="plates", data_schema=_plates_schema(current), errors=errors, description_placeholders=placeholders
        )

    async def async_step_device(self, user_input: dict[str, Any] | None = None):
        options = self.config_entry.options
        errors: dict[str, str] = {}
        current = {k: options[k] for k in DEVICE_OPTION_KEYS if k in options}
        if user_input is not None:
            current = _clean_device_input(user_input)
            errors = _device_errors(current)
            if not errors:
                self._device = current
                return await self.async_step_frigate_yaml()
        zones = list(options.get(CONF_ZONES) or [])
        data = self.config_entry.data
        try:
            self._info = await _fetch_info(
                self.hass, data[CONF_URL], data.get(CONF_USERNAME, ""), data.get(CONF_PASSWORD, "")
            )
            cam = self._info.cameras.get(data[CONF_CAMERA])
            if cam:
                zones = list(dict.fromkeys([*cam.zones, *zones]))
        except FrigateError:
            pass  # offline is fine here; the user can still type a zone name
        return self.async_show_form(step_id="device", data_schema=_device_schema(current, zones), errors=errors)

    async def async_step_frigate_yaml(self, user_input: dict[str, Any] | None = None):
        data = self.config_entry.data
        if user_input is not None:
            return self.async_create_entry(title="", data={**self.config_entry.options, **self._plates, **self._device})
        detect = "?"
        if self._info and data[CONF_CAMERA] in self._info.cameras:
            cam = self._info.cameras[data[CONF_CAMERA]]
            detect = f"{cam.detect_width}x{cam.detect_height}"
        return self.async_show_form(
            step_id="frigate_yaml",
            data_schema=vol.Schema({}),
            description_placeholders=_yaml_placeholders(self._plates[CONF_PEOPLE_TEXT], data[CONF_CAMERA], detect),
        )
