"""Explicit, administrator-only history import action."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN


@callback
def async_register_history_service(hass):
    if hass.services.has_service(DOMAIN, "import_history"):
        return

    async def import_history(call):
        coordinator = hass.data.get(DOMAIN, {}).get(call.data["entry_id"])
        if coordinator is None:
            raise ServiceValidationError("Select a loaded Geo Home integration")
        try:
            return await coordinator.history.async_backfill(
                call.data["start_date"], call.data["end_date"],
                dry_run=call.data["dry_run"],
            )
        except Exception as err:
            # HTTP exceptions can contain account-specific URLs. Keep them out of UI/log output.
            raise ServiceValidationError(
                f"History import failed ({type(err).__name__}); check the date range and retry"
            ) from None

    async_register_admin_service(
        hass, DOMAIN, "import_history", import_history,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("start_date"): cv.date,
            vol.Required("end_date"): cv.date,
            vol.Optional("dry_run", default=True): cv.boolean,
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
