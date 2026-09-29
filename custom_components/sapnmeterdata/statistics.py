"""Write SAPN hourly energy into Home Assistant external statistics.

The recorder holds each hour's energy as the row ``state`` and a running
total as ``sum``. Every write recomputes ``sum`` from the last row before the
earliest changed hour through to the newest row, so late or corrected data
never leaves the running total inconsistent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .series import build_rows

# How far before a changed hour to look for the row that anchors the sum.
# A longer search is only needed after a gap of more than this.
ANCHOR_SEARCH = timedelta(days=90)
WRITE_BATCH = 2000


def statistic_id(nmi: str, channel: str) -> str:
    """Return the external statistic ID for one meter channel."""
    safe_nmi = re.sub(r"[^a-z0-9]+", "_", nmi.lower()).strip("_")
    safe_channel = re.sub(r"[^a-z0-9]+", "_", channel.lower()).strip("_")
    return f"{DOMAIN}:{safe_nmi}_{safe_channel}"


def statistic_metadata(stat_id: str, name: str) -> StatisticMetaData:
    """Return metadata for an hourly kWh statistic with a running sum."""
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": name,
        "source": DOMAIN,
        "statistic_id": stat_id,
        "unit_class": "energy",
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }


def _row_start(row: Mapping) -> datetime:
    start = row["start"]
    if isinstance(start, datetime):
        return start.astimezone(UTC)
    return datetime.fromtimestamp(start, UTC)


async def _async_rows(
    hass: HomeAssistant,
    stat_id: str,
    start: datetime,
    end: datetime | None,
) -> list[Mapping]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return list(result.get(stat_id, []))


async def _async_write(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    rows: list[tuple[datetime, float, float]],
) -> None:
    for index in range(0, len(rows), WRITE_BATCH):
        async_add_external_statistics(
            hass,
            metadata,
            [
                StatisticData(start=start, state=state, sum=total)
                for start, state, total in rows[index : index + WRITE_BATCH]
            ],
        )
    await get_instance(hass).async_block_till_done()


async def async_replace_statistics(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    points: Mapping[datetime, float],
) -> None:
    """Delete a statistic and write ``points`` as its complete history."""
    recorder = get_instance(hass)
    recorder.async_clear_statistics([metadata["statistic_id"]])
    await recorder.async_block_till_done()
    await _async_write(hass, metadata, build_rows(0.0, points))


async def async_merge_statistics(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    points: Mapping[datetime, float],
    history_start: datetime,
) -> None:
    """Add or replace hours and keep the running sum consistent.

    ``history_start`` is the first hour this statistic has ever held, which
    bounds the search for the row before the earliest changed hour.
    """
    if not points:
        return
    stat_id = metadata["statistic_id"]
    first = min(points)

    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    last_rows = last.get(stat_id, [])
    if not last_rows:
        await _async_write(hass, metadata, build_rows(0.0, points))
        return
    if _row_start(last_rows[0]) < first:
        # Common case: appending newer hours after everything stored.
        base = float(last_rows[0].get("sum") or 0.0)
        await _async_write(hass, metadata, build_rows(base, points))
        return

    # New or corrected hours overlap stored ones. Rebuild every row from the
    # first changed hour onwards, anchored on the row just before it.
    search_start = max(first - ANCHOR_SEARCH, history_start)
    stored = await _async_rows(hass, stat_id, search_start, None)
    before = [row for row in stored if _row_start(row) < first]
    if not before and search_start > history_start:
        before = await _async_rows(hass, stat_id, history_start, first)
    base = float(before[-1].get("sum") or 0.0) if before else 0.0

    states = {
        _row_start(row): float(row.get("state") or 0.0)
        for row in stored
        if _row_start(row) >= first
    }
    states.update(points)
    await _async_write(hass, metadata, build_rows(base, states))
