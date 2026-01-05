import asyncio
import logging

from homeassistant import config_entries, core
from homeassistant.const import Platform
import sapnmeterdata

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Set up platform from a ConfigEntry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    entry.login = await hass.async_add_executor_job(sapnmeterdata.login, entry.data["email"], entry.data["password"])
    entry.meter = await hass.async_add_executor_job(sapnmeterdata.meter, entry.data["id"], entry.login)

    # Forward the setup to the sensor platform.
    hass.async_create_task(
        hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    )
    return True

async def options_update_listener(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry
):
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)

async def async_setup(hass: core.HomeAssistant, config: dict) -> bool:
    """Set up the GitHub Custom component from yaml configuration."""
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_remove_config_entry_device(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry
) -> bool:
    """Remove a config entry from a device."""
    return True