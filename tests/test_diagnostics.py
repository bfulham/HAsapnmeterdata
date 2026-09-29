"""Tests for diagnostics."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant

from custom_components.sapnmeterdata.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .common import EMAIL, FIRST, LATEST, NMI, PASSWORD, FakePortal, async_setup

pytestmark = pytest.mark.usefixtures("integration", "frozen_time")


async def test_diagnostics_hide_personal_details(
    hass: HomeAssistant, portal: FakePortal
) -> None:
    """Diagnostics include progress but no credentials, names, or NMIs."""
    portal.add(NMI, "E1", FIRST, LATEST)
    entry = await async_setup(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diagnostics)
    for secret in (EMAIL, PASSWORD, NMI, NMI[:10], "Home"):
        assert secret not in text
    stream = diagnostics["streams"]["sapnmeterdata:meter_1_e1"]
    assert stream["last"] == LATEST.isoformat()
    assert diagnostics["status"]["state"] == "up_to_date"
