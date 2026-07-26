"""Status sensor for SA Power Networks Meter Data."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STATUS_OPTIONS
from .coordinator import SAPNMeterDataCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the import status sensor."""
    async_add_entities([SAPNMeterDataStatusSensor(entry, entry.runtime_data)])


class SAPNMeterDataStatusSensor(
    CoordinatorEntity[SAPNMeterDataCoordinator],
    SensorEntity,
):
    """Show the most recent import result."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_has_entity_name = True
    _attr_options = STATUS_OPTIONS
    _attr_translation_key = "import_status"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: SAPNMeterDataCoordinator,
    ) -> None:
        """Initialize the status sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_import_status"

    @property
    def native_value(self) -> str:
        """Return the most recent import status."""
        return self.coordinator.data["status"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose non-sensitive import details."""
        return {
            key: value
            for key, value in self.coordinator.data.items()
            if key != "status"
        }
