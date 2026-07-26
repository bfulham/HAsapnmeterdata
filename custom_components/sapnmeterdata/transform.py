"""Pandas transformations for SA Power Networks interval data."""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Iterable
from datetime import UTC, datetime

import pandas as pd

from .statistics import HourlyPoint, HourlyStream


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
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[HourlyPoint, ...]:
    """Aggregate intervals into Home Assistant's UTC-aligned hours."""
    index = pd.DatetimeIndex(pd.to_datetime(series.index))
    if index.tz is None:
        index = index.tz_localize(
            timezone_name,
            ambiguous="infer",
            nonexistent="shift_forward",
        )
    else:
        index = index.tz_convert(timezone_name)

    utc_index = index.tz_convert(UTC)
    normalized = pd.Series(series.to_numpy(), index=utc_index)
    hourly = normalized.groupby(utc_index.floor("h")).sum(min_count=1).dropna()

    if window_start is not None:
        hourly = hourly.loc[hourly.index >= pd.Timestamp(window_start)]
    if window_end is not None:
        hourly = hourly.loc[hourly.index < pd.Timestamp(window_end)]

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
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, HourlyStream]:
    """Extract UTC-hour consumption and return-to-grid streams for one NMI."""
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
            window_start,
            window_end,
        )
        if points:
            streams[direction] = HourlyStream(points=points, channels=channels)
    return streams
