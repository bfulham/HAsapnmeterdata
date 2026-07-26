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
