"""Data coordinator for SA Power Networks Meter Data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, override

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import storage
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CONSUMPTION_CHANNELS,
    CONF_NMIS,
    CONF_RETURN_CHANNELS,
    DAILY_REFRESH_TIME,
    DATA_AVAILABLE_TIME,
    DEFAULT_CONSUMPTION_CHANNELS,
    DEFAULT_RETURN_CHANNELS,
    DOMAIN,
    HISTORICAL_CHUNK_DAYS,
    HISTORICAL_CHUNK_DELAY,
    LOGGER,
    SAPN_TIME_ZONE,
    STATISTICS_ALIGNMENT_VERSION,
    STATUS_ATTENTION,
    STATUS_BACKFILLING,
    STATUS_IMPORTED,
    STATUS_PARTIAL,
    STATUS_UP_TO_DATE,
    STATUS_WAITING,
    STORE_VERSION,
    UPDATE_INTERVAL,
)
from .schedule import (
    historical_chunk,
    latest_available_date,
    next_daily_refresh,
    utc_statistic_window,
)
from .statistics import (
    HourlyPoint,
    build_statistics,
    derive_base_sum,
    derive_prepend_base_sum,
    statistic_id,
    statistic_name,
)


class PortalAuthError(Exception):
    """Authentication failed inside the blocking portal worker."""


class PortalFetchError(Exception):
    """A top-level portal operation failed inside the blocking worker."""


@dataclass(frozen=True, slots=True)
class LocalDateRange:
    """A half-open range of Adelaide calendar dates."""

    start: date
    end: date

    @property
    def label(self) -> str:
        """Return a concise user-facing range."""
        if self.end == self.start + timedelta(days=1):
            return self.start.isoformat()
        final_date = self.end - timedelta(days=1)
        return f"{self.start.isoformat()} to {final_date.isoformat()}"


@dataclass(slots=True)
class FetchBatch:
    """Results from one blocking SAPN portal session."""

    streams: dict[str, dict[str, Any]] = field(default_factory=dict)
    no_data: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _fetch_meter_data(
    email: str,
    password: str,
    targets: dict[str, LocalDateRange],
    consumption_channels: str,
    return_channels: str,
) -> FetchBatch:
    """Fetch and transform date ranges without blocking HA's event loop."""
    from sapnmeterdata import (
        AuthError,
        FetchError,
        LoginError,
        NoDataError,
        login,
        meter,
    )

    from .transform import extract_hourly_streams

    try:
        client = login(email, password)
    except (AuthError, LoginError) as err:
        raise PortalAuthError(str(err)) from err
    except FetchError as err:
        raise PortalFetchError(str(err)) from err

    result = FetchBatch()
    for nmi, target in targets.items():
        # Adelaide midnight falls on a UTC half hour. Fetch the preceding local
        # day so the first UTC-aligned hour has all its source intervals.
        request_start = datetime.combine(
            target.start - timedelta(days=1),
            time.min,
        )
        request_end = datetime.combine(target.end, time.min)
        window_start, window_end = utc_statistic_window(
            target.start,
            target.end,
            SAPN_TIME_ZONE,
        )
        try:
            frame = meter(nmi, client).getdata(request_start, request_end)
        except (AuthError, LoginError) as err:
            raise PortalAuthError(str(err)) from err
        except NoDataError as err:
            result.no_data[nmi] = str(err)
        except FetchError as err:
            result.errors[nmi] = str(err)
        else:
            try:
                result.streams[nmi] = extract_hourly_streams(
                    frame,
                    nmi,
                    consumption_channels,
                    return_channels,
                    SAPN_TIME_ZONE,
                    window_start,
                    window_end,
                )
            except (TypeError, ValueError) as err:
                result.errors[nmi] = str(err)
    return result


class SAPNMeterDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch complete SAPN periods and import them as external statistics."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._entry = entry
        self._store = storage.Store[dict[str, Any]](
            hass,
            STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}",
        )
        self._state: dict[str, Any] | None = None
        self._unsub_daily_refresh: Callable[[], None] | None = None
        self._unsub_backfill_refresh: Callable[[], None] | None = None

    @callback
    def async_start_daily_refresh(self) -> None:
        """Schedule a refresh just after SAPN publishes the previous day."""
        self._schedule_daily_refresh(datetime.now(UTC))
        self._entry.async_on_unload(self._cancel_refresh_schedules)

    @callback
    def _schedule_daily_refresh(self, now: datetime) -> None:
        """Schedule the next 03:05 Adelaide refresh."""
        refresh_at = next_daily_refresh(
            now,
            SAPN_TIME_ZONE,
            DAILY_REFRESH_TIME,
        )
        self._unsub_daily_refresh = async_track_point_in_utc_time(
            self.hass,
            self._async_daily_refresh,
            refresh_at,
        )
        LOGGER.debug("Next SAPN daily import scheduled for %s", refresh_at)

    async def _async_daily_refresh(self, now: datetime) -> None:
        """Refresh and schedule the following daily run."""
        self._unsub_daily_refresh = None
        self._schedule_daily_refresh(now)
        await self.async_request_refresh()

    @callback
    def _schedule_backfill_refresh(self) -> None:
        """Schedule the next rate-limited historical chunk."""
        if self._unsub_backfill_refresh is not None:
            return
        self._unsub_backfill_refresh = async_call_later(
            self.hass,
            HISTORICAL_CHUNK_DELAY,
            self._async_backfill_refresh,
        )

    async def _async_backfill_refresh(self, _now: datetime) -> None:
        """Import the next historical chunk."""
        self._unsub_backfill_refresh = None
        await self.async_request_refresh()

    @callback
    def _cancel_refresh_schedules(self) -> None:
        """Cancel daily and historical refresh callbacks."""
        if self._unsub_daily_refresh is not None:
            self._unsub_daily_refresh()
            self._unsub_daily_refresh = None
        if self._unsub_backfill_refresh is not None:
            self._unsub_backfill_refresh()
            self._unsub_backfill_refresh = None

    async def _async_load_state(self) -> dict[str, Any]:
        """Load the import checkpoint once."""
        if self._state is None:
            loaded = await self._store.async_load()
            self._state = dict(loaded or {})
            self._state.setdefault("last_processed", {})
            self._state.setdefault("earliest_processed", {})
            self._state.setdefault(
                "historical_backfill",
                {
                    "active": False,
                    "before": {},
                    "completed": [],
                    "failed": {},
                    "chunks_imported": 0,
                },
            )
        return self._state

    async def _async_migrate_statistics_alignment(
        self,
        state: dict[str, Any],
        nmis: list[str],
    ) -> None:
        """Replace local-hour 0.2.2 rows with UTC-hour statistics."""
        if state.get("statistics_alignment_version") == STATISTICS_ALIGNMENT_VERSION:
            return

        stat_ids = [
            statistic_id(nmi, direction)
            for nmi in nmis
            for direction in ("consumption", "return")
        ]
        recorder = get_instance(self.hass)
        recorder.async_clear_statistics(stat_ids)
        await recorder.async_block_till_done()
        state["statistics_alignment_version"] = STATISTICS_ALIGNMENT_VERSION
        state["last_processed"] = {}
        state["earliest_processed"] = {}
        state["historical_backfill"] = {
            "active": False,
            "before": {},
            "completed": [],
            "failed": {},
            "chunks_imported": 0,
        }
        state.pop("last_successful_import", None)
        await self._store.async_save(state)
        LOGGER.info("Reset SAPN statistics for UTC-hour alignment migration")

    @staticmethod
    def _target_dates(
        nmis: list[str],
        last_processed: dict[str, str],
        latest_available_day: date,
    ) -> dict[str, date]:
        """Return the next available forward date due for each NMI."""
        targets: dict[str, date] = {}
        for nmi in nmis:
            stored = last_processed.get(nmi)
            if stored:
                try:
                    target = date.fromisoformat(stored) + timedelta(days=1)
                except ValueError:
                    target = latest_available_day
            else:
                target = latest_available_day
            if target <= latest_available_day:
                targets[nmi] = target
        return targets

    @staticmethod
    def _historical_ranges(
        nmis: list[str],
        state: dict[str, Any],
        latest_available_day: date,
    ) -> dict[str, LocalDateRange]:
        """Return one backwards chunk for each active NMI."""
        backfill = state["historical_backfill"]
        if not backfill.get("active"):
            return {}

        completed = set(backfill.get("completed", []))
        failed = set(backfill.get("failed", {}))
        before_by_nmi = backfill.setdefault("before", {})
        earliest = state["earliest_processed"]
        last_processed = state["last_processed"]
        ranges: dict[str, LocalDateRange] = {}
        for nmi in nmis:
            if nmi in completed or nmi in failed:
                continue
            before_raw = before_by_nmi.get(nmi)
            if before_raw is None:
                before_raw = earliest.get(nmi) or last_processed.get(nmi)
            try:
                before = (
                    date.fromisoformat(before_raw)
                    if before_raw
                    else latest_available_day + timedelta(days=1)
                )
            except ValueError:
                before = latest_available_day + timedelta(days=1)
            start, end = historical_chunk(before, HISTORICAL_CHUNK_DAYS)
            before_by_nmi[nmi] = before.isoformat()
            ranges[nmi] = LocalDateRange(start, end)
        return ranges

    async def async_start_historical_backfill(self) -> None:
        """Start or restart the resumable historical import."""
        state = await self._async_load_state()
        config = {**self._entry.data, **self._entry.options}
        nmis = [str(nmi) for nmi in config[CONF_NMIS]]
        available_day = latest_available_date(
            datetime.now(UTC),
            SAPN_TIME_ZONE,
            DATA_AVAILABLE_TIME,
        )
        earliest = state["earliest_processed"]
        last_processed = state["last_processed"]
        backfill = state["historical_backfill"]
        backfill["active"] = True
        backfill["completed"] = []
        backfill["failed"] = {}
        backfill["before"] = {
            nmi: (
                earliest.get(nmi)
                or last_processed.get(nmi)
                or (available_day + timedelta(days=1)).isoformat()
            )
            for nmi in nmis
        }
        await self._store.async_save(state)
        await self.async_request_refresh()

    async def _async_fetch_ranges(
        self,
        config: dict[str, Any],
        targets: dict[str, LocalDateRange],
    ) -> FetchBatch:
        """Fetch one forward or historical batch."""
        try:
            return await self.hass.async_add_executor_job(
                _fetch_meter_data,
                config[CONF_EMAIL],
                config[CONF_PASSWORD],
                targets,
                config.get(
                    CONF_CONSUMPTION_CHANNELS,
                    DEFAULT_CONSUMPTION_CHANNELS,
                ),
                config.get(CONF_RETURN_CHANNELS, DEFAULT_RETURN_CHANNELS),
            )
        except PortalAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PortalFetchError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            LOGGER.exception("Unexpected error while contacting SAPN")
            raise UpdateFailed(str(err)) from err

    async def _async_existing_statistics(
        self,
        stat_id: str,
        points: tuple[HourlyPoint, ...],
        mode: Literal["forward", "backfill"],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load rows needed to append, replace, or prepend a cumulative sum."""
        recorder = get_instance(self.hass)
        start = points[0].start
        end = points[-1].start + timedelta(hours=1)
        existing_result = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {stat_id},
            "hour",
            None,
            {"state", "sum"},
        )
        existing = list(existing_result.get(stat_id, []))

        if mode == "forward":
            reference_result = await recorder.async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,
                stat_id,
                False,
                {"state", "sum"},
            )
        else:
            reference_result = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                end,
                end + timedelta(hours=1),
                {stat_id},
                "hour",
                None,
                {"state", "sum"},
            )
        return existing, list(reference_result.get(stat_id, []))

    async def _async_import_stream(
        self,
        nmi: str,
        direction: str,
        points: tuple[HourlyPoint, ...],
        mode: Literal["forward", "backfill"],
    ) -> str:
        """Import one consumption or return-to-grid stream."""
        stat_id = statistic_id(nmi, direction)
        existing, reference = await self._async_existing_statistics(
            stat_id,
            points,
            mode,
        )
        if mode == "backfill":
            base_sum = derive_prepend_base_sum(points, existing, reference)
            include_baseline = False
        else:
            base_sum, include_baseline = derive_base_sum(
                points,
                existing,
                reference,
            )
        rows = build_statistics(points, base_sum, include_baseline)

        metadata: StatisticMetaData = {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": statistic_name(nmi, direction),
            "source": DOMAIN,
            "statistic_id": stat_id,
            "unit_class": "energy",
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        }
        async_add_external_statistics(
            self.hass,
            metadata,
            [StatisticData(**row) for row in rows],
        )
        return stat_id

    @staticmethod
    def _backfill_snapshot(state: dict[str, Any]) -> dict[str, Any]:
        """Return a copy suitable for status attributes."""
        backfill = state["historical_backfill"]
        return {
            "active": bool(backfill.get("active")),
            "chunk_days": HISTORICAL_CHUNK_DAYS,
            "before": dict(backfill.get("before", {})),
            "completed": list(backfill.get("completed", [])),
            "failed": dict(backfill.get("failed", {})),
            "chunks_imported": int(backfill.get("chunks_imported", 0)),
        }

    @override
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the newest day or one rate-limited historical chunk."""
        state = await self._async_load_state()
        config = {**self._entry.data, **self._entry.options}
        nmis = [str(nmi) for nmi in config[CONF_NMIS]]
        await self._async_migrate_statistics_alignment(state, nmis)

        last_processed: dict[str, str] = state["last_processed"]
        earliest_processed: dict[str, str] = state["earliest_processed"]
        available_day = latest_available_date(
            datetime.now(UTC),
            SAPN_TIME_ZONE,
            DATA_AVAILABLE_TIME,
        )
        forward_dates = self._target_dates(
            nmis,
            last_processed,
            available_day,
        )

        mode: Literal["forward", "backfill"] = "forward"
        targets = {
            nmi: LocalDateRange(target, target + timedelta(days=1))
            for nmi, target in forward_dates.items()
        }
        if not targets:
            mode = "backfill"
            targets = self._historical_ranges(nmis, state, available_day)

        requested_dates = {nmi: target.label for nmi, target in targets.items()}
        base_result: dict[str, Any] = {
            "status": STATUS_UP_TO_DATE,
            "latest_available_day": available_day.isoformat(),
            "requested_dates": requested_dates,
            "request_type": mode if targets else None,
            "imported": [],
            "waiting": [],
            "skipped": [],
            "errors": {},
            "channels": {},
            "statistics": {},
            "last_processed": dict(last_processed),
            "earliest_processed": dict(earliest_processed),
            "historical_backfill": self._backfill_snapshot(state),
            "last_successful_import": state.get("last_successful_import"),
        }
        if not targets:
            return base_result

        batch = await self._async_fetch_ranges(config, targets)
        imported: list[str] = []
        waiting: list[str] = []
        skipped: list[str] = []
        errors = dict(batch.errors)
        channels: dict[str, dict[str, list[str]]] = {}
        statistics: dict[str, dict[str, str]] = {}
        queued_statistics = False
        state_changed = False
        backfill_progress = False

        if mode == "forward":
            for nmi, message in batch.no_data.items():
                target_date = targets[nmi].start
                if target_date < available_day:
                    skipped.append(nmi)
                    last_processed[nmi] = target_date.isoformat()
                    state_changed = True
                    LOGGER.warning(
                        "Skipping unavailable SAPN data for NMI %s on %s: %s",
                        nmi,
                        target_date,
                        message,
                    )
                else:
                    waiting.append(nmi)
        else:
            backfill = state["historical_backfill"]
            completed = set(backfill.get("completed", []))
            for nmi, message in batch.no_data.items():
                completed.add(nmi)
                backfill_progress = True
                state_changed = True
                LOGGER.info(
                    "Historical SAPN data ended before %s for NMI %s: %s",
                    targets[nmi].end,
                    nmi,
                    message,
                )
            backfill["completed"] = sorted(completed)
            if batch.errors:
                failed = dict(backfill.get("failed", {}))
                failed.update(batch.errors)
                backfill["failed"] = failed
                state_changed = True

        for nmi, streams in batch.streams.items():
            if not streams:
                errors[nmi] = (
                    "No channels matched the configured consumption or "
                    "return-to-grid patterns."
                )
                if mode == "backfill":
                    state["historical_backfill"].setdefault("failed", {})[nmi] = errors[
                        nmi
                    ]
                    state_changed = True
                continue

            channels[nmi] = {}
            statistics[nmi] = {}
            for direction, stream in streams.items():
                channels[nmi][direction] = list(stream.channels)
                statistics[nmi][direction] = await self._async_import_stream(
                    nmi,
                    direction,
                    stream.points,
                    mode,
                )
                queued_statistics = True
            imported.append(nmi)
            state_changed = True

            if mode == "forward":
                target_date = targets[nmi].start
                last_processed[nmi] = target_date.isoformat()
                stored_earliest = earliest_processed.get(nmi)
                target_iso = target_date.isoformat()
                if stored_earliest is None or target_iso < stored_earliest:
                    earliest_processed[nmi] = target_iso
            else:
                backfill = state["historical_backfill"]
                backfill.setdefault("before", {})[nmi] = targets[nmi].start.isoformat()
                earliest_processed[nmi] = targets[nmi].start.isoformat()
                backfill["chunks_imported"] = (
                    int(backfill.get("chunks_imported", 0)) + 1
                )
                backfill_progress = True

        if queued_statistics:
            await get_instance(self.hass).async_block_till_done()

        if imported:
            state["last_successful_import"] = {
                "completed_at": datetime.now(UTC).isoformat(),
                "type": mode,
                "dates": requested_dates,
                "nmis": imported,
                "channels": channels,
                "statistics": statistics,
            }

        if mode == "backfill":
            backfill = state["historical_backfill"]
            completed = set(backfill.get("completed", []))
            failed = set(backfill.get("failed", {}))
            pending = [nmi for nmi in nmis if nmi not in (completed | failed)]
            backfill["active"] = bool(pending)
            if pending and backfill_progress:
                self._schedule_backfill_refresh()
            elif not pending and self._unsub_backfill_refresh is not None:
                self._unsub_backfill_refresh()
                self._unsub_backfill_refresh = None
        elif state["historical_backfill"].get("active") and (imported or skipped):
            self._schedule_backfill_refresh()

        if state_changed or imported:
            state["last_processed"] = last_processed
            state["earliest_processed"] = earliest_processed
            await self._store.async_save(state)

        if (
            mode == "forward"
            and batch.errors
            and not imported
            and not waiting
            and not skipped
        ):
            details = "; ".join(f"{nmi}: {error}" for nmi, error in errors.items())
            raise UpdateFailed(details)

        if mode == "backfill" and state["historical_backfill"].get("active"):
            status = STATUS_BACKFILLING
        elif errors or waiting or skipped:
            status = STATUS_PARTIAL if imported else STATUS_ATTENTION
            if waiting and not errors and not imported and not skipped:
                status = STATUS_WAITING
        else:
            status = STATUS_IMPORTED if imported else STATUS_UP_TO_DATE

        base_result.update(
            {
                "status": status,
                "imported": imported,
                "waiting": waiting,
                "skipped": skipped,
                "errors": errors,
                "channels": channels,
                "statistics": statistics,
                "last_processed": dict(last_processed),
                "earliest_processed": dict(earliest_processed),
                "historical_backfill": self._backfill_snapshot(state),
                "last_successful_import": state.get("last_successful_import"),
            }
        )
        return base_result
