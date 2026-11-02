"""Config flow for Geo Home integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
from datetime import date

from .const import DOMAIN
from .geohome import GeoHomeHub

_LOGGER = logging.getLogger(__name__)


def options_schema(options: dict = None) -> dict:
    """Return options schema."""
    options = options or {}
    return {
        vol.Required(
            "username",
            default=options.get("username", "admin"),
            description="Geo Home web username",
        ): str,
        vol.Required(
            "password",
            default=options.get("password", "password"),
            description="Geo Home web password",
        ): str,
        vol.Optional(
            "system_name",
            default=options.get("system_name", ""),
            description="Geo Home system name",
        ): str,
    }


def new_options(username: str, password: str, system_name: str="") -> dict:
    """Create a standard options object."""
    return {"username": username, "password": password,"system_name":system_name}


def options_data(user_input: dict) -> dict:
    """Return options dict."""
    return new_options(
        user_input.get("username", ""),
        user_input.get("password", ""),
        user_input.get("system_name", ""),
    )


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    hub = GeoHomeHub(data["username"], data["password"], data["system_name"], hass)

    if not await hub.authenticate():
        raise InvalidAuth

    # If you cannot connect:
    # throw CannotConnect
    # If the authentication is wrong:
    # InvalidAuth

    # Return info that you want to store in the config entry.
    return {"title": "Geo Home"}



class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Geo Home."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HistoryOptionsFlow()


    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:

            try:
                if user_input.get("history_start_date") and date.fromisoformat(user_input["history_start_date"]) > dt_util.utcnow().date():
                    raise ValueError("History start date is in the future")
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                #await self.async_set_unique_id(info.get("title"))
                #self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({**options_schema(),
                vol.Optional("history_start_date"): selector.DateSelector()}), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    config_entry,
                    unique_id=config_entry.unique_id,
                    data={**config_entry.data, **user_input},
                    reason="reconfigure_successful",
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(options_schema(config_entry.data)),
            errors=errors,
        )

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class HistoryOptionsFlow(config_entries.OptionsFlow):
    """Choose an explicit starting date without changing account credentials."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            selected = user_input.get("history_start_date")
            if selected and date.fromisoformat(selected) > dt_util.utcnow().date():
                errors["base"] = "future_date"
            else:
                self._selected_history = selected
                return await self.async_step_confirm()
        field = vol.Optional("history_start_date")
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema({field: selector.DateSelector()}), errors=errors
        )

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            options = {**self.config_entry.options}
            if self._selected_history:
                options["history_start_date"] = self._selected_history
            else:
                options.pop("history_start_date", None)
            return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}),
            description_placeholders={"start_date": self._selected_history or "No additional past history"},
        )
