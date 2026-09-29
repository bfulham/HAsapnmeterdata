"""Keep SA Power Networks meter data in Home Assistant statistics.

Each sync signs in once and, for every selected meter:

- imports the complete history of any enabled channel this config entry has
  not imported before (replacing whatever that statistic held); otherwise
- requests every day from the oldest day still needed to the newest day SAPN
  should have published, and merges it into the statistics.

A day that is missing, or whose readings are not yet final, is recorded as
pending and re-requested on later syncs for up to ``PENDING_RETENTION_DAYS``.
Missing days never stop newer days from being imported.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .channels import enabled_channels
from .const import (
    CONF_CHANNELS,
    CONF_METER_NAMES,
    CONF_NMIS,
    CONNECTION_RETRY_SECONDS,
    DISCOVERY_DAYS,
    DOMAIN,
    HISTORY_CHUNK_DAYS,
    HISTORY_EMPTY_DAYS,
    HISTORY_MAX_DAYS,
    LOGGER,
    LOGIN_FAILURES_BEFORE_REAUTH,
    PENDING_RETENTION_DAYS,
    REQUEST_DELAY_SECONDS,
    STATUS_ERROR,
    STATUS_SYNCING,
    STATUS_UP_TO_DATE,
    STATUS_WAITING,
    STORE_VERSION,
)
from .portal import (
    NEM12Error,
    SAPNAuthError,
    SAPNClient,
    SAPNConnectionError,
    SAPNError,
    SAPNLoginFailedError,
    SAPNNoDataError,
    SAPNPortalError,
)
from .series import DaySummary, day_start_utc, merge_points, summarize_nem12
from .statistics import (
    async_merge_statistics,
    async_replace_statistics,
    statistic_id,
    statistic_metadata,
)
from .timing import (
    latest_published_day,
    next_sync_after_error,
    next_sync_after_success,
)

type SAPNConfigEntry = ConfigEntry[SAPNCoordinator]
type ChannelDays = dict[str, dict[date, DaySummary]]

ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class MeterStatus:
    """What is known about one meter after the latest sync."""

    name: str
    latest_day: date | None = None
    pending_days: int = 0
    waiting: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SyncStatus:
    """The coordinator's data: the outcome of the latest sync."""

    state: str
    meters: dict[str, MeterStatus]
    last_success: datetime | None = None
    next_sync: datetime | None = None
    error: str | None = None


@dataclass(slots=True)
class Stream:
    """Import progress for one meter channel (one external statistic)."""

    nmi: str
    channel: str
    first: date | None = None
    last: date | None = None
    pending: set[date] = field(default_factory=set)

    def next_day(self, latest: date) -> date:
        """Return the oldest day this channel still needs."""
        if self.last is None:
            needed = latest - timedelta(days=DISCOVERY_DAYS - 1)
        else:
            needed = self.last + ONE_DAY
        return min([needed, *self.pending])

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "nmi": self.nmi,
            "channel": self.channel,
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
            "pending": sorted(day.isoformat() for day in self.pending),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Stream:
        """Restore a stream saved by ``as_dict``."""
        return cls(
            nmi=raw["nmi"],
            channel=raw["channel"],
            first=date.fromisoformat(raw["first"]) if raw.get("first") else None,
            last=date.fromisoformat(raw["last"]) if raw.get("last") else None,
            pending={date.fromisoformat(day) for day in raw.get("pending", [])},
        )


@contextmanager
def portal_session(hass: HomeAssistant) -> Iterator[aiohttp.ClientSession]:
    """Yield a client session with its own cookie jar for one portal login."""
    session = async_create_clientsession(hass, auto_cleanup=False)
    try:
        yield session
    finally:
        # Sessions from Home Assistant share its connector, so they are
        # detached rather than closed.
        session.detach()


def _merge_days(target: ChannelDays, source: ChannelDays) -> ChannelDays:
    for channel, days in source.items():
        target.setdefault(channel, {}).update(days)
    return target


def _newest_day(data: ChannelDays) -> date | None:
    return max((day for days in data.values() for day in days), default=None)


class SAPNCoordinator(DataUpdateCoordinator[SyncStatus]):
    """Sync SAPN meter data into long-term statistics on a schedule."""

    config_entry: SAPNConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SAPNConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            # Set after every sync from SAPN's publication schedule.
            update_interval=None,
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._streams: dict[str, Stream] = {}
        self._lock = asyncio.Lock()
        self._failures = 0
        self._login_failures = 0
        self._last_success: datetime | None = None
        self._meter_errors: dict[str, str] = {}
        self._waiting: set[str] = set()
        self._next_request = 0.0

    @property
    def nmis(self) -> list[str]:
        """Return the selected meters."""
        return list(self.config_entry.options.get(CONF_NMIS, []))

    def meter_name(self, nmi: str) -> str:
        """Return a meter's display name."""
        return self.config_entry.options.get(CONF_METER_NAMES, {}).get(nmi) or nmi

    async def async_load(self) -> None:
        """Restore saved progress and publish an initial status."""
        stored = await self._store.async_load() or {}
        self._streams = {
            stat_id: Stream.from_dict(raw)
            for stat_id, raw in stored.get("streams", {}).items()
        }
        if last_success := stored.get("last_success"):
            self._last_success = dt_util.parse_datetime(last_success)
        self.data = self._status(STATUS_SYNCING)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "streams": {
                    stat_id: stream.as_dict()
                    for stat_id, stream in self._streams.items()
                },
                "last_success": (
                    self._last_success.isoformat() if self._last_success else None
                ),
            }
        )

    @callback
    def async_start(self, _hass: HomeAssistant | None = None) -> None:
        """Run the first sync in the background once Home Assistant is up."""
        self.config_entry.async_create_background_task(
            self.hass, self.async_refresh(), f"{DOMAIN} sync"
        )

    def stream_diagnostics(self) -> dict[str, dict[str, Any]]:
        """Return import progress for diagnostics."""
        return {stat_id: stream.as_dict() for stat_id, stream in self._streams.items()}

    def _known_channels(self, nmi: str) -> set[str]:
        return {
            stream.channel for stream in self._streams.values() if stream.nmi == nmi
        }

    def _status(
        self,
        state: str,
        next_sync: datetime | None = None,
        error: str | None = None,
    ) -> SyncStatus:
        meters: dict[str, MeterStatus] = {}
        for nmi in self.nmis:
            channels = enabled_channels(
                self.config_entry.options, nmi, self._known_channels(nmi)
            )
            streams = [
                self._streams[stat_id]
                for channel in channels
                if (stat_id := statistic_id(nmi, channel)) in self._streams
            ]
            lasts = [stream.last for stream in streams if stream.last]
            pending: set[date] = set().union(*(stream.pending for stream in streams))
            meters[nmi] = MeterStatus(
                name=self.meter_name(nmi),
                latest_day=min(lasts) if lasts else None,
                pending_days=len(pending),
                waiting=nmi in self._waiting,
                error=self._meter_errors.get(nmi),
            )
        return SyncStatus(
            state=state,
            meters=meters,
            last_success=self._last_success,
            next_sync=next_sync,
            error=error,
        )

    def _schedule(self, next_sync: datetime) -> None:
        self.update_interval = max(next_sync - dt_util.utcnow(), timedelta(minutes=1))

    async def _async_update_data(self) -> SyncStatus:
        """Run one sync and decide when the next one should happen."""
        async with self._lock:
            if self.data is not None:
                self.data = replace(self.data, state=STATUS_SYNCING)
                self.async_update_listeners()
            try:
                waiting = await self._async_sync()
            except SAPNAuthError as err:
                raise ConfigEntryAuthFailed(f"SAPN rejected the login: {err}") from err
            except SAPNLoginFailedError as err:
                self._login_failures += 1
                if self._login_failures >= LOGIN_FAILURES_BEFORE_REAUTH:
                    raise ConfigEntryAuthFailed(
                        f"Signing in to SAPN failed {self._login_failures} times "
                        f"in a row: {err}"
                    ) from err
                return self._failed(err)
            except SAPNError as err:
                return self._failed(err)
            except Exception as err:
                LOGGER.exception("Unexpected error while syncing SAPN meter data")
                return self._failed(err)

            self._login_failures = 0
            if self._meter_errors:
                return self._failed(
                    "; ".join(
                        f"{self.meter_name(nmi)}: {message}"
                        for nmi, message in self._meter_errors.items()
                    )
                )
            if self._failures:
                LOGGER.info("SAPN meter data sync is working again")
            self._failures = 0
            self._last_success = dt_util.utcnow()
            await self._async_save()
            next_sync = next_sync_after_success(dt_util.utcnow(), waiting)
            self._schedule(next_sync)
            return self._status(
                STATUS_WAITING if waiting else STATUS_UP_TO_DATE, next_sync
            )

    def _failed(self, err: Exception | str) -> SyncStatus:
        self._failures += 1
        next_sync = next_sync_after_error(dt_util.utcnow(), self._failures)
        log = LOGGER.warning if self._failures == 1 else LOGGER.debug
        log("SAPN meter data sync failed, retrying at %s: %s", next_sync, err)
        self._schedule(next_sync)
        return self._status(STATUS_ERROR, next_sync, str(err))

    async def _async_sync(self) -> bool:
        """Sync every selected meter; return whether SAPN is behind schedule."""
        latest = latest_published_day(dt_util.utcnow())
        with portal_session(self.hass) as session:
            client = SAPNClient(
                session,
                self.config_entry.data[CONF_EMAIL],
                self.config_entry.data[CONF_PASSWORD],
            )
            await client.login()
            self._waiting.clear()
            self._meter_errors = {
                nmi: message
                for nmi, message in self._meter_errors.items()
                if nmi in self.nmis
            }
            for nmi in self.nmis:
                try:
                    if await self._async_sync_meter(client, nmi, latest):
                        self._waiting.add(nmi)
                except (SAPNAuthError, SAPNLoginFailedError, SAPNConnectionError):
                    raise
                except SAPNError as err:
                    LOGGER.debug("Sync of SAPN meter %s failed: %s", nmi, err)
                    self._meter_errors[nmi] = str(err)
                else:
                    self._meter_errors.pop(nmi, None)
                finally:
                    await self._async_save()
        return bool(self._waiting)

    async def _async_sync_meter(
        self,
        client: SAPNClient,
        nmi: str,
        latest: date,
    ) -> bool:
        """Sync one meter; return whether its newest published day is missing."""
        options = self.config_entry.options
        known = self._known_channels(nmi)
        enabled = enabled_channels(options, nmi, known)
        if not enabled and (known or options.get(CONF_CHANNELS, {}).get(nmi)):
            return False  # Every channel of this meter is set to Ignore.
        imported = {
            channel: self._streams[stat_id]
            for channel in enabled
            if (stat_id := statistic_id(nmi, channel)) in self._streams
        }

        if enabled and len(imported) == len(enabled):
            start = min(stream.next_day(latest) for stream in imported.values())
            data: ChannelDays = {}
            if start <= latest:
                data = await self._async_fetch_range(client, nmi, start, latest)
            enabled = enabled_channels(options, nmi, known | set(data))
            new_channels = [channel for channel in enabled if channel not in imported]
            history = (
                await self._async_fetch_history(client, nmi, latest)
                if new_channels
                else {}
            )
        else:
            # Some enabled channel has never been imported, or the meter's
            # channels are not known yet: request the meter's full history.
            data = history = await self._async_fetch_history(client, nmi, latest)
            enabled = enabled_channels(options, nmi, known | set(history))

        newest = _newest_day(data)
        for channel, name in enabled.items():
            stat_id = statistic_id(nmi, channel)
            if stream := self._streams.get(stat_id):
                await self._async_apply(
                    stream, name, data.get(channel, {}), newest, latest
                )
            else:
                await self._async_import_history(
                    nmi, channel, name, history.get(channel, {}), latest
                )

        lasts = [
            stream.last
            for channel in enabled
            if (stream := self._streams.get(statistic_id(nmi, channel))) and stream.last
        ]
        return bool(lasts) and max(lasts) < latest

    def _statistic_name(self, nmi: str, channel_name: str) -> str:
        return f"SAPN {self.meter_name(nmi)} {channel_name}"

    async def _async_import_history(
        self,
        nmi: str,
        channel: str,
        name: str,
        days: Mapping[date, DaySummary],
        latest: date,
    ) -> None:
        """Replace a statistic with a channel's complete history."""
        stat_id = statistic_id(nmi, channel)
        stream = Stream(nmi=nmi, channel=channel)
        points = merge_points(days.values())
        if points:
            await async_replace_statistics(
                self.hass,
                statistic_metadata(stat_id, self._statistic_name(nmi, name)),
                points,
            )
            stream.first, stream.last = min(days), max(days)
            stream.pending = {day for day, summary in days.items() if not summary.final}
            horizon = latest - timedelta(days=PENDING_RETENTION_DAYS)
            stream.pending.update(
                self._missing_days(days, max(stream.first, horizon), stream.last)
            )
            self._prune_pending(stream, latest)
            LOGGER.info(
                "Imported SAPN history for %s %s from %s to %s",
                nmi,
                channel,
                stream.first,
                stream.last,
            )
        else:
            # Statistics left under this ID by an earlier installation would
            # otherwise be continued as if this entry had written them.
            recorder = get_instance(self.hass)
            recorder.async_clear_statistics([stat_id])
            await recorder.async_block_till_done()
            LOGGER.info("SAPN has no history for %s %s yet", nmi, channel)
        self._streams[stat_id] = stream

    async def _async_apply(
        self,
        stream: Stream,
        name: str,
        days: Mapping[date, DaySummary],
        newest: date | None,
        latest: date,
    ) -> None:
        """Merge newly downloaded days into an imported channel."""
        start = stream.next_day(latest)
        points: dict[datetime, float] = {}
        for day, summary in days.items():
            if day < start and day not in stream.pending:
                continue
            points.update(summary.points())
            if summary.final:
                stream.pending.discard(day)
            else:
                stream.pending.add(day)
        if days:
            stream.first = min([*days, *([stream.first] if stream.first else [])])
            stream.last = max([*days, *([stream.last] if stream.last else [])])
        if stream.first is not None and newest is not None:
            # Other channels of this meter have data through ``newest``, so a
            # day this channel lacks before then is a gap to retry later.
            stream.pending.update(
                self._missing_days(days, max(start, stream.first), newest)
            )
        self._prune_pending(stream, latest)
        if points:
            assert stream.first is not None
            await async_merge_statistics(
                self.hass,
                statistic_metadata(
                    statistic_id(stream.nmi, stream.channel),
                    self._statistic_name(stream.nmi, name),
                ),
                points,
                day_start_utc(stream.first),
            )

    @staticmethod
    def _missing_days(present: Iterable[date], start: date, through: date) -> set[date]:
        """Return the days from ``start`` to ``through`` that are not present."""
        days = {start + timedelta(days=n) for n in range((through - start).days + 1)}
        return days - set(present)

    @staticmethod
    def _prune_pending(stream: Stream, latest: date) -> None:
        horizon = latest - timedelta(days=PENDING_RETENTION_DAYS)
        stream.pending = {
            day
            for day in stream.pending
            if day >= horizon and (stream.first is None or day >= stream.first)
        }

    async def _async_pace(self) -> None:
        """Keep a gap between portal requests."""
        delay = self._next_request - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_request = time.monotonic() + REQUEST_DELAY_SECONDS

    async def _async_download(
        self,
        client: SAPNClient,
        nmi: str,
        first_day: date,
        last_day: date,
    ) -> ChannelDays:
        """Download and summarize a date range, splitting it if SAPN refuses."""
        try:
            text = await self._async_request(client, nmi, first_day, last_day)
            if text is None:
                return {}
            return await self.hass.async_add_executor_job(
                summarize_nem12, text, nmi, first_day, last_day
            )
        except SAPNLoginFailedError:
            raise
        except (SAPNPortalError, NEM12Error) as err:
            if first_day >= last_day:
                raise SAPNPortalError(
                    f"SAPN data for {nmi} on {first_day} could not be read: {err}"
                ) from err
            LOGGER.debug(
                "Splitting SAPN request for %s %s to %s after: %s",
                nmi,
                first_day,
                last_day,
                err,
            )
        middle = first_day + (last_day - first_day) // 2
        older = await self._async_download(client, nmi, first_day, middle)
        newer = await self._async_download(client, nmi, middle + ONE_DAY, last_day)
        return _merge_days(older, newer)

    async def _async_request(
        self,
        client: SAPNClient,
        nmi: str,
        first_day: date,
        last_day: date,
    ) -> str | None:
        """Request NEM12 text, retrying once after a connection problem."""
        for attempt in range(2):
            await self._async_pace()
            try:
                return await client.download_nem12(nmi, first_day, last_day)
            except SAPNNoDataError:
                return None
            except SAPNConnectionError as err:
                if attempt:
                    raise
                LOGGER.debug("Retrying SAPN request for %s after: %s", nmi, err)
                await asyncio.sleep(CONNECTION_RETRY_SECONDS)
        raise AssertionError("unreachable")

    async def _async_fetch_range(
        self,
        client: SAPNClient,
        nmi: str,
        first_day: date,
        last_day: date,
    ) -> ChannelDays:
        """Download a date range in chunks, oldest first."""
        data: ChannelDays = {}
        chunk_start = first_day
        while chunk_start <= last_day:
            chunk_end = min(
                last_day, chunk_start + timedelta(days=HISTORY_CHUNK_DAYS - 1)
            )
            _merge_days(
                data,
                await self._async_download(client, nmi, chunk_start, chunk_end),
            )
            chunk_start = chunk_end + ONE_DAY
        return data

    async def _async_fetch_history(
        self,
        client: SAPNClient,
        nmi: str,
        latest: date,
    ) -> ChannelDays:
        """Download a meter's history, newest first, until it runs out."""
        data: ChannelDays = {}
        earliest = latest - timedelta(days=HISTORY_MAX_DAYS - 1)
        end = latest
        empty_days = 0
        error: SAPNPortalError | None = None
        while end >= earliest and empty_days < HISTORY_EMPTY_DAYS:
            start = max(earliest, end - timedelta(days=HISTORY_CHUNK_DAYS - 1))
            try:
                chunk = await self._async_download(client, nmi, start, end)
            except SAPNLoginFailedError:
                raise
            except SAPNPortalError as err:
                LOGGER.debug("Skipping SAPN history %s to %s: %s", start, end, err)
                error, chunk = err, {}
            if any(chunk.values()):
                _merge_days(data, chunk)
                empty_days = 0
            else:
                empty_days += (end - start).days + 1
            end = start - ONE_DAY
        if not data and error is not None:
            raise error
        return data
