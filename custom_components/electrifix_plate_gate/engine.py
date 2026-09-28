"""Pure decision engine: Frigate event + device state + settings -> Decision.

No Home Assistant imports. The runtime feeds this and performs the action.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .plates import Person, match_person

VEHICLE_LABELS = frozenset({"car", "truck", "motorcycle", "bus"})
BUSY_STATES = frozenset({"opening", "closing", "locking", "unlocking", "jammed", "unavailable", "unknown"})
# state-keyed transitions: (domain, current state) -> (action, needs_open_on_arrival?)
TRANSITIONS = {
    ("cover", "closed"): ("open", True), ("cover", "open"): ("close", False),
    ("lock", "locked"): ("unlock", True), ("lock", "unlocked"): ("lock", False),
}


@dataclass
class FrigateEvent:
    """The parts of a ``frigate/events`` payload the engine cares about."""

    id: str
    camera: str
    type: str  # "new" | "update" | "end"
    label: str
    sub_label: str | None
    raw_plate: str | None
    plate_score: float | None
    stationary: bool
    current_zones: list[str]
    entered_zones: list[str]
    start_time: float


def parse_frigate_event(payload) -> FrigateEvent | None:
    """Parse a ``frigate/events`` JSON payload. None if it is not a tracked-object event."""
    if not isinstance(payload, dict):
        return None
    after = payload.get("after")
    if not isinstance(after, dict) or not after.get("id"):
        return None
    sub = after.get("sub_label")
    if isinstance(sub, (list, tuple)):  # Frigate 0.18: [name, score]
        sub = sub[0] if sub else None
    return FrigateEvent(
        id=str(after["id"]),
        camera=str(after.get("camera", "")),
        type=str(payload.get("type", "update")),
        label=str(after.get("label", "")),
        sub_label=str(sub) if sub else None,
        raw_plate=after.get("recognized_license_plate") or None,
        plate_score=after.get("recognized_license_plate_score"),
        stationary=bool(after.get("stationary", False)),
        current_zones=list(after.get("current_zones") or []),
        entered_zones=list(after.get("entered_zones") or []),
        start_time=float(after.get("start_time") or 0.0),
    )


@dataclass
class DeviceState:
    """Snapshot of the target device."""

    domain: str
    state: str
    last_changed: float | None  # epoch seconds, None if unknown


@dataclass
class Settings:
    """Everything the user configured that the engine needs."""

    camera: str
    people: list[Person]
    match_distance: int = 0
    device_domain: str = "cover"
    open_on_arrival: bool = True
    close_on_leaving: bool = True
    cooldown_seconds: int = 180
    require_moving: bool = False
    zones: list[str] = field(default_factory=list)
    enabled: bool = True
    dry_run: bool = True
    once_per_event: bool = False  # also refuse a second action from the same Frigate event


@dataclass
class EngineMemory:
    """What the engine remembers between events."""

    last_action_at: float | None = None
    acted_events: dict[str, str] = field(default_factory=dict)  # event id -> action taken


@dataclass
class Decision:
    """What to do and why. ``reason`` names the rule that decided it."""

    action: str | None  # "open" | "close" | "trigger" | None
    reason: str
    person: str | None = None
    plate: str | None = None
    raw_plate: str | None = None
    pattern: str | None = None
    dry_run: bool = False

    @property
    def would_act(self) -> bool:
        return self.action is not None

    @property
    def acts(self) -> bool:
        return self.action is not None and not self.dry_run


def decide(
    event: FrigateEvent, device: DeviceState, settings: Settings, memory: EngineMemory, now: float
) -> Decision:
    """Apply the rules in order; the first one that fails names the reason."""
    if not settings.enabled:
        return Decision(None, "disabled")
    if event.camera != settings.camera:
        return Decision(None, "other_camera")
    if event.label not in VEHICLE_LABELS:
        return Decision(None, "not_vehicle")
    if event.type == "end":
        return Decision(None, "event_ended")

    person = plate = pattern = None
    if event.raw_plate:
        m = match_person(settings.people, event.raw_plate, settings.match_distance)
        if not m:
            return Decision(None, "no_match", raw_plate=event.raw_plate)
        person, plate, pattern = m.person, m.plate, m.pattern
    elif event.sub_label and any(p.name == event.sub_label for p in settings.people):
        person = event.sub_label  # older payloads: Frigate matched it, no raw read supplied
    else:
        return Decision(None, "no_plate")
    ctx = {"person": person, "plate": plate, "raw_plate": event.raw_plate, "pattern": pattern}

    if settings.require_moving and event.stationary:
        return Decision(None, "stationary", **ctx)
    if settings.zones and not (set(event.current_zones) | set(event.entered_zones)) & set(settings.zones):
        return Decision(None, "outside_zone", **ctx)
    if device.state in BUSY_STATES:
        return Decision(None, "device_busy", **ctx)

    if settings.device_domain in ("cover", "lock"):
        transition = TRANSITIONS.get((settings.device_domain, device.state))
        if transition is None:
            return Decision(None, "no_transition", **ctx)
        action, is_arrival = transition
        if (is_arrival and not settings.open_on_arrival) or (not is_arrival and not settings.close_on_leaving):
            return Decision(None, "no_transition", **ctx)
    else:
        if not settings.open_on_arrival:
            return Decision(None, "no_transition", **ctx)
        action = "trigger"
    if settings.once_per_event and event.id in memory.acted_events:
        return Decision(None, "same_event", **ctx)

    candidates = [t for t in (memory.last_action_at, device.last_changed) if t is not None]
    if candidates and now - max(candidates) < settings.cooldown_seconds:
        return Decision(None, "cooldown", **ctx)
    if settings.dry_run:
        return Decision(action, "dry_run", dry_run=True, **ctx)
    return Decision(action, "go", **ctx)
