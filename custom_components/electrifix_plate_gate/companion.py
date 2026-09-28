"""Plate Gate's view of the Companion(s): retained status → discovery; request/response over MQTT."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from datetime import timedelta

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)
PREFIX = "electrifix_plate_gate/companion"
STALE_S = 25 * 60  # 2.5 heartbeats: a retained "online" older than this is not believed


class CompanionError(Exception):
    """A refusal, failure or timeout; the message is for the user."""


@dataclass
class CompanionStatus:
    id: str
    version: str = ""
    online: bool = False
    deployment: str = ""
    frigate_config_dir: str = ""
    writable: bool = False
    models: list[dict] = field(default_factory=list)
    hardware: dict = field(default_factory=dict)
    updated: float = 0.0
    last_seen: float = 0.0

    @classmethod
    def from_payload(cls, cid: str, p: dict, previous: CompanionStatus | None) -> CompanionStatus:
        if p.get("online") is not True:  # LWT, clean shutdown, or a cleared/odd payload: offline
            base = previous or cls(id=cid)
            base.online = False
            base.last_seen = time.time()
            return base
        return cls(
            id=cid, version=str(p.get("version") or ""), online=True,
            deployment=str(p.get("deployment") or ""), frigate_config_dir=str(p.get("frigate_config_dir") or ""),
            writable=bool(p.get("writable")), models=list(p.get("models") or []), hardware=dict(p.get("hardware") or {}),
            updated=float(p.get("updated") or 0.0), last_seen=time.time(),
        )


class CompanionHub:
    """Discovers companions from their retained status and runs commands against them."""

    def __init__(self, hass: HomeAssistant, notify: Callable[[], None]) -> None:
        self.hass = hass
        self._notify = notify
        self.companions: dict[str, CompanionStatus] = {}
        self._pending: dict[str, tuple[asyncio.Future, Callable[[dict], None] | None]] = {}
        self._unsubs: list[Callable[[], None]] = []
        self.probe_timeout = 10.0  # quick liveness check before a long install wait
        self._stopping = False

    @staticmethod
    def is_live(c: CompanionStatus) -> bool:
        return c.online and (time.time() - c.last_seen) < STALE_S

    @property
    def best(self) -> CompanionStatus | None:
        online = sorted((c for c in self.companions.values() if self.is_live(c)), key=lambda c: c.id)
        return online[0] if online else None

    @property
    def any(self) -> CompanionStatus | None:
        if not self.companions:
            return None
        return self.best or sorted(self.companions.values(), key=lambda c: c.last_seen, reverse=True)[0]

    def has_model(self, filename: str) -> bool:
        best = self.best
        return bool(best and any(m.get("filename") == filename for m in best.models))

    async def async_start(self) -> None:
        self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{PREFIX}/+/status", self._on_status, 1))
        self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{PREFIX}/+/result/+", self._on_result, 1))
        # re-evaluate staleness every few minutes so the sensor goes 'offline' without a new message
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(minutes=5)))

    @callback
    def _tick(self, _now) -> None:
        self._notify()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._stopping = True
        for fut, _ in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    @callback
    def _on_status(self, msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload) if msg.payload else {}
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        parts = msg.topic.split("/")
        cid = str(payload.get("id") or (parts[2] if len(parts) > 2 else "")) or "unknown"
        self.companions[cid] = CompanionStatus.from_payload(cid, payload, self.companions.get(cid))
        self._notify()

    @callback
    def _on_result(self, msg: mqtt.ReceiveMessage) -> None:
        try:
            payload = json.loads(msg.payload)
        except (TypeError, ValueError):
            return
        rid = str(payload.get("req_id") or msg.topic.rsplit("/", 1)[-1])
        entry = self._pending.get(rid)
        if entry is None:
            return
        fut, on_progress = entry
        if not payload.get("done"):
            if on_progress and payload.get("progress"):
                on_progress(payload["progress"])
            return
        if fut.done():
            return
        if payload.get("ok"):
            fut.set_result(payload)
        else:
            fut.set_exception(CompanionError(str(payload.get("message") or "The companion reported a failure.")))

    async def async_command(
        self, cid: str, action: str, payload: dict | None = None, *, timeout: float = 600.0,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        status = self.companions.get(cid)
        if status is None:
            raise CompanionError("No companion is known. Install and start the Plate Gate Companion next to Frigate.")
        if not self.is_live(status):
            raise CompanionError(f"Companion '{cid}' is offline (or its last sign of life is stale). Start it and try again.")
        req_id = uuid.uuid4().hex
        fut: asyncio.Future = self.hass.loop.create_future()
        self._pending[req_id] = (fut, on_progress)
        try:
            await mqtt.async_publish(self.hass, f"{PREFIX}/{cid}/cmd", json.dumps({"req_id": req_id, "action": action, **(payload or {})}), 1, False)
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError as err:
                raise CompanionError(f"The companion did not answer within {timeout:g} s.") from err
            except asyncio.CancelledError:
                if self._stopping:
                    raise CompanionError("Plate Gate stopped while waiting for the companion.") from None
                raise
        finally:
            self._pending.pop(req_id, None)
