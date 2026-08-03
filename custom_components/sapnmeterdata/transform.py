"""Pandas transformations for SA Power Networks interval data."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pandas as pd
from pytz.exceptions import AmbiguousTimeError

from .statistics import HourlyPoint, HourlyStream


def available_channels(frame: pd.DataFrame, nmi: str) -> tuple[str, ...]:
    """Return the NEM12 channels present for one NMI."""
    if frame.empty:
        return ()
    if not isinstance(frame.columns, pd.MultiIndex) or frame.columns.nlevels < 2:
        raise ValueError("SAPN data does not have NMI/channel MultiIndex columns")
    channels = {
        str(column[1]).strip().upper()
        for column in frame.columns
        if str(column[0]) == str(nmi) and str(column[1]).strip()
    }
    if not channels:
        raise ValueError(f"SAPN data does not contain columns for NMI {nmi}")
    return tuple(sorted(channels))


def _hourly_points(
    series: pd.Series,
    timezone_name: str,
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[HourlyPoint, ...]:
    """Aggregate intervals into Home Assistant's UTC-aligned hours."""
    index = pd.DatetimeIndex(pd.to_datetime(series.index))
    if index.tz is None:
        try:
            index = index.tz_localize(
                timezone_name,
                ambiguous="infer",
                nonexistent="shift_forward",
            )
        except AmbiguousTimeError:
            # NEM12 always contains a fixed number of intervals per date.
            # Some parser output therefore contains only one copy of the
            # repeated wall-clock hour when daylight saving ends, leaving
            # pandas nothing from which to infer the offset. Preserve those
            # readings by assigning the lone ambiguous hour to standard time.
            index = index.tz_localize(
                timezone_name,
                ambiguous=False,
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


def extract_hourly_channels(
    frame: pd.DataFrame,
    nmi: str,
    channels: Iterable[str],
    timezone_name: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, HourlyStream]:
    """Extract a separate UTC-hour stream for every enabled NEM12 channel."""
    if frame.empty:
        return {}

    nmi_columns = [column for column in frame.columns if str(column[0]) == str(nmi)]
    if not nmi_columns:
        raise ValueError(f"SAPN data does not contain columns for NMI {nmi}")

    by_channel = frame.loc[:, nmi_columns].copy()
    by_channel.columns = [str(column[1]).strip().upper() for column in nmi_columns]

    streams: dict[str, HourlyStream] = {}
    for raw_channel in channels:
        channel = str(raw_channel).strip().upper()
        if channel not in by_channel.columns:
            continue
        series = pd.to_numeric(by_channel[channel], errors="coerce")
        points = _hourly_points(
            series,
            timezone_name,
            window_start,
            window_end,
        )
        if points:
            streams[channel] = HourlyStream(points=points, channels=(channel,))
    return streams


def stream_covers_window(
    stream: HourlyStream,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """Return whether a stream reaches both ends of a requested window.

    SAPN can briefly publish only part of the newest day for some NMIs. The
    coordinator must not advance that NMI's checkpoint until the first and
    final UTC-aligned hours are present. We intentionally do not require a
    fixed number of hours because Adelaide daylight-saving transitions can
    produce unusual NEM12 interval sequences.
    """
    if not stream.points or window_end <= window_start:
        return False

    starts = {point.start for point in stream.points}
    return window_start in starts and window_end - timedelta(hours=1) in starts
