"""Base entity: one device per Plate Gate, updates pushed by the runtime."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .runtime import PlateGateRuntime, device_info


class PlateGateEntity(Entity):
    """Common wiring for every Plate Gate entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: PlateGateRuntime, key: str) -> None:
        self.runtime = runtime
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_device_info = device_info(runtime.entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.runtime.add_listener(self._runtime_changed))

    @callback
    def _runtime_changed(self) -> None:
        self.async_write_ha_state()
