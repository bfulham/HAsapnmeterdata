"""Data coordinator for SA Power Networks Meter Data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, override

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
from homeassistant.helpers.event import async_track_point_in_utc_time
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
    LOGGER,
    SAPN_TIME_ZONE,
    STATUS_ATTENTION,
    STATUS_IMPORTED,
    STATUS_PARTIAL,
    STATUS_UP_TO_DATE,
    STATUS_WAITING,
    STORE_VERSION,
    UPDATE_INTERVAL,
)
from .schedule import latest_available_date, next_daily_refresh


class PortalAuthError(Exception):
    """Authentication failed inside the blocking portal worker."""


class PortalFetchError(Exception):
    """A top-level portal operation failed inside the blocking worker."""


@dataclass(slots=True)
class FetchBatch:
    """Results from one blocking SAPN portal session."""

    streams: dict[str, dict[str, Any]] = field(default_factory=dict)
    no_data: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _fetch_meter_data(
    email: str,
    password: str,
    targets: dict[str, date],
    consumption_channels: str,
    return_channels: str,
) -> FetchBatch:
    """Fetch and transform pending days without blocking HA's event loop."""
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
    for nmi, target_date in targets.items():
        start = datetime.combine(target_date, time.min)
        end = start + timedelta(days=1)
        try:
            frame = meter(nmi, client).getdata(start, end)
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
                )
            except (TypeError, ValueError) as err:
                result.errors[nmi] = str(err)
    return result


class SAPNMeterDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch complete SAPN days and import them as external statistics."""

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

    @callback
    def async_start_daily_refresh(self) -> None:
        """Schedule a refresh just after SAPN publishes the previous day."""
        self._schedule_daily_refresh(datetime.now(UTC))
        self._entry.async_on_unload(self._cancel_daily_refresh)

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
    def _cancel_daily_refresh(self) -> None:
        """Cancel the scheduled daily refresh."""
        if self._unsub_daily_refresh is not None:
            self._unsub_daily_refresh()
            self._unsub_daily_refresh = None

    async def _async_load_state(self) -> dict[str, Any]:
        """Load the import checkpoint once."""
        if self._state is None:
            loaded = await self._store.async_load()
            self._state = dict(loaded or {})
            self._state.setdefault("last_processed", {})
        return self._state

    @staticmethod
    def _target_dates(
        nmis: list[str],
        last_processed: dict[str, str],
        latest_available_day: date,
    ) -> dict[str, date]:
        """Return the next available date due for each NMI."""
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

    async def _async_existing_statistics(
        self,
        stat_id: str,
        points: tuple[Any, ...],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load rows needed to continue or deterministically replace a sum."""
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
            {"sum"},
        )
        existing = list(existing_result.get(stat_id, []))
        last_result = await recorder.async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            stat_id,
            False,
            {"sum"},
        )
        return existing, list(last_result.get(stat_id, []))

    async def _async_import_stream(
        self,
        nmi: str,
        direction: str,
        points: tuple[Any, ...],
    ) -> str:
        """Import one consumption or return-to-grid stream."""
        from .transform import (
            build_statistics,
            derive_base_sum,
            statistic_id,
            statistic_name,
        )

        stat_id = statistic_id(nmi, direction)
        existing, last = await self._async_existing_statistics(stat_id, points)
        base_sum, include_baseline = derive_base_sum(points, existing, last)
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

    @override
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and import data through SAPN's newest available day."""
        state = await self._async_load_state()
        last_processed: dict[str, str] = state["last_processed"]
        config = {**self._entry.data, **self._entry.options}
        nmis = [str(nmi) for nmi in config[CONF_NMIS]]
        available_day = latest_available_date(
            datetime.now(UTC),
            SAPN_TIME_ZONE,
            DATA_AVAILABLE_TIME,
        )
        targets = self._target_dates(nmis, last_processed, available_day)

        base_result: dict[str, Any] = {
            "status": STATUS_UP_TO_DATE,
            "latest_available_day": available_day.isoformat(),
            "requested_dates": {
                nmi: target.isoformat() for nmi, target in targets.items()
            },
            "imported": [],
            "waiting": [],
            "skipped": [],
            "errors": {},
            "channels": {},
            "statistics": {},
            "last_processed": dict(last_processed),
        }
        if not targets:
            return base_result

        try:
            batch = await self.hass.async_add_executor_job(
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

        imported: list[str] = []
        waiting: list[str] = []
        skipped: list[str] = []
        errors = dict(batch.errors)
        channels: dict[str, dict[str, list[str]]] = {}
        statistics: dict[str, dict[str, str]] = {}
        queued_statistics = False

        for nmi, message in batch.no_data.items():
            if targets[nmi] < available_day:
                skipped.append(nmi)
                last_processed[nmi] = targets[nmi].isoformat()
                LOGGER.warning(
                    "Skipping unavailable historical SAPN data for NMI %s on %s: %s",
                    nmi,
                    targets[nmi],
                    message,
                )
            else:
                waiting.append(nmi)

        for nmi, streams in batch.streams.items():
            if not streams:
                errors[nmi] = (
                    "No channels matched the configured consumption or "
                    "return-to-grid patterns."
                )
                continue

            channels[nmi] = {}
            statistics[nmi] = {}
            for direction, stream in streams.items():
                channels[nmi][direction] = list(stream.channels)
                statistics[nmi][direction] = await self._async_import_stream(
                    nmi,
                    direction,
                    stream.points,
                )
                queued_statistics = True
            imported.append(nmi)
            last_processed[nmi] = targets[nmi].isoformat()

        if queued_statistics:
            await get_instance(self.hass).async_block_till_done()

        if imported or skipped:
            state["last_processed"] = last_processed
            await self._store.async_save(state)

        if batch.errors and not imported and not waiting and not skipped:
            details = "; ".join(f"{nmi}: {error}" for nmi, error in errors.items())
            raise UpdateFailed(details)

        if errors or waiting or skipped:
            status = STATUS_PARTIAL if imported else STATUS_ATTENTION
            if waiting and not errors and not imported and not skipped:
                status = STATUS_WAITING
        else:
            status = STATUS_IMPORTED

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
            }
        )
        return base_result
