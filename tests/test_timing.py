"""Tests for SAPN publication timing."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.sapnmeterdata.timing import (
    ADELAIDE,
    latest_published_day,
    next_daily_sync,
    next_sync_after_error,
    next_sync_after_success,
)


def adelaide(*args: int) -> datetime:
    """Return an Adelaide local time."""
    return datetime(*args, tzinfo=ADELAIDE)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (adelaide(2026, 9, 29, 2, 59), date(2026, 9, 27)),
        (adelaide(2026, 9, 29, 3, 0), date(2026, 9, 28)),
        (adelaide(2026, 9, 29, 23, 59), date(2026, 9, 28)),
    ],
)
def test_latest_published_day(now: datetime, expected: date) -> None:
    """Yesterday is available from 03:00 Adelaide time."""
    assert latest_published_day(now.astimezone(UTC)) == expected


def test_next_daily_sync_across_daylight_saving_start() -> None:
    """The daily sync stays at 03:20 local time when the clocks change."""
    before = adelaide(2026, 10, 3, 12, 0).astimezone(UTC)
    first = next_daily_sync(before)
    second = next_daily_sync(first)
    assert first.astimezone(ADELAIDE).replace(tzinfo=None) == datetime(
        2026, 10, 4, 3, 20
    )
    assert second.astimezone(ADELAIDE).replace(tzinfo=None) == datetime(
        2026, 10, 5, 3, 20
    )
    assert second - first == timedelta(hours=24)


def test_waiting_retries_hourly_in_the_morning_then_every_three_hours() -> None:
    """Missing data is re-checked soon, but never past the next daily sync."""
    morning = adelaide(2026, 9, 29, 4, 0).astimezone(UTC)
    afternoon = adelaide(2026, 9, 29, 14, 0).astimezone(UTC)
    just_before_daily = adelaide(2026, 9, 30, 3, 10).astimezone(UTC)
    assert next_sync_after_success(morning, waiting=True) - morning == timedelta(
        hours=1
    )
    assert next_sync_after_success(afternoon, waiting=True) - afternoon == timedelta(
        hours=3
    )
    assert next_sync_after_success(just_before_daily, waiting=True) == next_daily_sync(
        just_before_daily
    )
    assert next_sync_after_success(morning, waiting=False) == next_daily_sync(morning)


def test_error_backoff() -> None:
    """Failures back off from 15 minutes to at most 3 hours."""
    now = datetime(2026, 9, 29, tzinfo=UTC)
    delays = [next_sync_after_error(now, count) - now for count in range(1, 8)]
    assert delays[0] == timedelta(minutes=15)
    assert delays[1] == timedelta(minutes=30)
    assert delays[-1] == timedelta(hours=3)
    assert delays == sorted(delays)
