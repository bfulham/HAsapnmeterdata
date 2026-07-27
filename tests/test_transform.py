"""Tests for SAPN interval transformation and publication scheduling."""

import importlib.util
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "sapnmeterdata"
PACKAGE_NAME = "sapn_component_test"
package = ModuleType(PACKAGE_NAME)
package.__path__ = [str(COMPONENT_DIR)]
sys.modules[PACKAGE_NAME] = package


def _load_component_module(name: str) -> ModuleType:
    """Load a component module without importing Home Assistant."""
    module_name = f"{PACKAGE_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        COMPONENT_DIR / f"{name}.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


statistics = _load_component_module("statistics")
schedule = _load_component_module("schedule")
channels = _load_component_module("channels")
transform = _load_component_module("transform")

HourlyPoint = statistics.HourlyPoint
build_statistics = statistics.build_statistics
default_channel_type = channels.default_channel_type
derive_base_sum = statistics.derive_base_sum
derive_prepend_base_sum = statistics.derive_prepend_base_sum
enabled_channels = channels.enabled_channels
extract_hourly_channels = transform.extract_hourly_channels
historical_chunk = schedule.historical_chunk
latest_available_date = schedule.latest_available_date
merge_channel_config = channels.merge_channel_config
next_daily_refresh = schedule.next_daily_refresh
normalize_channel_config = channels.normalize_channel_config
parse_patterns = channels.parse_patterns
statistic_id = statistics.statistic_id
statistic_name = statistics.statistic_name
utc_statistic_window = schedule.utc_statistic_window

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


def test_interval_channels_remain_separate_and_aggregate_hourly() -> None:
    """Every enabled channel gets its own UTC-aligned hourly stream."""
    target_start = date(2026, 7, 12)
    target_end = date(2026, 7, 13)
    index = pd.date_range(
        "2026-07-11",
        target_end,
        freq="5min",
        inclusive="left",
    )
    window_start, window_end = utc_statistic_window(
        target_start,
        target_end,
        "Australia/Adelaide",
    )
    streams = extract_hourly_channels(
        _frame(index),
        "20023157519",
        ("E1", "E2", "B1"),
        "Australia/Adelaide",
        window_start,
        window_end,
    )

    assert set(streams) == {"E1", "E2", "B1"}
    assert streams["E1"].channels == ("E1",)
    assert streams["E2"].channels == ("E2",)
    assert streams["B1"].channels == ("B1",)
    assert len(streams["E1"].points) == 24
    assert streams["E1"].points[0].value == pytest.approx(1.2)
    assert streams["E2"].points[0].value == pytest.approx(0.6)
    assert streams["B1"].points[0].value == pytest.approx(0.24)
    assert all(point.start.tzinfo is UTC for point in streams["E1"].points)
    assert all(point.start.minute == 0 for point in streams["E1"].points)


def test_channel_defaults_keep_reactive_registers_ignored() -> None:
    """E/B channels get safe energy defaults and K/Q remain ignored."""
    config = merge_channel_config(
        ["20023157519"],
        {"20023157519": ("E1", "E2", "B1", "K1", "Q1")},
    )
    meter_config = config["20023157519"]

    assert default_channel_type("E1") == "consumption"
    assert default_channel_type("B1") == "return"
    assert default_channel_type("K1") == "ignore"
    assert enabled_channels(meter_config) == ("E1", "E2", "B1")


def test_same_channel_code_can_have_a_different_name_per_meter() -> None:
    """Channel definitions are nested under their stable NMI identity."""
    config = normalize_channel_config(
        {
            "NMI1": {
                "E1": {"name": "Standard Consumption", "type": "consumption"},
                "E2": {"name": "Controlled Load", "type": "consumption"},
                "B1": {"name": "Solar", "type": "return"},
            },
            "NMI2": {
                "E1": {"name": "Pump Station", "type": "consumption"},
            },
        }
    )

    assert config["NMI1"]["E1"]["name"] == "Standard Consumption"
    assert config["NMI1"]["E2"]["name"] == "Controlled Load"
    assert config["NMI1"]["B1"]["name"] == "Solar"
    assert config["NMI2"]["E1"]["name"] == "Pump Station"


def test_adelaide_half_hour_is_aligned_to_utc_statistics() -> None:
    """SAPN and Recorder data use the same UTC hourly positions."""
    start, end = utc_statistic_window(
        date(2026, 7, 12),
        date(2026, 7, 13),
        "Australia/Adelaide",
    )

    assert start == datetime(2026, 7, 11, 14, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
    assert start.astimezone(ADELAIDE).minute == 30


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
    source_start = start - pd.DateOffset(days=1)
    index = pd.date_range(
        source_start,
        end,
        freq="5min",
        inclusive="left",
    ).tz_localize(None)
    window_start, window_end = utc_statistic_window(
        start.date(),
        end.date(),
        "Australia/Adelaide",
    )
    streams = extract_hourly_channels(
        _frame(index),
        "20023157519",
        ("E1",),
        "Australia/Adelaide",
        window_start,
        window_end,
    )
    assert len(streams["E1"].points) == expected_hours


def test_unique_nem12_intervals_survive_adelaide_dst_fallback() -> None:
    """A fixed 288-interval NEM12 day must not raise AmbiguousTimeError."""
    target_start = date(2026, 4, 5)
    target_end = date(2026, 4, 6)
    index = pd.date_range(
        "2026-04-04",
        target_end,
        freq="5min",
        inclusive="left",
    )
    window_start, window_end = utc_statistic_window(
        target_start,
        target_end,
        "Australia/Adelaide",
    )

    streams = extract_hourly_channels(
        _frame(index),
        "20023157519",
        ("E1",),
        "Australia/Adelaide",
        window_start,
        window_end,
    )

    assert len(streams["E1"].points) == 25
    assert sum(point.value for point in streams["E1"].points) == pytest.approx(28.8)


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
    assert rows[0]["state"] == 0.0
    assert rows[0]["sum"] == 0.0
    assert [row["state"] for row in rows[1:]] == [1.5, 2.0]
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


def test_historical_chunk_joins_to_newer_cumulative_sum() -> None:
    """Prepending older data does not create a discontinuity."""
    points = (
        HourlyPoint(datetime(2026, 7, 10, 12, tzinfo=UTC), 1.5),
        HourlyPoint(datetime(2026, 7, 10, 13, tzinfo=UTC), 2.0),
    )
    next_row = {
        "start": datetime(2026, 7, 10, 14, tzinfo=UTC).timestamp(),
        "state": 3.0,
        "sum": 103.0,
    }

    base = derive_prepend_base_sum(points, [], [next_row])
    rows = build_statistics(points, base, False)

    assert base == pytest.approx(96.5)
    assert rows[-1]["sum"] == pytest.approx(100.0)
    assert next_row["sum"] - rows[-1]["sum"] == pytest.approx(3.0)


def test_retried_historical_chunk_is_idempotent() -> None:
    """An existing row recovers the same negative historical base."""
    points = (
        HourlyPoint(datetime(2026, 7, 10, 12, tzinfo=UTC), 1.5),
        HourlyPoint(datetime(2026, 7, 10, 13, tzinfo=UTC), 2.0),
    )
    existing = [{"start": points[0].start.timestamp(), "sum": 98.0}]

    base = derive_prepend_base_sum(points, existing, [])

    assert base == pytest.approx(96.5)


def test_historical_chunk_moves_back_seven_days() -> None:
    """Backfill requests remain bounded instead of requesting years at once."""
    assert historical_chunk(date(2026, 7, 25), 7) == (
        date(2026, 7, 18),
        date(2026, 7, 25),
    )


def test_statistic_id_preserves_the_existing_integration_domain() -> None:
    """Each NMI/channel pair gets a stable independent statistic ID."""
    assert statistic_id("20023157519", "E1") == "sapnmeterdata:20023157519_e1"
    assert statistic_id("20023157519", "E2") == "sapnmeterdata:20023157519_e2"
    assert statistic_id("20023157519", "B1") == "sapnmeterdata:20023157519_b1"


def test_statistic_name_uses_the_sapn_friendly_meter_name() -> None:
    """Meter and user-supplied channel names are both user-facing."""
    assert (
        statistic_name(
            "20023157519",
            "E1",
            "MRC",
            "Standard Consumption",
        )
        == "SAPN MRC Standard Consumption"
    )
    assert statistic_name("20023157519", "B1", "MRC", "Solar") == "SAPN MRC Solar"
