"""Fixtures for SA Power Networks Meter Data tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder

from .common import NOW, FakePortal


@pytest.fixture
def integration(recorder_mock: Recorder, enable_custom_integrations: None) -> None:
    """Start the recorder (which must precede hass), then allow custom_components.

    Tests that set up the integration request this fixture.
    """


@pytest.fixture
def frozen_time(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Run at 10:00 Adelaide time on 29 September 2026 (latest day: the 28th)."""
    freezer.move_to(NOW)
    return freezer


@pytest.fixture
def portal() -> Generator[FakePortal]:
    """Replace the SAPN client with an in-memory portal and remove delays."""
    fake = FakePortal()
    with (
        patch("custom_components.sapnmeterdata.coordinator.SAPNClient", fake.client),
        patch("custom_components.sapnmeterdata.config_flow.SAPNClient", fake.client),
        patch("custom_components.sapnmeterdata.coordinator.REQUEST_DELAY_SECONDS", 0),
        patch(
            "custom_components.sapnmeterdata.coordinator.CONNECTION_RETRY_SECONDS", 0
        ),
    ):
        yield fake
