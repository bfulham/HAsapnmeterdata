from homeassistant import config_entries, core
from .const import DOMAIN
import voluptuous as vol
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_ID, CONF_NAME
import homeassistant.helpers.config_validation as cv
from typing import Any, Dict, Optional
from homeassistant.helpers.entity_registry import (
    async_entries_for_config_entry,
    async_get,
)
import socket
import sapnmeterdata

class LoginError(Exception):
    """Raised when login fails."""
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)
    pass

def validate_host(email: cv.string, password: cv.string, hass: core.HomeAssistant):
    """Validates a GitHub access token.
    Raises a ValueError if the auth token is invalid.
    """
    try:
        sapnmeterdata.login(email, password)
    except LoginError:
        raise ConnectionError("Invalid email or password")
    except Exception as e:
        raise ConnectionError(f"An unknown error occurred: {e}")
    return True

class SAPNMeterDataConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    data: Optional[Dict[str, Any]]
    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None):
        """Invoked when a user initiates a flow via the user interface."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                test = validate_host(user_input[CONF_EMAIL], user_input[CONF_PASSWORD], self.hass)
            except ConnectionError as e:
                errors["base"] = str(e)
            if not errors:
                # Input is valid, set data.
                self.data = user_input
                # User is done adding repos, create the config entry.
                return self.async_create_entry(title="SA Power Networks Meter Data", data=self.data)
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_ID): cv.positive_int,
            }), errors=errors
        )
    

class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handles options flow for the component."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Manage the options for the custom component."""
        errors: Dict[str, str] = {}
        # Grab all configured repos from the entity registry so we can populate the
        # multi-select dropdown that will allow a user to remove a repo.
        entity_registry = async_get(self.hass)
        entries = async_entries_for_config_entry(
            entity_registry, self.config_entry.entry_id
        )

        if user_input is not None:
            updated_config = dict(self.config_entry.data)

            updated_config[CONF_NAME] = user_input.get(CONF_NAME, updated_config.get(CONF_NAME))
            updated_config[CONF_EMAIL] = user_input.get(CONF_EMAIL, updated_config.get(CONF_EMAIL))
            updated_config[CONF_PASSWORD] = user_input.get(CONF_PASSWORD, updated_config.get(CONF_PASSWORD))
            updated_config[CONF_ID] = user_input.get(CONF_ID, updated_config.get(CONF_ID))

            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=updated_config,
                title=self.config_entry.title,
            )
            
        

        options_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self.config_entry.data.get(CONF_NAME)): cv.string,
                vol.Required(CONF_EMAIL, default=self.config_entry.data.get(CONF_EMAIL)): cv.string,
                vol.Required(CONF_PASSWORD, default=self.config_entry.data.get(CONF_PASSWORD)): cv.string,
                vol.Required(CONF_ID, default=self.config_entry.data.get(CONF_ID)): cv.integer,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=options_schema, errors=errors
        )