"""Platform for TV integration."""
from __future__ import annotations

import logging

import voluptuous as vol
import pandas as pd
from datetime import datetime
from .const import DOMAIN, MANUFACTURER, KNOWN_COLUMNS
import os

from pprint import pformat

# Import the device class from the component that you want to support
import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import (PLATFORM_SCHEMA, SensorEntity, SensorDeviceClass, SensorStateClass)
from homeassistant.const import CONF_NAME, CONF_EMAIL, CONF_PASSWORD, CONF_ID
from homeassistant import config_entries, core
from homeassistant.helpers.device_registry import DeviceInfo

_LOGGER = logging.getLogger(DOMAIN)

# Validation of the user's configuration
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_EMAIL): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Required(CONF_ID): cv.positive_int,
})

async def getdata(meter, hass):
    """Get data from SAPN meter."""
    data = await hass.async_add_executor_job(meter.getdata)
    return data

async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    """Set up the sapn meters."""
    # Add devices
    config = hass.data[DOMAIN][config_entry.entry_id]
    _LOGGER.info(pformat(config))

    sensors = await getdata(config_entry.meter, hass)
    for sensor in sensors.columns.get_level_values(1).tolist():
        async_add_entities([SAPNmeterdata(sensor, config_entry, hass)])
                

class SAPNmeterdata(SensorEntity):
    """Representation of a SAPN meter."""

    def __init__(self, sensor, config_entry, hass) -> None:
        """Initialize a SAPN meter."""
        self._hass = hass
        self._meter = config_entry.meter
        self._device_name = config_entry.data["name"]
        self._name = config_entry.data["name"] + " " + sensor
        self._sensor_name = sensor
        self._manufacturer = MANUFACTURER
        self._serialnumber = config_entry.data["id"]
        self._unique_id = self._serialnumber
        self._device_class = SensorDeviceClass.ENERGY
        self._state_class = SensorStateClass.TOTAL_INCREASING
        self._unit = "kWh"
        self._state = None


    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._unique_id)
            },
            name=self._device_name,
            suggested_area="Outside",
            manufacturer=self._manufacturer,
            serial_number=self._serialnumber,
        )

    @property
    def name(self) -> str:
        """Return the display name of this device."""
        return self._name
    
    @property
    def state(self):
        return self._state
    
    @property
    def state_class(self) -> SensorStateClass:
        return self._state_class
    
    @property
    def device_class(self) -> SensorDeviceClass:
        return self._device_class
    
    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._unit
    
    @property
    def unique_id(self) -> str:
        return self._name

    async def async_update(self) -> None:
        """Fetch new state data for this display."""
        data = await getdata(self._meter, self._hass)
        _LOGGER.debug(pformat(data))
        _LOGGER.debug("Updating sensor %s to %s", self._sensor_name, data[(self._meter.nmi, self._sensor_name)].iloc[-1])
        self._state = data[(self._meter.nmi, self._sensor_name)].iloc[-1]