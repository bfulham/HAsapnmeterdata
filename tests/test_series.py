"""Tests for converting NEM12 days into hourly statistics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.sapnmeterdata.portal.nem12 import parse_nem12
from custom_components.sapnmeterdata.series import (
    build_rows,
    day_start_utc,
    summarize_day,
    summarize_nem12,
)

from .common import NMI, NMI_2, nem12_200, nem12_300


def _summary(day: date, values: list[float | None], uom: str = "KWH", **kwargs):
    interval = 1440 // len(values)
    text = "\n".join(
        [
            nem12_200(NMI, "E1", uom=uom, interval=interval),
            nem12_300(day, values, **kwargs),
        ]
    )
    (readings,) = parse_nem12(text)
    return summarize_day(readings)


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 7, 1),  # Adelaide standard time
        date(2026, 1, 15),  # Adelaide daylight saving time
        date(2026, 10, 4),  # Adelaide clocks go forward
        date(2026, 4, 5),  # Adelaide clocks go back
    ],
)
def test_nem12_day_is_24_utc_hours_from_1400(day: date) -> None:
    """NEM12 is UTC+10 all year, so every day is exactly 24 UTC hours."""
    values = [float(i) for i in range(288)]
    summary = _summary(day, values)
    points = summary.points()
    start = datetime.combine(day - timedelta(days=1), datetime.min.time(), UTC)
    start += timedelta(hours=14)
    assert day_start_utc(day) == start
    assert list(points) == [start + timedelta(hours=h) for h in range(24)]
    # The first hour holds intervals 0-11, the last holds 276-287.
    assert points[start] == pytest.approx(sum(range(12)))
    assert points[start + timedelta(hours=23)] == pytest.approx(sum(range(276, 288)))
    assert sum(points.values()) == pytest.approx(sum(values))


def test_units_are_converted_to_kwh() -> None:
    """Wh readings are converted to kWh."""
    summary = _summary(date(2026, 7, 1), [500.0] * 48, uom="WH")
    assert set(summary.points().values()) == {1.0}


def test_non_energy_units_are_skipped() -> None:
    """Reactive energy is not summarised."""
    assert _summary(date(2026, 7, 1), [1.0] * 48, uom="KVARH") is None


def test_missing_interval_drops_only_its_hour() -> None:
    """An hour with a missing interval is omitted and the day is not final."""
    values: list[float | None] = [0.01] * 288
    values[30] = None  # 02:30 NEM time, in the third hour
    summary = _summary(date(2026, 7, 1), values)
    assert summary.hours[2] is None
    assert len(summary.points()) == 23
    assert not summary.final


def test_estimated_day_is_not_final() -> None:
    """Estimated readings are imported but flagged for re-checking."""
    summary = _summary(date(2026, 7, 1), [0.01] * 288, quality="E52")
    assert len(summary.points()) == 24
    assert not summary.final


def test_summarize_nem12_filters_meter_and_dates() -> None:
    """Only the requested meter, range, and energy channels are returned."""
    text = "\n".join(
        [
            nem12_200(NMI, "E1"),
            nem12_300(date(2026, 7, 1), [0.01] * 288),
            nem12_300(date(2026, 7, 2), [0.01] * 288),
            nem12_200(NMI, "Q1", uom="KVARH"),
            nem12_300(date(2026, 7, 1), [0.01] * 288),
            nem12_200(NMI_2, "E1"),
            nem12_300(date(2026, 7, 1), [0.01] * 288),
        ]
    )
    result = summarize_nem12(text, NMI, date(2026, 7, 1), date(2026, 7, 1))
    assert list(result) == ["E1"]
    assert list(result["E1"]) == [date(2026, 7, 1)]


def test_build_rows_continues_the_sum() -> None:
    """Rows are sorted and the sum continues from the base."""
    hour = datetime(2026, 7, 1, tzinfo=UTC)
    states = {hour + timedelta(hours=1): 2.0, hour: 1.5}
    assert build_rows(10.0, states) == [
        (hour, 1.5, 11.5),
        (hour + timedelta(hours=1), 2.0, 13.5),
    ]
