"""The SA Power Networks Meter Data integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LOGGER, STORE_VERSION
from .coordinator import SAPNConfigEntry, SAPNCoordinator

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SAPNConfigEntry) -> bool:
    """Set up SA Power Networks Meter Data from a config entry."""
    coordinator = SAPNCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Syncing can take several minutes when importing history, so it never
    # delays Home Assistant's startup.
    entry.async_on_unload(async_at_started(hass, coordinator.async_start))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SAPNConfigEntry) -> None:
    """Reload after the options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SAPNConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SAPNConfigEntry) -> None:
    """Delete saved import progress. Imported statistics are kept."""
    await Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: SAPNConfigEntry) -> bool:
    """Refuse entries created by versions before the 1.0 rewrite."""
    if entry.version < 5:
        LOGGER.error(
            "This SA Power Networks Meter Data entry was created by an earlier "
            "version and cannot be upgraded. Delete it and add the integration again"
        )
        return False
    return True
