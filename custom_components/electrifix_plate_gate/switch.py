"""Enabled and Dry run switches."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PlateGateEntity
from .runtime import PlateGateRuntime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: PlateGateRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EnabledSwitch(runtime), DryRunSwitch(runtime)])


class EnabledSwitch(PlateGateEntity, SwitchEntity):
    _attr_icon = "mdi:boom-gate"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "enabled")

    @property
    def is_on(self) -> bool:
        return self.runtime.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_enabled(False)


class DryRunSwitch(PlateGateEntity, SwitchEntity):
    _attr_icon = "mdi:test-tube"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "dry_run")

    @property
    def is_on(self) -> bool:
        return self.runtime.dry_run

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_set_dry_run(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_dry_run(False)
