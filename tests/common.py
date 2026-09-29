"""Shared test data and a fake SAPN portal client."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sapnmeterdata.const import (
    CHANNEL_CONSUMPTION,
    CHANNEL_IGNORE,
    CHANNEL_RETURN,
    CONF_CHANNEL_NAME,
    CONF_CHANNEL_TYPE,
    CONF_CHANNELS,
    CONF_METER_NAMES,
    CONF_NMIS,
    DOMAIN,
)
from custom_components.sapnmeterdata.portal import MeterAssignment, SAPNNoDataError
from custom_components.sapnmeterdata.statistics import statistic_id

NMI = "20012345678"
NMI_2 = "20098765432"
EMAIL = "someone@example.com"
PASSWORD = "correct horse"

NOW = datetime(2026, 9, 29, 0, 30, tzinfo=UTC)  # 10:00 in Adelaide
LATEST = date(2026, 9, 28)
FIRST = LATEST - timedelta(days=99)
E1 = statistic_id(NMI, "E1")
B1 = statistic_id(NMI, "B1")
E2 = statistic_id(NMI, "E2")
Q1 = statistic_id(NMI, "Q1")
ONE_DAY = timedelta(days=1)


def channel(name: str, channel_type: str) -> dict[str, str]:
    """Return one channel's options."""
    return {CONF_CHANNEL_NAME: name, CONF_CHANNEL_TYPE: channel_type}


OPTIONS = {
    CONF_NMIS: [NMI],
    CONF_METER_NAMES: {NMI: "Home"},
    CONF_CHANNELS: {
        NMI: {
            "E1": channel("General", CHANNEL_CONSUMPTION),
            "B1": channel("Solar", CHANNEL_RETURN),
            "E2": channel("Hot water", CHANNEL_IGNORE),
        }
    },
}


async def async_setup(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Add the config entry and wait for its first sync."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        title=EMAIL,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
        options=options or OPTIONS,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def days(first: date, last: date) -> Iterable[date]:
    """Yield every day from ``first`` to ``last`` inclusive."""
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def nem12_300(
    day: date,
    values: list[float | None],
    quality: str = "A",
    updated: str = "20260101000000",
) -> str:
    """Return one 300 record."""
    fields = ["" if value is None else f"{value:.6f}" for value in values]
    return f"300,{day:%Y%m%d},{','.join(fields)},{quality},,,{updated},"


def nem12_200(nmi: str, suffix: str, uom: str = "KWH", interval: int = 5) -> str:
    """Return a 200 record in SAPN's style (10-character NMI)."""
    return f"200,{nmi[:10]},E1B1E2,{suffix},{suffix},,METER1,{uom},{interval:02d},"


def interval_meter(nmi: str = NMI, name: str = "Home") -> MeterAssignment:
    """Return an interval-capable meter assignment."""
    return MeterAssignment(
        nmi=nmi,
        description=name,
        address="1 Example St",
        customer_name=None,
        business_name=None,
        meter_type="COMMS4D",
        meter_type_description="Interval Meter",
    )


class FakePortal:
    """In-memory SAPN portal: meter data plus scripted failures."""

    def __init__(self) -> None:
        """Initialize an empty portal."""
        # (nmi, suffix) -> day -> (288 five-minute values, quality)
        self.data: dict[
            tuple[str, str], dict[date, tuple[list[float | None], str]]
        ] = {}
        self.uoms: dict[tuple[str, str], str] = {}
        self.assignments: list[MeterAssignment] = [interval_meter()]
        self.requests: list[tuple[str, date, date]] = []
        self.download_errors: list[Exception] = []
        self.login_errors: list[Exception] = []
        self.logins = 0

    def add(
        self,
        nmi: str,
        suffix: str,
        first: date,
        last: date,
        kwh: float = 0.01,
        quality: str = "A",
        uom: str = "KWH",
    ) -> None:
        """Publish ``kwh`` per five-minute interval for a range of days."""
        self.uoms[(nmi, suffix)] = uom
        channel = self.data.setdefault((nmi, suffix), {})
        for day in days(first, last):
            channel[day] = ([kwh] * 288, quality)

    def remove(self, nmi: str, day: date, suffix: str | None = None) -> None:
        """Withdraw a day (for one or every channel)."""
        for (data_nmi, data_suffix), channel in self.data.items():
            if data_nmi == nmi and suffix in (None, data_suffix):
                channel.pop(day, None)

    def nem12(self, nmi: str, first: date, last: date) -> str | None:
        """Return NEM12 text like SAPN's, or None when there is no data."""
        lines: list[str] = []
        for (data_nmi, suffix), channel in sorted(self.data.items()):
            selected = sorted(day for day in channel if first <= day <= last)
            if data_nmi != nmi or not selected:
                continue
            lines.append(nem12_200(nmi, suffix, self.uoms[(nmi, suffix)]))
            lines.extend(nem12_300(day, *channel[day]) for day in selected)
        return "\n".join([*lines, "900"]) + "\n" if lines else None

    def client(self, *args: object, **kwargs: object) -> FakeClient:
        """Stand in for the ``SAPNClient`` constructor."""
        return FakeClient(self)


class FakeClient:
    """Implements the ``SAPNClient`` interface against a ``FakePortal``."""

    def __init__(self, portal: FakePortal) -> None:
        """Initialize the client."""
        self._portal = portal

    async def login(self) -> None:
        """Sign in, or raise the next scripted login error."""
        self._portal.logins += 1
        if self._portal.login_errors:
            raise self._portal.login_errors.pop(0)

    async def get_assignments(self) -> list[MeterAssignment]:
        """Return the account's meters."""
        return list(self._portal.assignments)

    async def download_nem12(self, nmi: str, first_day: date, last_day: date) -> str:
        """Return NEM12 text, or raise the next scripted error."""
        self._portal.requests.append((nmi, first_day, last_day))
        if self._portal.download_errors:
            raise self._portal.download_errors.pop(0)
        text = self._portal.nem12(nmi, first_day, last_day)
        if text is None:
            raise SAPNNoDataError(f"No data for {nmi}")
        return text
