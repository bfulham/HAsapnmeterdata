"""Sensors for SA Power Networks Meter Data.

Meter readings themselves are written to long-term statistics for the Energy
dashboard; these sensors only report how up to date that import is.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATUS_OPTIONS
from .coordinator import SAPNConfigEntry
from .entity import SAPNAccountEntity, SAPNMeterEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SAPNConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            SAPNSyncStatusSensor(coordinator, "sync_status"),
            *(
                SAPNLatestDataSensor(coordinator, nmi, "latest_data")
                for nmi in coordinator.nmis
            ),
        ]
    )


class SAPNSyncStatusSensor(SAPNAccountEntity, SensorEntity):
    """Outcome of the latest sync with the SAPN portal."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _unrecorded_attributes = frozenset({"last_success", "next_sync", "error"})

    @property
    def native_value(self) -> str:
        """Return the sync state."""
        return self.coordinator.data.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when the last and next syncs happen, and any error."""
        data = self.coordinator.data
        return {
            "last_success": data.last_success.isoformat()
            if data.last_success
            else None,
            "next_sync": data.next_sync.isoformat() if data.next_sync else None,
            "error": data.error,
        }


class SAPNLatestDataSensor(SAPNMeterEntity, SensorEntity):
    """Newest day imported for every channel of a meter."""

    _attr_device_class = SensorDeviceClass.DATE
    _unrecorded_attributes = frozenset({"pending_days", "waiting_for_sapn", "error"})

    @property
    def native_value(self) -> date | None:
        """Return the newest complete day."""
        return self.coordinator.data.meters[self._nmi].latest_day

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return days awaiting data from SAPN and any error."""
        meter = self.coordinator.data.meters[self._nmi]
        return {
            "pending_days": meter.pending_days,
            "waiting_for_sapn": meter.waiting,
            "error": meter.error,
        }
