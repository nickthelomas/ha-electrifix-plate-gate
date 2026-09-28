"""ElectriFix Plate Gate — Frigate plate reads open your garage or gate, safely."""
from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_DRY_RUN, DOMAIN, NAME
from .runtime import PlateGateRuntime, structural_options

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the runtime and the entities for one Plate Gate."""
    runtime = PlateGateRuntime(hass, entry)
    await runtime.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    if entry.options.get(CONF_DRY_RUN, True):
        persistent_notification.async_create(
            hass,
            f"{entry.title} is in **dry run**: it will log what it would do but not move anything. "
            "Turn off its *Dry run* switch when the *Last action* sensor looks right.",
            title=NAME,
            notification_id=f"{DOMAIN}_{entry.entry_id}_dry_run",
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the runtime."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS) if PLATFORMS else True
    if ok:
        runtime: PlateGateRuntime | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if runtime:
            await runtime.async_stop()
    return ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on real option changes; just sync the live toggles otherwise."""
    runtime: PlateGateRuntime | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return
    if structural_options(dict(entry.options)) == runtime.structural:
        runtime.sync_volatile()
        return
    await hass.config_entries.async_reload(entry.entry_id)
