"""When SAPN data becomes available and when to check for it."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ADELAIDE = ZoneInfo("Australia/Adelaide")

# SAPN publishes the previous day at 03:00 Adelaide time.
PUBLISH_TIME = time(3, 0)
DAILY_SYNC_TIME = time(3, 20)
# While a published day is still missing, check hourly in the morning and
# every three hours after that.
WAITING_RETRY_EARLY = timedelta(hours=1)
WAITING_RETRY_LATE = timedelta(hours=3)
WAITING_EARLY_UNTIL = time(12, 0)
ERROR_BACKOFF = (
    timedelta(minutes=15),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=3),
)


def latest_published_day(now: datetime) -> date:
    """Return the newest day SAPN should have published by ``now``."""
    local = now.astimezone(ADELAIDE)
    return local.date() - timedelta(days=1 if local.time() >= PUBLISH_TIME else 2)


def next_daily_sync(now: datetime) -> datetime:
    """Return the next daily sync time in UTC."""
    local = now.astimezone(ADELAIDE)
    candidate = datetime.combine(local.date(), DAILY_SYNC_TIME, ADELAIDE)
    if candidate <= local:
        candidate = datetime.combine(
            local.date() + timedelta(days=1), DAILY_SYNC_TIME, ADELAIDE
        )
    return candidate.astimezone(UTC)


def next_sync_after_success(now: datetime, waiting: bool) -> datetime:
    """Return when to sync next after a run that completed.

    ``waiting`` means SAPN has not yet published a day it should have.
    """
    daily = next_daily_sync(now)
    if not waiting:
        return daily
    local = now.astimezone(ADELAIDE)
    retry = (
        WAITING_RETRY_EARLY
        if local.time() < WAITING_EARLY_UNTIL
        else WAITING_RETRY_LATE
    )
    return min(now + retry, daily)


def next_sync_after_error(now: datetime, consecutive_failures: int) -> datetime:
    """Return when to retry after ``consecutive_failures`` failed runs."""
    index = min(max(consecutive_failures, 1), len(ERROR_BACKOFF)) - 1
    return now + ERROR_BACKOFF[index]
