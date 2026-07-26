"""Pure data transformation and scheduling helpers for SAPN meter data."""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class HourlyPoint:
    """Energy recorded during one local clock hour."""

    start: datetime
    value: float


@dataclass(frozen=True, slots=True)
class HourlyStream:
    """An hourly energy stream and the channels that contributed to it."""

    points: tuple[HourlyPoint, ...]
    channels: tuple[str, ...]


def parse_patterns(value: str | Iterable[str]) -> tuple[str, ...]:
    """Return normalized, non-empty channel patterns."""
    if isinstance(value, str):
        parts = re.split(r"[,;\s]+", value)
    else:
        parts = [str(part) for part in value]
    return tuple(part.strip().upper() for part in parts if part.strip())


def _matches(channel: str, patterns: tuple[str, ...]) -> bool:
    """Return whether a channel matches any configured glob."""
    normalized = channel.upper()
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def _channel_series(
    frame: pd.DataFrame,
    channels: tuple[str, ...],
) -> pd.Series:
    """Combine interval energy across selected channels."""
    selected = frame.loc[:, list(channels)].apply(pd.to_numeric, errors="coerce")
    return selected.sum(axis=1, min_count=1)


def _hourly_points(
    series: pd.Series,
    timezone_name: str,
) -> tuple[HourlyPoint, ...]:
    """Localize SAPN timestamps and aggregate interval energy by local hour."""
    index = pd.DatetimeIndex(pd.to_datetime(series.index))
    if index.tz is None:
        index = index.tz_localize(
            timezone_name,
            ambiguous="infer",
            nonexistent="shift_forward",
        )
    else:
        index = index.tz_convert(timezone_name)

    normalized = pd.Series(series.to_numpy(), index=index)
    # DatetimeIndex.floor() cannot reliably infer all repeated timestamps in
    # Adelaide's 25-hour DST day. Python datetimes retain their ``fold`` value,
    # so replacing the sub-hour fields preserves both distinct 02:00 hours.
    hour_index = pd.DatetimeIndex(
        timestamp.to_pydatetime().replace(minute=0, second=0, microsecond=0)
        for timestamp in normalized.index
    )
    hourly = normalized.groupby(hour_index).sum(min_count=1).dropna()

    return tuple(
        HourlyPoint(
            start=timestamp.to_pydatetime(),
            value=float(value),
        )
        for timestamp, value in hourly.items()
        if math.isfinite(float(value))
    )


def extract_hourly_streams(
    frame: pd.DataFrame,
    nmi: str,
    consumption_patterns: str | Iterable[str],
    return_patterns: str | Iterable[str],
    timezone_name: str,
) -> dict[str, HourlyStream]:
    """Extract consumption and return-to-grid hourly streams for one NMI."""
    if frame.empty:
        return {}
    if not isinstance(frame.columns, pd.MultiIndex) or frame.columns.nlevels < 2:
        raise ValueError("SAPN data does not have NMI/channel MultiIndex columns")

    nmi_columns = [column for column in frame.columns if str(column[0]) == str(nmi)]
    if not nmi_columns:
        raise ValueError(f"SAPN data does not contain columns for NMI {nmi}")

    by_channel = frame.loc[:, nmi_columns].copy()
    by_channel.columns = [str(column[1]) for column in nmi_columns]

    pattern_sets = {
        "consumption": parse_patterns(consumption_patterns),
        "return": parse_patterns(return_patterns),
    }
    streams: dict[str, HourlyStream] = {}
    for direction, patterns in pattern_sets.items():
        channels = tuple(
            channel for channel in by_channel.columns if _matches(channel, patterns)
        )
        if not channels:
            continue
        points = _hourly_points(
            _channel_series(by_channel, channels),
            timezone_name,
        )
        if points:
            streams[direction] = HourlyStream(points=points, channels=channels)
    return streams


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
    """Find the cumulative sum immediately before the first hourly point.

    Existing rows in the target period make repeated imports deterministic.
    Otherwise, the most recent cumulative sum continues the statistic. A brand
    new stream starts at zero and requests a baseline row.
    """
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


def build_statistics(
    points: Iterable[HourlyPoint],
    base_sum: float,
    include_baseline: bool,
) -> list[dict[str, Any]]:
    """Build cumulative Home Assistant statistics rows."""
    point_list = tuple(points)
    if not point_list:
        return []

    rows: list[dict[str, Any]] = []
    if include_baseline:
        rows.append(
            {
                "start": point_list[0].start - timedelta(hours=1),
                "state": float(base_sum),
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
                "state": running_sum,
                "sum": running_sum,
                "last_reset": None,
            }
        )
    return rows
