"""Tests for SAPN interval transformation and publication scheduling."""

import importlib.util
import sys
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "sapnmeterdata" / "transform.py"
)
SCHEDULE_MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "sapnmeterdata" / "schedule.py"
)
SPEC = importlib.util.spec_from_file_location("sapn_transform", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
transform = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transform
SPEC.loader.exec_module(transform)

SCHEDULE_SPEC = importlib.util.spec_from_file_location(
    "sapn_schedule",
    SCHEDULE_MODULE_PATH,
)
assert SCHEDULE_SPEC is not None
assert SCHEDULE_SPEC.loader is not None
schedule = importlib.util.module_from_spec(SCHEDULE_SPEC)
SCHEDULE_SPEC.loader.exec_module(schedule)

HourlyPoint = transform.HourlyPoint
build_statistics = transform.build_statistics
derive_base_sum = transform.derive_base_sum
extract_hourly_streams = transform.extract_hourly_streams
latest_available_date = schedule.latest_available_date
next_daily_refresh = schedule.next_daily_refresh
parse_patterns = transform.parse_patterns
statistic_id = transform.statistic_id

ADELAIDE = ZoneInfo("Australia/Adelaide")


def _frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Build representative five-minute NEM12 data."""
    nmi = "20023157519"
    return pd.DataFrame(
        {
            (nmi, "E1"): 0.10,
            (nmi, "E2"): 0.05,
            (nmi, "B1"): 0.02,
            (nmi, "K1"): 10.0,
        },
        index=index,
    )


def test_before_3am_yesterday_is_not_available() -> None:
    """The 26th must not be requested before 03:00 on the 27th."""
    now = datetime(2026, 7, 27, 2, 59, tzinfo=ADELAIDE)
    assert (
        latest_available_date(now, "Australia/Adelaide", time(3))
        == datetime(
            2026,
            7,
            25,
        ).date()
    )


def test_at_3am_yesterday_becomes_available() -> None:
    """The 26th becomes eligible at 03:00 on the 27th."""
    now = datetime(2026, 7, 27, 3, 0, tzinfo=ADELAIDE)
    assert (
        latest_available_date(now, "Australia/Adelaide", time(3))
        == datetime(
            2026,
            7,
            26,
        ).date()
    )


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 7, 27, 2, 0, tzinfo=ADELAIDE),
        datetime(2026, 1, 27, 2, 0, tzinfo=ADELAIDE),
    ],
)
def test_daily_refresh_is_0305_adelaide_in_standard_and_daylight_time(
    now: datetime,
) -> None:
    """The dedicated refresh remains at Adelaide 03:05 across DST."""
    refresh = next_daily_refresh(now, "Australia/Adelaide", time(3, 5))
    local_refresh = refresh.astimezone(ADELAIDE)
    assert local_refresh.date() == now.date()
    assert local_refresh.time().replace(tzinfo=None) == time(3, 5)
    assert refresh.tzinfo is UTC


def test_refresh_after_0305_is_scheduled_for_tomorrow() -> None:
    """Only one dedicated refresh is scheduled per local day."""
    now = datetime(2026, 7, 27, 3, 6, tzinfo=ADELAIDE)
    refresh = next_daily_refresh(now, "Australia/Adelaide", time(3, 5))
    assert refresh.astimezone(ADELAIDE) == datetime(
        2026,
        7,
        28,
        3,
        5,
        tzinfo=ADELAIDE,
    )


def test_patterns_accept_commas_semicolons_and_spaces() -> None:
    """Channel patterns are easy to configure in the UI."""
    assert parse_patterns("E1, e2; E*") == ("E1", "E2", "E*")


def test_interval_channels_are_combined_and_aggregated_hourly() -> None:
    """E channels combine as consumption while B channels remain return."""
    index = pd.date_range(
        "2026-07-12",
        "2026-07-13",
        freq="5min",
        inclusive="left",
    )
    streams = extract_hourly_streams(
        _frame(index),
        "20023157519",
        "E*",
        "B*",
        "Australia/Adelaide",
    )

    assert streams["consumption"].channels == ("E1", "E2")
    assert streams["return"].channels == ("B1",)
    assert len(streams["consumption"].points) == 24
    assert streams["consumption"].points[0].value == pytest.approx(1.8)
    assert streams["return"].points[0].value == pytest.approx(0.24)


@pytest.mark.parametrize(
    ("local_day", "expected_hours"),
    [
        ("2025-10-05", 23),
        ("2025-04-06", 25),
    ],
)
def test_daylight_saving_days_have_the_correct_hour_count(
    local_day: str,
    expected_hours: int,
) -> None:
    """Adelaide DST transitions preserve each real elapsed hour."""
    start = pd.Timestamp(local_day, tz="Australia/Adelaide")
    end = start + pd.DateOffset(days=1)
    index = pd.date_range(start, end, freq="5min", inclusive="left").tz_localize(None)
    streams = extract_hourly_streams(
        _frame(index),
        "20023157519",
        "E*",
        "B*",
        "Australia/Adelaide",
    )
    assert len(streams["consumption"].points) == expected_hours


def test_new_stream_gets_a_baseline_and_continuous_sum() -> None:
    """The first imported hour has a previous point for Energy Dashboard delta."""
    points = (
        HourlyPoint(datetime(2026, 7, 11, 14, tzinfo=UTC), 1.5),
        HourlyPoint(datetime(2026, 7, 11, 15, tzinfo=UTC), 2.0),
    )
    base, include_baseline = derive_base_sum(points, [], [])
    rows = build_statistics(points, base, include_baseline)

    assert include_baseline is True
    assert rows[0]["start"] == datetime(2026, 7, 11, 13, tzinfo=UTC)
    assert rows[0]["sum"] == 0.0
    assert [row["sum"] for row in rows[1:]] == [1.5, 3.5]


def test_existing_target_row_makes_reimport_idempotent() -> None:
    """An existing cumulative row is used to recover the original base."""
    points = (
        HourlyPoint(datetime(2026, 7, 11, 14, tzinfo=UTC), 1.5),
        HourlyPoint(datetime(2026, 7, 11, 15, tzinfo=UTC), 2.0),
    )
    existing = [{"start": points[0].start.timestamp(), "sum": 101.5}]
    base, include_baseline = derive_base_sum(
        points,
        existing,
        [{"start": points[-1].start.timestamp(), "sum": 103.5}],
    )
    rows = build_statistics(points, base, include_baseline)

    assert base == 100.0
    assert include_baseline is False
    assert [row["sum"] for row in rows] == [101.5, 103.5]


def test_latest_sum_continues_the_next_day() -> None:
    """A new day starts from the previous imported cumulative value."""
    points = (HourlyPoint(datetime(2026, 7, 12, 14, tzinfo=UTC), 2.5),)
    base, include_baseline = derive_base_sum(
        points,
        [],
        [{"start": datetime(2026, 7, 11, 13, tzinfo=UTC), "sum": 40.0}],
    )
    rows = build_statistics(points, base, include_baseline)

    assert include_baseline is False
    assert rows[0]["sum"] == 42.5


def test_statistic_id_preserves_the_existing_integration_domain() -> None:
    """0.2.0 keeps the domain used by the 0.1.x repository."""
    assert (
        statistic_id("20023157519", "consumption")
        == "sapnmeterdata:20023157519_consumption"
    )
