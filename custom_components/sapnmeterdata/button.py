"""Manual import button for SA Power Networks Meter Data."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SAPNMeterDataCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the manual import button."""
    async_add_entities([SAPNMeterDataImportButton(entry, entry.runtime_data)])


class SAPNMeterDataImportButton(
    CoordinatorEntity[SAPNMeterDataCoordinator],
    ButtonEntity,
):
    """Retry importing SAPN's latest available day."""

    _attr_has_entity_name = True
    _attr_translation_key = "import_previous_day"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: SAPNMeterDataCoordinator,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_import_previous_day"

    async def async_press(self) -> None:
        """Request a safe, idempotent import."""
        await self.coordinator.async_request_refresh()
