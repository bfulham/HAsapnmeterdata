"""Base entities for SA Power Networks Meter Data."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SAPNCoordinator


class SAPNAccountEntity(CoordinatorEntity[SAPNCoordinator]):
    """An entity describing the SAPN portal account."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SAPNCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"SAPN {entry.title}",
            manufacturer=MANUFACTURER,
            model="Your Meter Data portal",
            entry_type=DeviceEntryType.SERVICE,
        )


class SAPNMeterEntity(CoordinatorEntity[SAPNCoordinator]):
    """An entity describing one meter (NMI)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SAPNCoordinator, nmi: str, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._nmi = nmi
        self._attr_unique_id = f"{nmi}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, nmi)},
            name=f"SAPN {coordinator.meter_name(nmi)}",
            manufacturer=MANUFACTURER,
            model="Interval meter",
            serial_number=nmi,
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    @property
    def available(self) -> bool:
        """Return whether the meter is still selected."""
        return super().available and self._nmi in self.coordinator.data.meters
