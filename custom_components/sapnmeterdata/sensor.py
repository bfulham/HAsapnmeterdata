"""Platform for TV integration."""
from __future__ import annotations

import logging

import voluptuous as vol
import pandas as pd
from datetime import datetime
from .const import DOMAIN, MANUFACTURER, KNOWN_COLUMNS

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

def getdata(meter, path):
    """Get data from SAPN meter."""
    data = meter.getdata(path)
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

    sensors = hass.async_add_executor_job(getdata, config_entry.meter, "/config/custom_components/sapnmeterdata/data")
    for sensor in sensors[1][0][1].columns.tolist():
        if sensor not in KNOWN_COLUMNS:
            async_add_entities([SAPNmeterdata(sensor, config_entry)])
                

class SAPNmeterdata(SensorEntity):
    """Representation of a SAPN meter."""

    def __init__(self, sensor, config_entry, location = "") -> None:
        """Initialize a SAPN meter."""
        self._sensor = config_entry.meter
        self._device_name = config_entry.data["name"]
        self._name = config_entry.data["name"] + " " + sensor
        self._sensor_name = sensor
        self._manufacturer = MANUFACTURER
        self._serialnumber = self._sensor.data["Serial Number"]
        self._unique_id = self._serialnumber
        self._device_class = SensorDeviceClass.TEMPERATURE
        self._state_class = SensorStateClass.MEASUREMENT
        self._unit = "°C"


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
        if self._sensor_name == "Build date":
            return datetime.strptime(self._state, "%b %d %Y")
        else:
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
        if self._location == "":
            self._state = self._sensor.data[self._sensor_name]
        else:
            self._state = self._sensor.data[self._location][self._sensor_name]