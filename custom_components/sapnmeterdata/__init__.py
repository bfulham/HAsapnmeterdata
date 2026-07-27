"""The SA Power Networks Meter Data integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_ID, CONF_PASSWORD, Platform
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_AVAILABLE_NMIS,
    CONF_CONSUMPTION_CHANNELS,
    CONF_NMI_NAMES,
    CONF_NMIS,
    CONF_RETURN_CHANNELS,
    DEFAULT_CONSUMPTION_CHANNELS,
    DEFAULT_RETURN_CHANNELS,
)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate the single-NMI 0.1.x configuration to 0.2.0."""
    if entry.version > 2:
        return False

    if entry.version == 1:
        nmi = str(entry.data[CONF_ID])
        email = entry.data[CONF_EMAIL]
        data = {
            CONF_EMAIL: email,
            CONF_PASSWORD: entry.data[CONF_PASSWORD],
            CONF_AVAILABLE_NMIS: [nmi],
            CONF_NMIS: [nmi],
            CONF_NMI_NAMES: {nmi: nmi},
            CONF_CONSUMPTION_CHANNELS: DEFAULT_CONSUMPTION_CHANNELS,
            CONF_RETURN_CHANNELS: DEFAULT_RETURN_CHANNELS,
        }
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            version=2,
            unique_id=f"{email.casefold()}:{nmi}",
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SA Power Networks Meter Data from a config entry."""
    # Import only when an entry is being set up. Python imports this package
    # before it opens config_flow.py, so a module-level coordinator import
    # would otherwise load the complete data stack when the user clicks Add.
    from .coordinator import SAPNMeterDataCoordinator

    coordinator = SAPNMeterDataCoordinator(hass, entry)
    startup_pending = hass.state is not CoreState.running
    if startup_pending:
        coordinator.async_set_updated_data(coordinator.startup_data())
    else:
        await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    if startup_pending:
        entry.async_on_unload(
            async_at_started(hass, coordinator.async_start_after_hass)
        )
    else:
        coordinator.async_start_daily_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload after options change."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
