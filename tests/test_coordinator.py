"""Tests for syncing SAPN meter data into Home Assistant statistics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.sapnmeterdata.const import (
    CHANNEL_IGNORE,
    CONF_CHANNELS,
    DOMAIN,
    STATUS_ERROR,
    STATUS_UP_TO_DATE,
    STATUS_WAITING,
)
from custom_components.sapnmeterdata.portal import (
    SAPNAuthError,
    SAPNConnectionError,
    SAPNLoginFailedError,
    SAPNPortalError,
)
from custom_components.sapnmeterdata.series import day_start_utc
from custom_components.sapnmeterdata.statistics import (
    statistic_id,
    statistic_metadata,
)

from .common import (
    B1,
    E1,
    E2,
    FIRST,
    LATEST,
    NMI,
    NOW,
    ONE_DAY,
    OPTIONS,
    Q1,
    FakePortal,
    async_setup,
    channel,
)

pytestmark = pytest.mark.usefixtures("integration", "frozen_time")


async def async_sync(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run one more sync."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def async_rows(
    hass: HomeAssistant, stat_id: str
) -> list[tuple[datetime, float, float]]:
    """Return ``(start, state, sum)`` rows of a statistic."""
    await async_wait_recording_done(hass)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return [
        (datetime.fromtimestamp(row["start"], UTC), row["state"], row["sum"])
        for row in result.get(stat_id, [])
    ]


def assert_consistent(rows: list[tuple[datetime, float, float]]) -> None:
    """Every row's sum must be the running total of every state so far."""
    total = 0.0
    for start, state, running in rows:
        total += state
        assert running == pytest.approx(total, abs=1e-6), start


def hours(first: date, last: date) -> list[datetime]:
    """Return every UTC hour of NEM12 days ``first`` to ``last``."""
    start = day_start_utc(first)
    count = ((last - first).days + 1) * 24
    return [start + timedelta(hours=h) for h in range(count)]


def stored_stream(hass_storage: dict[str, Any], entry: MockConfigEntry, stat_id: str):
    """Return a stream's saved progress."""
    return hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["streams"][stat_id]


async def test_first_sync_imports_history(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """The first sync imports every enabled energy channel's history."""
    portal.add(NMI, "E1", FIRST, LATEST, kwh=0.01)
    portal.add(NMI, "B1", FIRST, LATEST, kwh=0.02)
    portal.add(NMI, "E2", FIRST, LATEST)
    portal.add(NMI, "Q1", FIRST, LATEST, uom="KVARH")

    entry = await async_setup(hass)

    rows = await async_rows(hass, E1)
    assert [start for start, _, _ in rows] == hours(FIRST, LATEST)
    assert {round(state, 6) for _, state, _ in rows} == {0.12}
    assert_consistent(rows)
    solar = await async_rows(hass, B1)
    assert len(solar) == 100 * 24
    assert_consistent(solar)
    assert await async_rows(hass, E2) == []  # set to Ignore
    assert await async_rows(hass, Q1) == []  # reactive energy

    # Newest first in 30-day chunks until 60 days without data, one login.
    ends = [LATEST - timedelta(days=30 * n) for n in range(6)]
    assert portal.requests == [(NMI, end - timedelta(days=29), end) for end in ends]
    assert portal.logins == 1

    status = entry.runtime_data.data
    assert status.state == STATUS_UP_TO_DATE
    assert status.meters[NMI].latest_day == LATEST
    registry = er.async_get(hass)
    sync_status = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_sync_status"
    )
    latest_data = registry.async_get_entity_id("sensor", DOMAIN, f"{NMI}_latest_data")
    assert hass.states.get(sync_status).state == STATUS_UP_TO_DATE
    assert hass.states.get(latest_data).state == LATEST.isoformat()


async def test_replaces_statistics_from_an_earlier_installation(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """Rows left under the same ID by an older version are removed."""
    async_add_external_statistics(
        hass,
        statistic_metadata(E1, "Old"),
        [StatisticData(start=datetime(2025, 1, 1, tzinfo=UTC), state=5.0, sum=999.0)],
    )
    await async_wait_recording_done(hass)
    portal.add(NMI, "E1", FIRST, LATEST)

    await async_setup(hass)

    rows = await async_rows(hass, E1)
    assert rows[0][0] == day_start_utc(FIRST)
    assert_consistent(rows)


async def test_daily_sync_appends_one_day(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    frozen_time: FrozenDateTimeFactory,
) -> None:
    """After history, each sync requests only the new day."""
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.add(NMI, "B1", FIRST, LATEST)
    entry = await async_setup(hass)
    portal.requests.clear()

    frozen_time.tick(ONE_DAY)
    portal.add(NMI, "E1", LATEST + ONE_DAY, LATEST + ONE_DAY, kwh=0.03)
    portal.add(NMI, "B1", LATEST + ONE_DAY, LATEST + ONE_DAY)
    await async_sync(hass, entry)

    assert portal.requests == [(NMI, LATEST + ONE_DAY, LATEST + ONE_DAY)]
    rows = await async_rows(hass, E1)
    assert [start for start, _, _ in rows] == hours(FIRST, LATEST + ONE_DAY)
    assert rows[-1][1] == pytest.approx(0.36)
    assert_consistent(rows)
    assert entry.runtime_data.data.meters[NMI].latest_day == LATEST + ONE_DAY


async def test_missing_day_does_not_block_newer_days(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    portal: FakePortal,
    frozen_time: FrozenDateTimeFactory,
) -> None:
    """A day SAPN has not published is retried while newer days import."""
    gap = LATEST - timedelta(days=2)
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.add(NMI, "B1", FIRST, LATEST)
    portal.remove(NMI, gap)
    entry = await async_setup(hass)

    assert entry.runtime_data.data.state == STATUS_UP_TO_DATE
    assert stored_stream(hass_storage, entry, E1)["pending"] == [gap.isoformat()]
    rows = await async_rows(hass, E1)
    assert len(rows) == 99 * 24
    assert_consistent(rows)

    # The next day arrives, and so does the missing one.
    frozen_time.tick(ONE_DAY)
    portal.add(NMI, "E1", gap, gap)
    portal.add(NMI, "B1", gap, gap)
    portal.add(NMI, "E1", LATEST + ONE_DAY, LATEST + ONE_DAY)
    portal.add(NMI, "B1", LATEST + ONE_DAY, LATEST + ONE_DAY)
    portal.requests.clear()
    await async_sync(hass, entry)

    assert portal.requests == [(NMI, gap, LATEST + ONE_DAY)]
    rows = await async_rows(hass, E1)
    assert [start for start, _, _ in rows] == hours(FIRST, LATEST + ONE_DAY)
    assert_consistent(rows)
    assert stored_stream(hass_storage, entry, E1)["pending"] == []


async def test_channel_missing_a_day_is_retried(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    portal: FakePortal,
    frozen_time: FrozenDateTimeFactory,
) -> None:
    """One channel lacking a day does not hold back the others."""
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.add(NMI, "B1", FIRST, LATEST)
    entry = await async_setup(hass)

    frozen_time.tick(ONE_DAY)
    new_day = LATEST + ONE_DAY
    portal.add(NMI, "E1", new_day, new_day)
    await async_sync(hass, entry)

    assert entry.runtime_data.data.state == STATUS_UP_TO_DATE
    assert (await async_rows(hass, E1))[-1][0] == hours(new_day, new_day)[-1]
    assert (await async_rows(hass, B1))[-1][0] == hours(LATEST, LATEST)[-1]
    assert stored_stream(hass_storage, entry, B1)["pending"] == [new_day.isoformat()]
    assert entry.runtime_data.data.meters[NMI].pending_days == 1

    portal.add(NMI, "B1", new_day, new_day)
    await async_sync(hass, entry)
    solar = await async_rows(hass, B1)
    assert solar[-1][0] == hours(new_day, new_day)[-1]
    assert_consistent(solar)
    assert entry.runtime_data.data.meters[NMI].pending_days == 0


async def test_estimated_day_is_corrected(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    portal: FakePortal,
) -> None:
    """Estimated readings are replaced when SAPN publishes actual ones."""
    portal.add(NMI, "E1", FIRST, LATEST - ONE_DAY)
    portal.add(NMI, "E1", LATEST, LATEST, kwh=0.05, quality="E52")
    portal.add(NMI, "B1", FIRST, LATEST)
    entry = await async_setup(hass)
    assert (await async_rows(hass, E1))[-1][1] == pytest.approx(0.6)
    assert stored_stream(hass_storage, entry, E1)["pending"] == [LATEST.isoformat()]

    portal.add(NMI, "E1", LATEST, LATEST, kwh=0.01, quality="A")
    portal.requests.clear()
    await async_sync(hass, entry)

    assert portal.requests == [(NMI, LATEST, LATEST)]
    rows = await async_rows(hass, E1)
    assert rows[-1][1] == pytest.approx(0.12)
    assert len(rows) == 100 * 24
    assert_consistent(rows)
    assert stored_stream(hass_storage, entry, E1)["pending"] == []


async def test_waiting_for_sapn(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """When yesterday is not published yet, check again in an hour."""
    portal.add(NMI, "E1", FIRST, LATEST - ONE_DAY)
    portal.add(NMI, "B1", FIRST, LATEST - ONE_DAY)
    entry = await async_setup(hass)

    status = entry.runtime_data.data
    assert status.state == STATUS_WAITING
    assert status.meters[NMI].waiting
    assert status.next_sync == NOW + timedelta(hours=1)
    assert entry.runtime_data.update_interval == timedelta(hours=1)


async def test_connection_error_backs_off(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """Connection problems are retried with increasing delays."""
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.login_errors = [SAPNConnectionError("down"), SAPNConnectionError("down")]
    entry = await async_setup(hass)

    status = entry.runtime_data.data
    assert status.state == STATUS_ERROR
    assert status.error == "down"
    assert status.next_sync == NOW + timedelta(minutes=15)
    await async_sync(hass, entry)
    assert entry.runtime_data.data.next_sync == NOW + timedelta(minutes=30)

    await async_sync(hass, entry)
    assert entry.runtime_data.data.state == STATUS_UP_TO_DATE
    assert entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_meter_error_is_reported_and_retried(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    portal: FakePortal,
    frozen_time: FrozenDateTimeFactory,
) -> None:
    """A portal error for one meter is shown and retried, not an auth failure."""
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.add(NMI, "B1", FIRST, LATEST)
    entry = await async_setup(hass)

    frozen_time.tick(ONE_DAY)
    portal.add(NMI, "E1", LATEST + ONE_DAY, LATEST + ONE_DAY)
    portal.add(NMI, "B1", LATEST + ONE_DAY, LATEST + ONE_DAY)
    portal.download_errors = [SAPNPortalError("Unexpected reply")]
    await async_sync(hass, entry)
    status = entry.runtime_data.data
    assert status.state == STATUS_ERROR
    assert "Unexpected reply" in status.meters[NMI].error
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    await async_sync(hass, entry)
    status = entry.runtime_data.data
    assert status.state == STATUS_UP_TO_DATE
    assert status.meters[NMI].error is None
    assert status.meters[NMI].latest_day == LATEST + ONE_DAY


async def test_rejected_login_starts_reauth(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """Only a definite rejection asks the user for a new password."""
    portal.add(NMI, "E1", FIRST, LATEST)
    entry = await async_setup(hass)
    portal.login_errors = [SAPNAuthError("Your login attempt has failed.")]
    await async_sync(hass, entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_repeated_unexplained_login_failures_start_reauth(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """Login failures without a reason only trigger reauth after three runs."""
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.login_errors = [SAPNLoginFailedError("login form again")] * 3
    entry = await async_setup(hass)
    await async_sync(hass, entry)
    assert entry.runtime_data.data.state == STATUS_ERROR
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    await async_sync(hass, entry)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_all_channels_ignored(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """A meter whose channels are all ignored is not downloaded."""
    portal.add(NMI, "E1", FIRST, LATEST)
    options = {
        **OPTIONS,
        CONF_CHANNELS: {NMI: {"E1": channel("General", CHANNEL_IGNORE)}},
    }
    entry = await async_setup(hass, options)
    assert portal.requests == []
    assert entry.runtime_data.data.state == STATUS_UP_TO_DATE


async def test_meter_without_known_channels_uses_defaults(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """Channels first seen after setup are imported using their defaults."""
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.add(NMI, "B1", FIRST, LATEST)
    portal.add(NMI, "K1", FIRST, LATEST)
    await async_setup(hass, {**OPTIONS, CONF_CHANNELS: {NMI: {}}})

    assert len(await async_rows(hass, E1)) == 2400
    assert len(await async_rows(hass, B1)) == 2400
    assert await async_rows(hass, statistic_id(NMI, "K1")) == []


async def test_progress_survives_restart(
    recorder_mock: Recorder, hass: HomeAssistant, portal: FakePortal
) -> None:
    """Reloading does not import history again."""
    portal.add(NMI, "E1", FIRST, LATEST)
    portal.add(NMI, "B1", FIRST, LATEST)
    entry = await async_setup(hass)
    portal.requests.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert portal.requests == []
    assert entry.runtime_data.data.state == STATUS_UP_TO_DATE
    assert entry.runtime_data.data.meters[NMI].latest_day == LATEST


async def test_old_entries_must_be_re_added(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Entries from before the rewrite are not migrated."""
    entry = MockConfigEntry(domain=DOMAIN, version=4, data={}, options={})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
