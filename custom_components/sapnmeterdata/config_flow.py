"""Config flow for SA Power Networks Meter Data."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .channels import channel_settings
from .const import (
    CHANNEL_TYPES,
    CONF_CHANNEL_NAME,
    CONF_CHANNEL_TYPE,
    CONF_CHANNELS,
    CONF_METER_NAMES,
    CONF_NMIS,
    DISCOVERY_DAYS,
    DOMAIN,
    LOGGER,
)
from .coordinator import SAPNConfigEntry, portal_session
from .portal import (
    MeterAssignment,
    NEM12Error,
    SAPNAuthError,
    SAPNClient,
    SAPNConnectionError,
    SAPNLoginFailedError,
    SAPNNoDataError,
    SAPNPortalError,
)
from .series import summarize_nem12
from .timing import latest_published_day

EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})
CHANNEL_TYPE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(CHANNEL_TYPES),
        translation_key="channel_type",
        mode=SelectSelectorMode.DROPDOWN,
    )
)


def _error_key(err: Exception) -> str:
    """Map a portal error to a form error key."""
    if isinstance(err, (SAPNAuthError, SAPNLoginFailedError)):
        return "invalid_auth"
    if isinstance(err, SAPNConnectionError):
        return "cannot_connect"
    if isinstance(err, SAPNPortalError):
        return "portal_error"
    LOGGER.exception("Unexpected error talking to the SAPN portal")
    return "unknown"


async def _async_fetch_meters(
    hass: HomeAssistant,
    email: str,
    password: str,
) -> list[MeterAssignment]:
    with portal_session(hass) as session:
        client = SAPNClient(session, email, password)
        await client.login()
        return await client.get_assignments()


async def _async_discover_channels(
    hass: HomeAssistant,
    email: str,
    password: str,
    nmis: list[str],
) -> dict[str, list[str]]:
    """Return the energy channels in each meter's recent data.

    A meter whose sample cannot be read gets no channels here; the default
    channels are used once syncing finds its data.
    """
    last_day = latest_published_day(dt_util.utcnow())
    first_day = last_day - timedelta(days=DISCOVERY_DAYS - 1)
    found: dict[str, list[str]] = {}
    with portal_session(hass) as session:
        client = SAPNClient(session, email, password)
        await client.login()
        for nmi in nmis:
            found[nmi] = []
            try:
                text = await client.download_nem12(nmi, first_day, last_day)
                channels = await hass.async_add_executor_job(
                    summarize_nem12, text, nmi, first_day, last_day
                )
            except SAPNNoDataError:
                continue
            except SAPNLoginFailedError:
                raise
            except (SAPNPortalError, NEM12Error) as err:
                LOGGER.warning("Could not read recent SAPN data for %s: %s", nmi, err)
                continue
            found[nmi] = sorted(channels)
    return found


def _meter_label(meter: MeterAssignment) -> str:
    return meter.nmi if meter.name == meter.nmi else f"{meter.name} ({meter.nmi})"


class _MeterSteps:
    """Meter selection and channel naming shared by setup and options."""

    hass: HomeAssistant
    _email: str
    _password: str
    _defaults: Mapping[str, Any]
    _meters: dict[str, MeterAssignment]
    _excluded: list[MeterAssignment]
    _selected: list[str]
    _discovered: dict[str, list[str]]
    _channels: dict[str, dict[str, dict[str, str]]]
    _queue: list[str]

    def _init_steps(self, defaults: Mapping[str, Any]) -> None:
        self._email = ""
        self._password = ""
        self._defaults = defaults
        self._meters = {}
        self._excluded = []
        self._selected = []
        self._discovered = {}
        self._channels = {}
        self._queue = []

    async def _async_load_meters(self, email: str, password: str) -> str | None:
        """Sign in and list the account's meters; return an error key."""
        try:
            assignments = await _async_fetch_meters(self.hass, email, password)
        except Exception as err:  # noqa: BLE001
            return _error_key(err)
        self._email, self._password = email, password
        self._meters = {
            meter.nmi: meter
            for meter in assignments
            if meter.supports_interval_data is not False
        }
        self._excluded = [
            meter for meter in assignments if meter.supports_interval_data is False
        ]
        return None if self._meters else "no_interval_meters"

    def _options(self) -> dict[str, Any]:
        return {
            CONF_NMIS: list(self._selected),
            CONF_METER_NAMES: {nmi: self._meters[nmi].name for nmi in self._selected},
            CONF_CHANNELS: {nmi: self._channels.get(nmi, {}) for nmi in self._selected},
        }

    def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_meters(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose which meters to import."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = [nmi for nmi in user_input[CONF_NMIS] if nmi in self._meters]
            if not selected:
                errors["base"] = "no_meters_selected"
            else:
                try:
                    self._discovered = await _async_discover_channels(
                        self.hass, self._email, self._password, selected
                    )
                except Exception as err:  # noqa: BLE001
                    errors["base"] = _error_key(err)
                else:
                    self._selected = selected
                    self._queue = list(selected)
                    return await self.async_step_channels()

        default = [
            nmi for nmi in self._defaults.get(CONF_NMIS, []) if nmi in self._meters
        ] or list(self._meters)
        schema = vol.Schema(
            {
                vol.Required(CONF_NMIS, default=default): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=nmi, label=_meter_label(meter))
                            for nmi, meter in self._meters.items()
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        excluded = ", ".join(
            f"{_meter_label(meter)}: {meter.meter_type_label}"
            for meter in self._excluded
        )
        return self.async_show_form(  # type: ignore[attr-defined,no-any-return]
            step_id="meters",
            data_schema=schema,
            errors=errors,
            description_placeholders={"excluded": excluded or "none"},
        )

    async def async_step_channels(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Name each channel of one meter and choose how it is used."""
        nmi = self._queue[0]
        configured = self._defaults.get(CONF_CHANNELS, {}).get(nmi, {})
        channels = sorted(set(self._discovered.get(nmi, [])) | set(configured))

        if channels and user_input is not None:
            self._channels[nmi] = {
                channel: {
                    CONF_CHANNEL_NAME: (
                        str(user_input[f"{channel} name"]).strip() or channel
                    ),
                    CONF_CHANNEL_TYPE: user_input[f"{channel} use"],
                }
                for channel in channels
            }
        if not channels or user_input is not None:
            # A meter with no recent data gets default channels once SAPN
            # returns some.
            self._channels.setdefault(nmi, {})
            self._queue.pop(0)
            if self._queue:
                return await self.async_step_channels()
            return self._async_finish()

        fields: dict[Any, Any] = {}
        for channel in channels:
            name, channel_type = channel_settings(self._defaults, nmi, channel)
            fields[vol.Required(f"{channel} name", default=name)] = TextSelector()
            fields[vol.Required(f"{channel} use", default=channel_type)] = (
                CHANNEL_TYPE_SELECTOR
            )
        return self.async_show_form(  # type: ignore[attr-defined,no-any-return]
            step_id="channels",
            data_schema=vol.Schema(fields),
            description_placeholders={"meter": _meter_label(self._meters[nmi])},
        )


class SAPNConfigFlow(_MeterSteps, ConfigFlow, domain=DOMAIN):
    """Set up SA Power Networks Meter Data."""

    VERSION = 5

    def __init__(self) -> None:
        """Initialize the flow."""
        self._init_steps({})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SAPNConfigEntry) -> SAPNOptionsFlow:
        """Return the options flow."""
        return SAPNOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Ask for the portal login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.casefold())
            self._abort_if_unique_id_configured()
            if error := await self._async_load_meters(email, user_input[CONF_PASSWORD]):
                errors["base"] = error
            else:
                return await self.async_step_meters()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA,
                {CONF_EMAIL: user_input[CONF_EMAIL]} if user_input else {},
            ),
            errors=errors,
        )

    def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._email,
            data={CONF_EMAIL: self._email, CONF_PASSWORD: self._password},
            options=self._options(),
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start reauthentication after SAPN rejects the login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await self._async_load_meters(email, user_input[CONF_PASSWORD]):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": email},
        )


class SAPNOptionsFlow(_MeterSteps, OptionsFlow):
    """Change the imported meters and channels."""

    config_entry: SAPNConfigEntry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Refresh the account's meters, then show the meter selection."""
        self._init_steps(self.config_entry.options)
        if error := await self._async_load_meters(
            self.config_entry.data[CONF_EMAIL],
            self.config_entry.data[CONF_PASSWORD],
        ):
            return self.async_abort(reason=error)
        return await self.async_step_meters()

    def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options())
