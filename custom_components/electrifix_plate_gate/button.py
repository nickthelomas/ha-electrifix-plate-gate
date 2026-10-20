"""Test-a-plate button: runs the engine on a made-up read, never actuates."""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME
from .entity import PlateGateEntity
from .runtime import PlateGateRuntime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime: PlateGateRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TestMatchButton(runtime), RestoreFrigateButton(runtime)])


class TestMatchButton(PlateGateEntity, ButtonEntity):
    _attr_icon = "mdi:car-search"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "test_match")

    async def async_press(self) -> None:
        await self.runtime.async_test_match()


class RestoreFrigateButton(PlateGateEntity, ButtonEntity):
    """One click: put back the Frigate config from before Plate Gate's last write (restarts Frigate)."""

    _attr_icon = "mdi:backup-restore"

    def __init__(self, runtime: PlateGateRuntime) -> None:
        super().__init__(runtime, "restore_frigate")

    async def async_press(self) -> None:
        result = await self.runtime.ops.async_restore_latest()
        if not result.ok:
            persistent_notification.async_create(
                self.hass, result.message, title=f"{NAME}: Restore Frigate",
                notification_id=f"{self.runtime.entry.entry_id}_restore_button",
            )
