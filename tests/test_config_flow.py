"""Tests for the config and options flows."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sapnmeterdata.const import (
    CHANNEL_CONSUMPTION,
    CHANNEL_IGNORE,
    CHANNEL_RETURN,
    CONF_CHANNELS,
    CONF_METER_NAMES,
    CONF_NMIS,
    DOMAIN,
)
from custom_components.sapnmeterdata.portal import (
    MeterAssignment,
    SAPNAuthError,
    SAPNConnectionError,
    SAPNPortalError,
)

from .common import (
    EMAIL,
    LATEST,
    NMI,
    NMI_2,
    OPTIONS,
    PASSWORD,
    FakePortal,
    channel,
    interval_meter,
)

pytestmark = pytest.mark.usefixtures("integration", "frozen_time")

SHED = MeterAssignment(
    nmi="20055555555",
    description="Shed",
    address=None,
    customer_name=None,
    business_name=None,
    meter_type="BASIC",
    meter_type_description="Basic Meter",
)


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Skip setting up entries created by the flows."""
    with patch(
        "custom_components.sapnmeterdata.async_setup_entry", return_value=True
    ) as mock:
        yield mock


@pytest.fixture
def account(portal: FakePortal) -> FakePortal:
    """An account with two interval meters and one basic meter."""
    portal.assignments = [
        interval_meter(NMI, "Home"),
        interval_meter(NMI_2, "Pump"),
        SHED,
    ]
    recent = LATEST - timedelta(days=13)
    portal.add(NMI, "E1", recent, LATEST)
    portal.add(NMI, "B1", recent, LATEST)
    portal.add(NMI, "Q1", recent, LATEST, uom="KVARH")
    return portal


async def test_full_flow(hass: HomeAssistant, account: FakePortal) -> None:
    """Sign in, choose meters, and name each discovered channel."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: f" {EMAIL} ", CONF_PASSWORD: PASSWORD}
    )
    assert result["step_id"] == "meters"
    assert (
        "Shed (20055555555): Basic Meter"
        in result["description_placeholders"]["excluded"]
    )
    nmi_options = result["data_schema"].schema[CONF_NMIS].config["options"]
    assert [option["value"] for option in nmi_options] == [NMI, NMI_2]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NMIS: [NMI, NMI_2]}
    )
    assert result["step_id"] == "channels"
    assert result["description_placeholders"] == {"meter": f"Home ({NMI})"}
    # Reactive energy (Q1) is not offered.
    assert [str(key) for key in result["data_schema"].schema] == [
        "B1 name",
        "B1 use",
        "E1 name",
        "E1 use",
    ]
    assert account.requests == [
        (NMI, LATEST - timedelta(days=13), LATEST),
        (NMI_2, LATEST - timedelta(days=13), LATEST),
    ]

    # The second meter has no recent data, so it has no channel step.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "B1 name": "Solar",
            "B1 use": CHANNEL_RETURN,
            "E1 name": " ",
            "E1 use": CHANNEL_CONSUMPTION,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    assert result["options"] == {
        CONF_NMIS: [NMI, NMI_2],
        CONF_METER_NAMES: {NMI: "Home", NMI_2: "Pump"},
        CONF_CHANNELS: {
            NMI: {
                "B1": channel("Solar", CHANNEL_RETURN),
                "E1": channel("E1", CHANNEL_CONSUMPTION),
            },
            NMI_2: {},
        },
    }
    assert result["result"].unique_id == EMAIL


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (SAPNAuthError("Your login attempt has failed."), "invalid_auth"),
        (SAPNConnectionError("timeout"), "cannot_connect"),
        (SAPNPortalError("maintenance"), "portal_error"),
        (RuntimeError("bug"), "unknown"),
    ],
)
async def test_login_errors(
    hass: HomeAssistant, account: FakePortal, error: Exception, key: str
) -> None:
    """Login problems are shown on the form, and the user can retry."""
    account.login_errors = [error]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": key}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["step_id"] == "meters"


async def test_unreadable_sample_uses_defaults(
    hass: HomeAssistant, account: FakePortal
) -> None:
    """A meter whose recent data cannot be read is set up with defaults."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    account.download_errors = [SAPNPortalError("Unexpected reply")]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NMIS: [NMI]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_CHANNELS] == {NMI: {}}


async def test_no_interval_meters(hass: HomeAssistant, account: FakePortal) -> None:
    """An account with only basic meters cannot be added."""
    account.assignments = [SHED]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "no_interval_meters"}


async def test_already_configured(hass: HomeAssistant, account: FakePortal) -> None:
    """The same account cannot be added twice."""
    MockConfigEntry(domain=DOMAIN, version=5, unique_id=EMAIL).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL.upper(), CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, account: FakePortal) -> None:
    """A new password can be entered after SAPN rejects the old one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "old"},
        options=OPTIONS,
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    account.login_errors = [SAPNAuthError("Your login attempt has failed.")]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_options_flow(hass: HomeAssistant, account: FakePortal) -> None:
    """Meters and channels can be changed later, keeping existing names."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
        options=OPTIONS,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NMIS: [NMI]}
    )
    assert result["step_id"] == "channels"
    schema = result["data_schema"].schema
    defaults = {str(key): key.default() for key in schema}
    # E2 is no longer in the recent data but is still configured.
    assert defaults == {
        "B1 name": "Solar",
        "B1 use": CHANNEL_RETURN,
        "E1 name": "General",
        "E1 use": CHANNEL_CONSUMPTION,
        "E2 name": "Hot water",
        "E2 use": CHANNEL_IGNORE,
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**defaults, "E2 use": CHANNEL_CONSUMPTION}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CHANNELS][NMI]["E2"] == channel(
        "Hot water", CHANNEL_CONSUMPTION
    )


async def test_options_flow_login_failure(
    hass: HomeAssistant, account: FakePortal
) -> None:
    """The options flow stops if the portal cannot be reached."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
        options=OPTIONS,
    )
    entry.add_to_hass(hass)
    account.login_errors = [SAPNConnectionError("timeout")]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
