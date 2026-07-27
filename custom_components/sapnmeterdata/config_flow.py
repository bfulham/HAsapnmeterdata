"""Config flow for SA Power Networks Meter Data."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
from homeassistant.helpers import selector

from .const import (
    CONF_AVAILABLE_NMIS,
    CONF_CONSUMPTION_CHANNELS,
    CONF_NMI_NAMES,
    CONF_NMIS,
    CONF_RETURN_CHANNELS,
    DEFAULT_CONSUMPTION_CHANNELS,
    DEFAULT_RETURN_CHANNELS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class InvalidAuthError(Exception):
    """Credentials were rejected by the SAPN portal."""


class CannotConnectError(Exception):
    """The SAPN portal could not be reached or queried."""


def _discover_nmis(
    email: str,
    password: str,
) -> tuple[list[str], dict[str, str]]:
    """Validate credentials and return NMIs with SAPN's friendly names."""
    # This function always runs in Home Assistant's executor. Keeping the
    # import here avoids importing pandas and the NEM12 parser on HA's event
    # loop merely because the user opened the config flow.
    from sapnmeterdata import AuthError, FetchError, LoginError, login

    try:
        assignments = login(email, password).getNMIAssignments()
        return (
            [assignment.nmi for assignment in assignments],
            {assignment.nmi: assignment.friendly_name for assignment in assignments},
        )
    except (AuthError, LoginError) as err:
        raise InvalidAuthError from err
    except FetchError as err:
        raise CannotConnectError from err


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


def _meter_schema(
    available_nmis: list[str],
    nmi_names: Mapping[str, str],
    defaults: Mapping[str, Any] | None = None,
) -> vol.Schema:
    """Return the meter and channel mapping schema."""
    defaults = defaults or {}
    selected = [
        nmi for nmi in defaults.get(CONF_NMIS, available_nmis) if nmi in available_nmis
    ]
    if not selected:
        selected = available_nmis
    meter_options = [
        {
            "value": nmi,
            "label": (
                f"{friendly_name} ({nmi})"
                if (friendly_name := nmi_names.get(nmi, nmi)) != nmi
                else nmi
            ),
        }
        for nmi in available_nmis
    ]

    return vol.Schema(
        {
            vol.Required(CONF_NMIS, default=selected): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=meter_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_CONSUMPTION_CHANNELS,
                default=defaults.get(
                    CONF_CONSUMPTION_CHANNELS,
                    DEFAULT_CONSUMPTION_CHANNELS,
                ),
            ): selector.TextSelector(),
            vol.Required(
                CONF_RETURN_CHANNELS,
                default=defaults.get(
                    CONF_RETURN_CHANNELS,
                    DEFAULT_RETURN_CHANNELS,
                ),
            ): selector.TextSelector(),
        }
    )


class SAPNMeterDataConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an SA Power Networks Meter Data config flow."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        self._email = ""
        self._password = ""
        self._available_nmis: list[str] = []
        self._nmi_names: dict[str, str] = {}

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
                    self._available_nmis,
                    self._nmi_names,
                ) = await self.hass.async_add_executor_job(
                    _discover_nmis, self._email, self._password
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
                    errors["base"] = "no_nmis"
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
        """Select NMIs and map channel directions."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_NMIS]:
                errors["base"] = "select_nmi"
            else:
                return self.async_create_entry(
                    title=self._email,
                    data={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        CONF_AVAILABLE_NMIS: self._available_nmis,
                        CONF_NMIS: user_input[CONF_NMIS],
                        CONF_NMI_NAMES: self._nmi_names,
                        CONF_CONSUMPTION_CHANNELS: user_input[
                            CONF_CONSUMPTION_CHANNELS
                        ],
                        CONF_RETURN_CHANNELS: user_input[CONF_RETURN_CHANNELS],
                    },
                )

        return self.async_show_form(
            step_id="meters",
            data_schema=_meter_schema(
                self._available_nmis,
                self._nmi_names,
            ),
            errors=errors,
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
                nmis, nmi_names = await self.hass.async_add_executor_job(
                    _discover_nmis,
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
        self._available_nmis: list[str] | None = None
        self._nmi_names: dict[str, str] | None = None

    @override
    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Change selected NMIs and channel patterns."""
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}

        if self._available_nmis is None:
            try:
                (
                    self._available_nmis,
                    self._nmi_names,
                ) = await self.hass.async_add_executor_job(
                    _discover_nmis,
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

        available_nmis = self._available_nmis or [
            str(nmi)
            for nmi in self.config_entry.data.get(
                CONF_AVAILABLE_NMIS,
                self.config_entry.data[CONF_NMIS],
            )
        ]
        nmi_names = self._nmi_names or {
            str(nmi): str(name) for nmi, name in current.get(CONF_NMI_NAMES, {}).items()
        }
        for nmi in available_nmis:
            nmi_names.setdefault(nmi, nmi)

        if user_input is not None:
            if not user_input[CONF_NMIS]:
                errors["base"] = "select_nmi"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **user_input,
                        CONF_NMI_NAMES: nmi_names,
                    },
                )

        return self.async_show_form(
            step_id="init",
            data_schema=_meter_schema(
                available_nmis,
                nmi_names,
                current,
            ),
            errors=errors,
        )
