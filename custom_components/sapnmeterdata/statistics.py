"""Pure helpers for SA Power Networks external statistics."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class HourlyPoint:
    """Energy recorded during one UTC-aligned hour."""

    start: datetime
    value: float


@dataclass(frozen=True, slots=True)
class HourlyStream:
    """An hourly energy stream and the channels that contributed to it."""

    points: tuple[HourlyPoint, ...]
    channels: tuple[str, ...]


def statistic_id(nmi: str, direction: str) -> str:
    """Return a stable Home Assistant external statistic ID."""
    safe_nmi = re.sub(r"[^a-z0-9_]", "_", str(nmi).lower()).strip("_")
    return f"sapnmeterdata:{safe_nmi}_{direction}"


def statistic_name(nmi: str, direction: str) -> str:
    """Return a user-facing statistic name."""
    label = "Grid consumption" if direction == "consumption" else "Return to grid"
    return f"SAPN {nmi} {label}"


def _row_timestamp(row: Mapping[str, Any]) -> float | None:
    """Return a statistics row timestamp in Unix seconds."""
    value = row.get("start")
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


def derive_base_sum(
    points: Iterable[HourlyPoint],
    existing_rows: Iterable[Mapping[str, Any]],
    last_rows: Iterable[Mapping[str, Any]],
) -> tuple[float, bool]:
    """Find the cumulative sum immediately before a forward import."""
    point_list = tuple(points)
    cumulative = 0.0
    existing = tuple(existing_rows)
    for point in point_list:
        cumulative += point.value
        point_timestamp = point.start.timestamp()
        for row in existing:
            row_timestamp = _row_timestamp(row)
            row_sum = row.get("sum")
            if (
                row_timestamp is not None
                and abs(row_timestamp - point_timestamp) < 0.5
                and isinstance(row_sum, (int, float))
            ):
                return float(row_sum) - cumulative, False

    usable_last_rows = [
        row
        for row in last_rows
        if _row_timestamp(row) is not None and isinstance(row.get("sum"), (int, float))
    ]
    if usable_last_rows:
        latest = max(usable_last_rows, key=lambda row: _row_timestamp(row) or 0.0)
        return float(latest["sum"]), False

    return 0.0, True


def derive_prepend_base_sum(
    points: Iterable[HourlyPoint],
    existing_rows: Iterable[Mapping[str, Any]],
    next_rows: Iterable[Mapping[str, Any]],
) -> float:
    """Find a base which joins older rows to the first newer statistic.

    Existing rows make a retried chunk deterministic. Otherwise, the final
    cumulative value in the historical chunk is joined to the cumulative
    value immediately before the next hourly statistic.
    """
    point_list = tuple(points)
    existing = tuple(existing_rows)
    if existing:
        return derive_base_sum(point_list, existing, [])[0]

    total = sum(point.value for point in point_list)
    usable_next_rows = [
        row
        for row in next_rows
        if _row_timestamp(row) is not None and isinstance(row.get("sum"), (int, float))
    ]
    if not usable_next_rows:
        return 0.0

    next_row = min(usable_next_rows, key=lambda row: _row_timestamp(row) or 0.0)
    next_sum = float(next_row["sum"])
    next_state = next_row.get("state")
    boundary_sum = (
        next_sum - float(next_state)
        if isinstance(next_state, (int, float))
        else next_sum
    )
    return boundary_sum - total


def build_statistics(
    points: Iterable[HourlyPoint],
    base_sum: float,
    include_baseline: bool,
) -> list[dict[str, Any]]:
    """Build hourly state values and a continuous cumulative sum."""
    point_list = tuple(points)
    if not point_list:
        return []

    rows: list[dict[str, Any]] = []
    if include_baseline:
        rows.append(
            {
                "start": point_list[0].start - timedelta(hours=1),
                "state": 0.0,
                "sum": float(base_sum),
                "last_reset": None,
            }
        )

    running_sum = float(base_sum)
    for point in point_list:
        running_sum += point.value
        rows.append(
            {
                "start": point.start,
                "state": point.value,
                "sum": running_sum,
                "last_reset": None,
            }
        )
    return rows
