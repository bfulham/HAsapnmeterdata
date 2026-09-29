"""Button for SA Power Networks Meter Data."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import SAPNConfigEntry
from .entity import SAPNAccountEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SAPNConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sync button."""
    async_add_entities([SAPNSyncButton(entry.runtime_data, "sync_now")])


class SAPNSyncButton(SAPNAccountEntity, ButtonEntity):
    """Check SAPN for new data now."""

    async def async_press(self) -> None:
        """Start a sync without waiting for it to finish."""
        self.coordinator.config_entry.async_create_background_task(
            self.hass, self.coordinator.async_request_refresh(), f"{DOMAIN} sync"
        )
