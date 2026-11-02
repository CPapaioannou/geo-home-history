"""Exercise the importer against a real, isolated Home Assistant recorder."""
import asyncio
import logging
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.core import HomeAssistant
from homeassistant import loader
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.setup import async_setup_component

from custom_components.geo_home.history import GeoHistoryImporter
from custom_components.geo_home import history as history_module


async def main():
    with TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        try:
            loader.async_setup(hass)
            config = {
                "homeassistant": {"latitude": 51, "longitude": 0, "elevation": 0,
                                  "unit_system": "metric", "time_zone": "Europe/London",
                                  "country": "GB"},
                "recorder": {"db_url": f"sqlite:///{directory}/recorder.db"},
            }
            assert await async_from_config_dict(config, hass) is hass
            await hass.async_start()
            assert await async_setup_component(hass, "recorder", config)
            await hass.async_block_till_done()
            await get_instance(hass).async_block_till_done()
            start = int(datetime(2026, 10, 31, tzinfo=UTC).timestamp())
            payload = [{"startTimestamp": start + i * 900, "readings": [{
                "energyType": "IMPORT", "tierType": "VARIABLE", "duration": 900,
                "energyWattHours": 100,
            }]} for i in range(4)]
            hub = SimpleNamespace(deviceId="smoke-meter", async_get_history=AsyncMock(return_value=payload))
            importer = GeoHistoryImporter(hass, hub, "smoke")
            # Fixed origin keeps this test independent of the CI runner's date.
            importer.data = {"system_id": "smoke-meter", "origin": "2026-10-31",
                             "next_date": "2026-10-31", "hours": {}}
            statistic_id = "geo_home:smoke_meter_electricity_import"

            async def recorded(expected_sum):
                # The recorder thread can already have taken a job off its queue
                # while its transaction is still committing. Wait for visibility.
                for _ in range(40):
                    await get_instance(hass).async_block_till_done()
                    result = await get_instance(hass).async_add_executor_job(
                        get_last_statistics, hass, 2, statistic_id, True, {"sum", "state"}
                    )
                    found = result.get(statistic_id, [])
                    if found and found[0]["sum"] == expected_sum:
                        return found
                    await asyncio.sleep(0.25)
                raise AssertionError(f"Recorder did not expose expected sum: {result}")

            clock = SimpleNamespace(utcnow=lambda: datetime(2026, 11, 2, 10, tzinfo=UTC))
            with patch.object(history_module, "dt_util", clock):
                await importer._async_refresh()
            assert len(importer.data["hours"]) == 1, importer.data
            rows = await recorded(0.4)
            assert len(rows) == 2, rows
            # Replay is a replacement at the same timestamp, not an extra delta.
            importer.data["next_date"] = "2026-10-31"
            with patch.object(history_module, "dt_util", clock):
                await importer._async_refresh()
            await recorded(0.4)
            for row in payload:
                row["readings"][0]["energyWattHours"] = 200
            importer.data["next_date"] = "2026-10-31"
            with patch.object(history_module, "dt_util", clock):
                await importer._async_refresh()
            await recorded(0.8)
            print("Real HA recorder: baseline, hourly sum, replay and correction verified")
        finally:
            await hass.async_stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(asyncio.wait_for(main(), timeout=90))
