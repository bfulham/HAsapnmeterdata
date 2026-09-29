"""Convert NEM12 days into hourly energy and cumulative statistics rows.

NEM12 uses NEM time (UTC+10, no daylight saving), so each NEM12 day is
exactly 24 UTC hours from 14:00 UTC on the previous date. Every NEM12
interval therefore falls inside a single Home Assistant (UTC) hour.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

from .portal.nem12 import KWH_MULTIPLIERS, DayReadings, nmi_matches, parse_nem12

NEM_TIME = timezone(timedelta(hours=10), "AEST")
HOUR = timedelta(hours=1)
PRECISION = 6


@dataclass(frozen=True, slots=True)
class DaySummary:
    """Hourly kWh for one channel on one NEM12 day."""

    day: date
    hours: tuple[float | None, ...]
    final: bool

    def points(self) -> dict[datetime, float]:
        """Return complete hours keyed by their UTC start."""
        start = day_start_utc(self.day)
        return {
            start + hour * HOUR: value
            for hour, value in enumerate(self.hours)
            if value is not None
        }


def day_start_utc(day: date) -> datetime:
    """Return the UTC instant at which a NEM12 day starts."""
    return datetime.combine(day, time(), NEM_TIME).astimezone(UTC)


def summarize_day(readings: DayReadings) -> DaySummary | None:
    """Aggregate one NEM12 day into 24 hourly kWh values.

    Returns None for non-energy units (such as kVArh). An hour containing a
    missing interval value is None and the day is not final.
    """
    multiplier = KWH_MULTIPLIERS.get(readings.uom)
    if multiplier is None:
        return None
    per_hour = 60 // readings.interval_minutes
    hours: list[float | None] = []
    for hour in range(24):
        chunk = readings.values[hour * per_hour : (hour + 1) * per_hour]
        if len(chunk) != per_hour or any(value is None for value in chunk):
            hours.append(None)
        else:
            hours.append(round(sum(chunk) * multiplier, PRECISION))  # type: ignore[arg-type]
    return DaySummary(day=readings.day, hours=tuple(hours), final=readings.is_final)


def summarize_nem12(
    text: str,
    nmi: str,
    first_day: date,
    last_day: date,
) -> dict[str, dict[date, DaySummary]]:
    """Parse NEM12 text into ``{channel: {day: summary}}`` for one meter.

    Days outside ``first_day``..``last_day`` and non-energy channels are
    dropped.
    """
    channels: dict[str, dict[date, DaySummary]] = {}
    for readings in parse_nem12(text):
        if not nmi_matches(nmi, readings.nmi):
            continue
        if not first_day <= readings.day <= last_day:
            continue
        summary = summarize_day(readings)
        if summary is not None:
            channels.setdefault(readings.suffix, {})[readings.day] = summary
    return channels


def merge_points(summaries: Iterable[DaySummary]) -> dict[datetime, float]:
    """Return every complete hour from several days."""
    points: dict[datetime, float] = {}
    for summary in summaries:
        points.update(summary.points())
    return points


def build_rows(
    base_sum: float,
    states: Mapping[datetime, float],
) -> list[tuple[datetime, float, float]]:
    """Return ``(start, state, sum)`` rows continuing from ``base_sum``."""
    running = base_sum
    rows: list[tuple[datetime, float, float]] = []
    for start in sorted(states):
        running = round(running + states[start], PRECISION)
        rows.append((start, states[start], running))
    return rows
