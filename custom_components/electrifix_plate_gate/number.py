"""Cooldown number."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PlateGateEntity
from .runtime import PlateGateRuntime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: PlateGateRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CooldownNumber(runtime)])


class CooldownNumber(PlateGateEntity, NumberEntity):
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "cooldown")

    @property
    def native_value(self) -> float:
        return float(self.runtime.cooldown_seconds)

    async def async_set_native_value(self, value: float) -> None:
        await self.runtime.async_set_cooldown(int(value))
