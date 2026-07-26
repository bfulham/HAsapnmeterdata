"""Publication scheduling helpers for SA Power Networks meter data."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def latest_available_date(
    now: datetime,
    timezone_name: str,
    available_time: time,
) -> date:
    """Return the newest SAPN calendar day expected to be available."""
    local_now = now.astimezone(ZoneInfo(timezone_name))
    days_back = 1 if local_now.time() >= available_time else 2
    return local_now.date() - timedelta(days=days_back)


def next_daily_refresh(
    now: datetime,
    timezone_name: str,
    refresh_time: time,
) -> datetime:
    """Return the next daily refresh instant as a UTC datetime."""
    timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone)
    candidate = datetime.combine(local_now.date(), refresh_time, timezone)
    if candidate <= local_now:
        candidate = datetime.combine(
            local_now.date() + timedelta(days=1),
            refresh_time,
            timezone,
        )
    return candidate.astimezone(UTC)


def utc_statistic_window(
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    """Return UTC-hour boundaries covering a local calendar-date range."""
    if end_date <= start_date:
        raise ValueError("end_date must be after start_date")

    timezone = ZoneInfo(timezone_name)
    local_start = datetime.combine(start_date, time.min, timezone)
    local_end = datetime.combine(end_date, time.min, timezone)
    start_utc = local_start.astimezone(UTC).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    end_utc = local_end.astimezone(UTC).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    return start_utc, end_utc


def historical_chunk(before: date, days: int) -> tuple[date, date]:
    """Return one backwards date chunk ending before ``before``."""
    if days < 1:
        raise ValueError("days must be at least one")
    return before - timedelta(days=days), before
