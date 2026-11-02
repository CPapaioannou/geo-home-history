"""The Geo Home integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .geohome import GeoHomeHub
from .history import GeoHistoryImporter

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


class GeoHomeCoordinator(DataUpdateCoordinator):
    """Coordinator for Geo Home."""
    def __init__(self, hass, hub):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="geo home sensor",
            update_interval=timedelta(seconds=120),
        )
        self.hub = hub

    async def _async_update_data(self):
        async with asyncio.timeout(30):
            res = await self.hub.get_device_data()
            if not res:
                raise UpdateFailed("Failed to fetch data from GeoHome API")
            return datetime.now()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Geo Home from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    username = entry.data.get("username")
    password = entry.data.get("password")
    system_name = entry.data.get("system_name")

    hub = GeoHomeHub(username, password, system_name, hass)
    coordinator = GeoHomeCoordinator(hass, hub)

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    history = GeoHistoryImporter(hass, hub, entry.entry_id)
    coordinator.history = history

    @callback
    def refresh_history(_now=None):
        entry.async_create_background_task(
            hass, history.async_refresh(), "Geo timestamped history import"
        )

    entry.async_on_unload(
        async_track_time_interval(hass, refresh_history, timedelta(minutes=15))
    )
    refresh_history()

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if DOMAIN in hass.data:
            hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
