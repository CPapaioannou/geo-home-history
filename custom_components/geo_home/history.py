"""Import timestamped Geo energy history into a separate recorder series."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
import asyncio

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .history_data import complete_hours, cumulative_rows

_LOGGER = logging.getLogger(__name__)
FORMAT_VERSION = 2


class GeoHistoryImporter:
    """Persist an hourly ledger and explicitly requested historical imports."""

    def __init__(self, hass, hub, entry_id, start_date=None):
        self.hass = hass
        self.hub = hub
        self.store = Store(hass, 1, f"{DOMAIN}.history.{entry_id}")
        self.data = None
        self.running = False
        self.requested_start = date.fromisoformat(start_date) if start_date else None

    @property
    def statistic_id(self):
        meter = self.hub.deviceId.replace("-", "_").lower()
        return f"{DOMAIN}:{meter}_electricity_import"

    async def _async_load(self):
        if self.data is not None:
            return
        now = dt_util.utcnow()
        if self.requested_start and self.requested_start > now.date():
            raise ValueError("History start date is in the future")
        saved = await self.store.async_load()
        if saved is not None and saved.get("system_id") != self.hub.deviceId:
            raise ValueError("Cached history belongs to another meter")
        origin = self.requested_start or now.date()
        self.data = saved or {
            "system_id": self.hub.deviceId,
            "origin": origin.isoformat(),
            "start_hour": int((datetime.combine(origin, time.min, UTC)
                               if self.requested_start else
                               now.replace(minute=0, second=0, microsecond=0)).timestamp()),
            "next_date": origin.isoformat(), "hours": {},
            "format_version": FORMAT_VERSION,
        }
        if self.data.get("format_version", 1) < FORMAT_VERSION:
            self.data = {**self.data, "format_version": FORMAT_VERSION,
                         "rebuild_pending": True}
        if self.requested_start and int(datetime.combine(self.requested_start, time.min, UTC).timestamp()) < int(self._origin().timestamp()):
            self.data = {**self.data, "origin": self.requested_start.isoformat(),
                         "start_hour": int(datetime.combine(self.requested_start, time.min, UTC).timestamp()),
                         "next_date": self.requested_start.isoformat()}

    def _origin(self):
        return datetime.fromtimestamp(self.data.get("start_hour", int(datetime.combine(
            date.fromisoformat(self.data["origin"]), time.min, UTC).timestamp())), UTC)

    async def _async_fetch(self, start, end, now):
        payload = await self.hub.async_get_history(start, end)
        hours = complete_hours(payload, now)
        # Geo may include the first quarter of the following date at its end boundary.
        lower = int(datetime.combine(start, time.min, UTC).timestamp())
        upper = int(datetime.combine(end + timedelta(days=1), time.min, UTC).timestamp())
        return {key: wh for key, wh in hours.items() if lower <= int(key) < upper}

    async def _async_publish(self, candidate):
        origin = datetime.fromtimestamp(candidate.get("start_hour", int(datetime.combine(
            date.fromisoformat(candidate["origin"]), time.min, UTC).timestamp())), UTC)
        rows = cumulative_rows(candidate["hours"], origin)
        # Persist a replayable ledger before changing recorder state. Legacy caches
        # get one ordered clear-and-reimport to remove old placeholder rows.
        await self.store.async_save(candidate)
        self.data = candidate
        if candidate.get("rebuild_pending"):
            cleared = asyncio.Event()
            get_instance(self.hass).async_clear_statistics(
                [self.statistic_id],
                on_done=lambda: self.hass.loop.call_soon_threadsafe(cleared.set),
            )
            await asyncio.wait_for(cleared.wait(), timeout=60)
        if rows:
            metadata = {
                "statistic_id": self.statistic_id, "source": DOMAIN,
                "name": "Geo electricity — timestamped history",
                "unit_of_measurement": "kWh", "unit_class": "energy",
                "mean_type": StatisticMeanType.NONE, "has_sum": True,
            }
            async_add_external_statistics(self.hass, metadata, rows)
        # Both operations use the same recorder queue, so the clear precedes the import.
        # A saved pending flag makes an interrupted migration safe to replay.
        if candidate.get("rebuild_pending"):
            await get_instance(self.hass).async_block_till_done()
            clean = {**candidate, "rebuild_pending": False}
            await self.store.async_save(clean)
            self.data = clean
        _LOGGER.debug("Queued %s completed Geo electricity hours", len(rows))

    async def async_refresh(self, _now=None):
        if self.running:
            return
        self.running = True
        try:
            await self._async_refresh()
        except Exception as err:
            _LOGGER.warning("Geo history refresh failed (%s); existing readings retained",
                            type(err).__name__)
        finally:
            self.running = False

    async def _async_refresh(self):
        await self._async_load()
        now = dt_util.utcnow()
        today = now.date()
        origin_date = date.fromisoformat(self.data["origin"])
        start = max(origin_date, date.fromisoformat(self.data["next_date"]))
        end = min(today, start + timedelta(days=6))
        hours = await self._async_fetch(start, end, now)
        start_hour = int(self._origin().timestamp())
        hours = {key: wh for key, wh in hours.items() if int(key) >= start_hour}
        candidate = {**self.data, "hours": {**self.data["hours"], **hours},
                     "next_date": ((end + timedelta(days=1)) if end < today else
                                   max(origin_date, today - timedelta(days=2))).isoformat()}
        await self._async_publish(candidate)

    async def async_backfill(self, start, end, *, dry_run=True):
        """Preview or import a requested UTC date range, at most 31 dates per call."""
        if start > end or end > dt_util.utcnow().date() or (end - start).days > 30:
            raise ValueError("Choose an ordered range of at most 31 UTC dates, ending today or earlier")
        if self.running:
            raise ValueError("A history refresh is in progress; retry shortly")
        self.running = True
        try:
            await self._async_load()
            now = dt_util.utcnow()
            hours = {}
            for offset in range(0, (end - start).days + 1, 7):
                batch_start = start + timedelta(days=offset)
                hours.update(await self._async_fetch(batch_start, min(end, batch_start + timedelta(days=6)), now))
            result = {"dry_run": dry_run, "start_date": start.isoformat(),
                      "end_date": end.isoformat(), "complete_hours": len(hours),
                      "energy_kwh": sum(hours.values()) / 1000}
            if dry_run or not hours:
                return result
            first = min(int(key) for key in hours)
            origin = min(int(self._origin().timestamp()), first)
            # Only extend the origin to a real complete reading, never to a placeholder.
            candidate = {**self.data, "origin": datetime.fromtimestamp(origin, UTC).date().isoformat(),
                         "start_hour": origin, "hours": {**self.data["hours"], **hours}}
            await self._async_publish(candidate)
            return result
        finally:
            self.running = False
