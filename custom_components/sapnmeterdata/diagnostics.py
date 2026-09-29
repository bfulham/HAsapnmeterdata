"""Diagnostics for SA Power Networks Meter Data."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_METER_NAMES, CONF_NMIS
from .coordinator import SAPNConfigEntry

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_METER_NAMES, "title", "name", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: SAPNConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics with credentials, names, and NMIs removed."""
    coordinator = entry.runtime_data
    diagnostics = {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "status": async_redact_data(asdict(coordinator.data), TO_REDACT),
        "streams": coordinator.stream_diagnostics(),
    }
    # NMIs identify a customer's premises; replace them with stable aliases.
    text = json.dumps(diagnostics, default=str)
    for index, nmi in enumerate(entry.options.get(CONF_NMIS, []), start=1):
        # Longest form first: NEM12 drops the NMI's final checksum digit.
        for form in (nmi, nmi.lower(), nmi[:10]):
            text = text.replace(form, f"meter_{index}")
    return json.loads(text)
