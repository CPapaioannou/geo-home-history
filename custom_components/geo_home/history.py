"""Import timestamped Geo energy history into a separate recorder series."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .history_data import complete_hours, cumulative_rows

_LOGGER = logging.getLogger(__name__)


class GeoHistoryImporter:
    """Persist our own hourly ledger; never modify existing sensor statistics."""

    def __init__(self, hass, hub, entry_id):
        self.hass = hass
        self.hub = hub
        self.store = Store(hass, 1, f"{DOMAIN}.history.{entry_id}")
        self.data = None
        self.running = False

    async def async_refresh(self, _now=None):
        """Fetch at most seven dates per refresh; retry failures on the next tick."""
        if self.running:
            return
        self.running = True
        try:
            await self._async_refresh()
        except Exception as err:
            # Exception text may include authenticated request details. Log type only.
            _LOGGER.warning("Geo history refresh failed (%s); existing readings retained",
                            type(err).__name__)
        finally:
            self.running = False

    async def _async_refresh(self):
        now = dt_util.utcnow()
        today = now.date()
        if self.data is None:
            saved = await self.store.async_load()
            if saved is not None and saved.get("system_id") != self.hub.deviceId:
                raise ValueError("Cached history belongs to another meter")
            self.data = saved or {
                "system_id": self.hub.deviceId,
                "origin": (today - timedelta(days=2)).isoformat(),
                "next_date": (today - timedelta(days=2)).isoformat(),
                "hours": {},
            }
        origin_date = date.fromisoformat(self.data["origin"])
        start = max(origin_date, date.fromisoformat(self.data["next_date"]))
        end = min(today, start + timedelta(days=6))
        payload = await self.hub.async_get_history(start, end)
        hours = complete_hours(payload, now)
        lower = int(datetime.combine(start, time.min, UTC).timestamp())
        upper = int(datetime.combine(end + timedelta(days=1), time.min, UTC).timestamp())
        if any(not lower <= int(key) < upper for key in hours):
            raise ValueError("History response contains out-of-window readings")
        ledger = {**self.data["hours"], **hours}
        origin = datetime.combine(origin_date, time.min, UTC)
        rows = cumulative_rows(ledger, origin)
        candidate = {**self.data, "hours": ledger,
                     "next_date": ((end + timedelta(days=1)) if end < today else
                                   max(origin_date, today - timedelta(days=2))).isoformat()}
        # Save first: an interrupted recorder import can be replayed idempotently.
        await self.store.async_save(candidate)
        self.data = candidate
        if not ledger:
            return
        meter = self.hub.deviceId.replace("-", "_").lower()
        metadata = {
            "statistic_id": f"{DOMAIN}:{meter}_electricity_import",
            "source": DOMAIN,
            "name": "Geo electricity — timestamped history",
            "unit_of_measurement": "kWh",
            "unit_class": "energy",
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
        }
        async_add_external_statistics(self.hass, metadata, rows)
        _LOGGER.debug("Queued %s completed Geo electricity hours", len(ledger))
