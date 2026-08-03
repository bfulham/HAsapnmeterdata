"""Config flow for SA Power Networks Meter Data."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import SectionConfig, section
from homeassistant.helpers import selector

from .channels import merge_channel_config
from .const import (
    CHANNEL_DISCOVERY_DAYS,
    CHANNEL_TYPE_CONSUMPTION,
    CHANNEL_TYPE_IGNORE,
    CHANNEL_TYPE_RETURN,
    CONF_AVAILABLE_NMIS,
    CONF_CHANNEL_CONFIG,
    CONF_CHANNEL_NAME,
    CONF_CHANNEL_TYPE,
    CONF_CONSUMPTION_CHANNELS,
    CONF_EXCLUDED_NMIS,
    CONF_NMI_NAMES,
    CONF_NMIS,
    CONF_RETURN_CHANNELS,
    DATA_AVAILABLE_TIME,
    DEFAULT_CONSUMPTION_CHANNELS,
    DEFAULT_RETURN_CHANNELS,
    DOMAIN,
    SAPN_TIME_ZONE,
)
from .meters import meter_type_label, supports_interval_data
from .schedule import latest_available_date

_LOGGER = logging.getLogger(__name__)


class InvalidAuthError(Exception):
    """Credentials were rejected by the SAPN portal."""


class CannotConnectError(Exception):
    """The SAPN portal could not be reached or queried."""


def _connect_account(
    email: str,
    password: str,
) -> tuple[Any, list[str], dict[str, str], dict[str, str]]:
    """Validate credentials and return a reusable client and meter metadata."""
    # These imports stay inside the executor worker so opening Add Integration
    # does not load pandas and the NEM12 parser on Home Assistant's event loop.
    from sapnmeterdata import AuthError, FetchError, LoginError, login

    try:
        client = login(email, password)
        assignments = client.getNMIAssignments()
        supported_assignments = [
            assignment
            for assignment in assignments
            if supports_interval_data(assignment) is not False
        ]
        return (
            client,
            [assignment.nmi for assignment in supported_assignments],
            {assignment.nmi: assignment.friendly_name for assignment in assignments},
            {
                assignment.nmi: meter_type_label(assignment)
                for assignment in assignments
                if supports_interval_data(assignment) is False
            },
        )
    except (AuthError, LoginError) as err:
        raise InvalidAuthError from err
    except FetchError as err:
        raise CannotConnectError from err


def _discover_channels(
    client: Any,
    nmis: list[str],
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Read a bounded recent period and return channels found for each NMI."""
    from sapnmeterdata import (
        AuthError,
        FetchError,
        LoginError,
        NoDataError,
        meter,
    )

    available_day = latest_available_date(
        datetime.now(UTC),
        SAPN_TIME_ZONE,
        DATA_AVAILABLE_TIME,
    )
    start = datetime.combine(
        available_day - timedelta(days=CHANNEL_DISCOVERY_DAYS - 1),
        time.min,
    )
    end = datetime.combine(available_day + timedelta(days=1), time.min)
    discovered: dict[str, tuple[str, ...]] = {}
    errors: dict[str, str] = {}

    for nmi in nmis:
        try:
            frame = meter(nmi, client).getdata(start, end)
            channels = {
                str(column[1]).strip().upper()
                for column in frame.columns
                if len(column) >= 2
                and str(column[0]) == str(nmi)
                and str(column[1]).strip()
            }
            discovered[nmi] = tuple(sorted(channels))
            if not channels:
                errors[nmi] = "No NEM12 channels were present in the sample."
        except (AuthError, LoginError) as err:
            raise InvalidAuthError from err
        except NoDataError as err:
            discovered[nmi] = ()
            errors[nmi] = str(err)
        except (FetchError, TypeError, ValueError) as err:
            discovered[nmi] = ()
            errors[nmi] = str(err)

    return discovered, errors


def _credentials_schema() -> vol.Schema:
    """Return the SAPN credentials form schema."""
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.EMAIL,
                    autocomplete="username",
                )
            ),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
        }
    )


def _meter_label(nmi: str, nmi_names: Mapping[str, str]) -> str:
    """Return an unambiguous meter label."""
    friendly_name = str(nmi_names.get(nmi, nmi)).strip() or nmi
    return f"{friendly_name} ({nmi})" if friendly_name != nmi else nmi


def _excluded_meter_summary(
    excluded_nmis: Mapping[str, str],
    nmi_names: Mapping[str, str],
) -> str:
    """Return a concise list of automatically excluded basic meters."""
    if not excluded_nmis:
        return "No basic or manually read meters were found."
    return "Excluded automatically: " + ", ".join(
        f"{_meter_label(nmi, nmi_names)} — {meter_type}"
        for nmi, meter_type in sorted(excluded_nmis.items())
    )


def _meter_schema(
    available_nmis: list[str],
    nmi_names: Mapping[str, str],
    defaults: Mapping[str, Any] | None = None,
) -> vol.Schema:
    """Return the meter selection schema."""
    defaults = defaults or {}
    selected = [
        nmi for nmi in defaults.get(CONF_NMIS, available_nmis) if nmi in available_nmis
    ]
    if not selected:
        selected = available_nmis
    meter_options = [
        {"value": nmi, "label": _meter_label(nmi, nmi_names)} for nmi in available_nmis
    ]
    return vol.Schema(
        {
            vol.Required(CONF_NMIS, default=selected): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=meter_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _channel_type_selector() -> selector.SelectSelector:
    """Return the channel-use selector."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                {
                    "value": CHANNEL_TYPE_CONSUMPTION,
                    "label": "Grid consumption",
                },
                {
                    "value": CHANNEL_TYPE_RETURN,
                    "label": "Return to grid",
                },
                {"value": CHANNEL_TYPE_IGNORE, "label": "Ignore"},
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _channel_schema(
    nmis: list[str],
    nmi_names: Mapping[str, str],
    channel_config: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> tuple[
    vol.Schema,
    dict[str, tuple[str, dict[str, tuple[str, str]]]],
]:
    """Return grouped name/type fields and their NMI/channel bindings."""
    schema: dict[Any, Any] = {}
    bindings: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {}

    for meter_index, nmi in enumerate(nmis, start=1):
        meter_channels = channel_config.get(nmi, {})
        if not meter_channels:
            continue
        section_key = f"{_meter_label(nmi, nmi_names)} channels"
        if section_key in bindings:
            section_key = f"{section_key} #{meter_index}"
        fields: dict[Any, Any] = {}
        channel_bindings: dict[str, tuple[str, str]] = {}
        for channel in sorted(meter_channels):
            definition = meter_channels[channel]
            name_key = f"{channel} name"
            type_key = f"{channel} use as"
            fields[
                vol.Required(
                    name_key,
                    default=definition.get(CONF_CHANNEL_NAME, channel),
                )
            ] = selector.TextSelector()
            fields[
                vol.Required(
                    type_key,
                    default=definition.get(CONF_CHANNEL_TYPE, CHANNEL_TYPE_IGNORE),
                )
            ] = _channel_type_selector()
            channel_bindings[channel] = (name_key, type_key)
        schema[vol.Required(section_key)] = section(
            vol.Schema(fields),
            SectionConfig(collapsed=True),
        )
        bindings[section_key] = (nmi, channel_bindings)

    if not schema:
        schema[vol.Required("Continue without discovered channels", default=True)] = (
            selector.BooleanSelector()
        )
    return vol.Schema(schema), bindings


def _channel_form_result(
    user_input: Mapping[str, Any],
    bindings: Mapping[str, tuple[str, Mapping[str, tuple[str, str]]]],
    defaults: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> dict[str, dict[str, dict[str, str]]]:
    """Convert dynamic channel form sections into stored configuration."""
    result = {
        str(nmi): {
            str(channel): {
                CONF_CHANNEL_NAME: str(definition.get(CONF_CHANNEL_NAME, channel)),
                CONF_CHANNEL_TYPE: str(
                    definition.get(CONF_CHANNEL_TYPE, CHANNEL_TYPE_IGNORE)
                ),
            }
            for channel, definition in channels.items()
        }
        for nmi, channels in defaults.items()
    }
    for section_key, (nmi, channel_bindings) in bindings.items():
        section_data = user_input.get(section_key, {})
        if not isinstance(section_data, Mapping):
            continue
        for channel, (name_key, type_key) in channel_bindings.items():
            name = str(section_data.get(name_key, channel)).strip() or channel
            channel_type = str(section_data.get(type_key, CHANNEL_TYPE_IGNORE)).strip()
            result.setdefault(nmi, {})[channel] = {
                CONF_CHANNEL_NAME: name,
                CONF_CHANNEL_TYPE: channel_type,
            }
    return result


def _discovery_warning(
    errors: Mapping[str, str],
    nmi_names: Mapping[str, str],
) -> str:
    """Return a concise warning for meters whose channels were unavailable."""
    if not errors:
        return "All selected meters were inspected successfully."
    labels = ", ".join(_meter_label(nmi, nmi_names) for nmi in errors)
    return (
        "No recent channels could be read for: "
        f"{labels}. They can be configured later when SAPN returns data."
    )


class SAPNMeterDataConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an SA Power Networks Meter Data config flow."""

    VERSION = 4

    def __init__(self) -> None:
        """Initialize the flow."""
        self._email = ""
        self._password = ""
        self._client: Any | None = None
        self._available_nmis: list[str] = []
        self._excluded_nmis: dict[str, str] = {}
        self._nmi_names: dict[str, str] = {}
        self._selected_nmis: list[str] = []
        self._channel_config: dict[str, dict[str, dict[str, str]]] = {}
        self._channel_errors: dict[str, str] = {}
        self._channel_bindings: dict[
            str,
            tuple[str, dict[str, tuple[str, str]]],
        ] = {}

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SAPNMeterDataOptionsFlow()

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect credentials and discover assigned NMIs."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            try:
                (
                    self._client,
                    self._available_nmis,
                    self._nmi_names,
                    self._excluded_nmis,
                ) = await self.hass.async_add_executor_job(
                    _connect_account,
                    self._email,
                    self._password,
                )
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating SAPN credentials")
                errors["base"] = "unknown"
            else:
                if not self._available_nmis:
                    errors["base"] = (
                        "no_interval_nmis" if self._excluded_nmis else "no_nmis"
                    )
                elif any(
                    str(entry.data.get(CONF_EMAIL, "")).casefold()
                    == self._email.casefold()
                    for entry in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                else:
                    await self.async_set_unique_id(self._email.casefold())
                    self._abort_if_unique_id_configured()
                    return await self.async_step_meters()

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(),
            errors=errors,
        )

    async def async_step_meters(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select meters and inspect their recent NEM12 channels."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_NMIS]:
                errors["base"] = "select_nmi"
            else:
                self._selected_nmis = [str(nmi) for nmi in user_input[CONF_NMIS]]
                try:
                    (
                        discovered,
                        self._channel_errors,
                    ) = await self.hass.async_add_executor_job(
                        _discover_channels,
                        self._client,
                        self._selected_nmis,
                    )
                except InvalidAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected error discovering SAPN channels")
                    errors["base"] = "channel_discovery"
                else:
                    self._channel_config = merge_channel_config(
                        self._selected_nmis,
                        discovered,
                    )
                    return await self.async_step_channels()

        return self.async_show_form(
            step_id="meters",
            data_schema=_meter_schema(
                self._available_nmis,
                self._nmi_names,
                {CONF_NMIS: self._selected_nmis or self._available_nmis},
            ),
            errors=errors,
            description_placeholders={
                "excluded_meters": _excluded_meter_summary(
                    self._excluded_nmis,
                    self._nmi_names,
                )
            },
        )

    async def async_step_channels(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Name every discovered channel and choose how it is imported."""
        data_schema, self._channel_bindings = _channel_schema(
            self._selected_nmis,
            self._nmi_names,
            self._channel_config,
        )
        if user_input is not None:
            channel_config = _channel_form_result(
                user_input,
                self._channel_bindings,
                self._channel_config,
            )
            return self.async_create_entry(
                title=self._email,
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_AVAILABLE_NMIS: self._available_nmis,
                    CONF_EXCLUDED_NMIS: self._excluded_nmis,
                    CONF_NMIS: self._selected_nmis,
                    CONF_NMI_NAMES: self._nmi_names,
                    CONF_CHANNEL_CONFIG: channel_config,
                    CONF_CONSUMPTION_CHANNELS: DEFAULT_CONSUMPTION_CHANNELS,
                    CONF_RETURN_CHANNELS: DEFAULT_RETURN_CHANNELS,
                },
            )

        return self.async_show_form(
            step_id="channels",
            data_schema=data_schema,
            description_placeholders={
                "discovery_result": _discovery_warning(
                    self._channel_errors,
                    self._nmi_names,
                )
            },
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Update credentials after an authentication failure."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]
        if user_input is not None:
            try:
                (
                    _,
                    nmis,
                    nmi_names,
                    excluded_nmis,
                ) = await self.hass.async_add_executor_job(
                    _connect_account,
                    email,
                    user_input[CONF_PASSWORD],
                )
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reauthenticating with SAPN")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_AVAILABLE_NMIS: nmis,
                        CONF_EXCLUDED_NMIS: excluded_nmis,
                        CONF_NMI_NAMES: nmi_names,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={CONF_EMAIL: email},
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    )
                }
            ),
            errors=errors,
        )


class SAPNMeterDataOptionsFlow(OptionsFlow):
    """Handle SA Power Networks Meter Data options."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._client: Any | None = None
        self._available_nmis: list[str] | None = None
        self._excluded_nmis: dict[str, str] | None = None
        self._nmi_names: dict[str, str] | None = None
        self._selected_nmis: list[str] = []
        self._channel_config: dict[str, dict[str, dict[str, str]]] = {}
        self._channel_errors: dict[str, str] = {}
        self._channel_bindings: dict[
            str,
            tuple[str, dict[str, tuple[str, str]]],
        ] = {}

    @override
    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select meters before editing their individual channels."""
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}

        if self._available_nmis is None:
            try:
                (
                    self._client,
                    self._available_nmis,
                    self._nmi_names,
                    self._excluded_nmis,
                ) = await self.hass.async_add_executor_job(
                    _connect_account,
                    self.config_entry.data[CONF_EMAIL],
                    self.config_entry.data[CONF_PASSWORD],
                )
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error refreshing SAPN NMIs")
                errors["base"] = "unknown"

        available_nmis = (
            self._available_nmis
            if self._available_nmis is not None
            else [
                str(nmi)
                for nmi in self.config_entry.data.get(
                    CONF_AVAILABLE_NMIS,
                    self.config_entry.data[CONF_NMIS],
                )
            ]
        )
        nmi_names = (
            self._nmi_names
            if self._nmi_names is not None
            else {
                str(nmi): str(name)
                for nmi, name in current.get(CONF_NMI_NAMES, {}).items()
            }
        )
        for nmi in available_nmis:
            nmi_names.setdefault(nmi, nmi)
        self._nmi_names = nmi_names
        excluded_nmis = (
            self._excluded_nmis
            if self._excluded_nmis is not None
            else {
                str(nmi): str(meter_type)
                for nmi, meter_type in current.get(CONF_EXCLUDED_NMIS, {}).items()
            }
        )
        self._excluded_nmis = excluded_nmis

        if user_input is not None:
            if not user_input[CONF_NMIS]:
                errors["base"] = "select_nmi"
            else:
                self._selected_nmis = [str(nmi) for nmi in user_input[CONF_NMIS]]
                discovered: dict[str, tuple[str, ...]] = {}
                if self._client is not None:
                    try:
                        (
                            discovered,
                            self._channel_errors,
                        ) = await self.hass.async_add_executor_job(
                            _discover_channels,
                            self._client,
                            self._selected_nmis,
                        )
                    except InvalidAuthError:
                        errors["base"] = "invalid_auth"
                    except Exception:
                        _LOGGER.exception("Unexpected error discovering SAPN channels")
                        errors["base"] = "channel_discovery"
                if not errors:
                    self._channel_config = merge_channel_config(
                        self._selected_nmis,
                        discovered,
                        current.get(CONF_CHANNEL_CONFIG, {}),
                        consumption_patterns=current.get(
                            CONF_CONSUMPTION_CHANNELS,
                            DEFAULT_CONSUMPTION_CHANNELS,
                        ),
                        return_patterns=current.get(
                            CONF_RETURN_CHANNELS,
                            DEFAULT_RETURN_CHANNELS,
                        ),
                    )
                    return await self.async_step_channels()

        return self.async_show_form(
            step_id="init",
            data_schema=_meter_schema(
                available_nmis,
                nmi_names,
                current,
            ),
            errors=errors,
            description_placeholders={
                "excluded_meters": _excluded_meter_summary(
                    excluded_nmis,
                    nmi_names,
                )
            },
        )

    async def async_step_channels(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Rename and classify the selected meters' channels."""
        current = {**self.config_entry.data, **self.config_entry.options}
        nmi_names = self._nmi_names or {}
        data_schema, self._channel_bindings = _channel_schema(
            self._selected_nmis,
            nmi_names,
            self._channel_config,
        )
        if user_input is not None:
            channel_config = _channel_form_result(
                user_input,
                self._channel_bindings,
                self._channel_config,
            )
            return self.async_create_entry(
                title="",
                data={
                    CONF_NMIS: self._selected_nmis,
                    CONF_NMI_NAMES: nmi_names,
                    CONF_EXCLUDED_NMIS: self._excluded_nmis or {},
                    CONF_CHANNEL_CONFIG: channel_config,
                    CONF_CONSUMPTION_CHANNELS: current.get(
                        CONF_CONSUMPTION_CHANNELS,
                        DEFAULT_CONSUMPTION_CHANNELS,
                    ),
                    CONF_RETURN_CHANNELS: current.get(
                        CONF_RETURN_CHANNELS,
                        DEFAULT_RETURN_CHANNELS,
                    ),
                },
            )

        return self.async_show_form(
            step_id="channels",
            data_schema=data_schema,
            description_placeholders={
                "discovery_result": _discovery_warning(
                    self._channel_errors,
                    nmi_names,
                )
            },
        )
