"""Exercise first-hour accounting, migration and backfills against real HA."""
import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant import loader
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from custom_components.geo_home import history as history_module
from custom_components.geo_home.history import GeoHistoryImporter
from custom_components.geo_home.config_flow import HistoryOptionsFlow
from custom_components.geo_home.services import async_register_history_service

START = datetime(2026, 10, 31, tzinfo=UTC)
CLOCK = SimpleNamespace(utcnow=lambda: datetime(2026,11,2,12,tzinfo=UTC))


def payload(start, wh):
    return [{"startTimestamp": int(start.timestamp())+i*900, "readings": [{
        "energyType":"IMPORT", "tierType":"VARIABLE", "duration":900,
        "energyWattHours":wh,
    }]} for i in range(4)]


async def main():
    with TemporaryDirectory() as directory:
        hass=HomeAssistant(directory)
        try:
            loader.async_setup(hass)
            config={"homeassistant":{"latitude":51,"longitude":0,"elevation":0,
                    "unit_system":"metric","time_zone":"Europe/London","country":"GB"},
                    "recorder":{"db_url":f"sqlite:///{directory}/recorder.db"}}
            assert await async_from_config_dict(config,hass) is hass
            await hass.async_start()
            assert await async_setup_component(hass,"recorder",config)
            await hass.async_block_till_done()
            await get_instance(hass).async_block_till_done()
            hub=SimpleNamespace(deviceId="smoke-meter",async_get_history=AsyncMock(return_value=payload(START,100)))
            importer=GeoHistoryImporter(hass,hub,"smoke","2026-10-31")
            sid=importer.statistic_id

            async def rows(expected_count, expected_total):
                for _ in range(40):
                    await get_instance(hass).async_block_till_done()
                    result=await get_instance(hass).async_add_executor_job(
                        statistics_during_period,hass,START-timedelta(days=3),
                        START+timedelta(days=3),{sid},"hour",None,{"sum","change","state"})
                    found=result.get(sid,[])
                    if len(found)==expected_count and abs(found[-1]["sum"]-expected_total)<1e-10:
                        return found
                    await asyncio.sleep(0.25)
                raise AssertionError(f"Unexpected recorder data: {result}")

            with patch.object(history_module,"dt_util",CLOCK):
                await importer._async_refresh()
                first=await rows(1,0.4)
                assert first[0]["change"]==0.4,first
                assert first[0]["start"]==START.timestamp(),first
                # Re-import is idempotent; a correction changes exactly its real hour.
                await importer._async_refresh()
                await rows(1,0.4)
                hub.async_get_history.return_value=payload(START,200)
                await importer._async_refresh()
                await rows(1,0.8)
                # Seed a legacy placeholder and cache, then verify migration removes it.
                metadata={"statistic_id":sid,"source":"geo_home","name":"Geo electricity — timestamped history",
                          "unit_of_measurement":"kWh","unit_class":"energy","mean_type":0,"has_sum":True}
                async_add_external_statistics(hass,metadata,[{"start":START-timedelta(days=2,hours=1),"sum":-9.0,"state":0.0}])
                await importer.store.async_save({"system_id":"smoke-meter","origin":"2026-10-31",
                      "next_date":"2026-10-31","hours":{str(int(START.timestamp())):800}})
                importer=GeoHistoryImporter(hass,hub,"smoke")
                await importer._async_refresh()
                await rows(1,0.8)
                earlier=START-timedelta(days=2)
                hub.async_get_history.return_value=payload(earlier,100)+payload(earlier+timedelta(days=1),150)
                before=dict(importer.data["hours"])
                preview=await importer.async_backfill(earlier.date(),date(2026,10,30))
                assert preview["complete_hours"]==2 and importer.data["hours"]==before
                await rows(1,0.8)
                result=await importer.async_backfill(earlier.date(),date(2026,10,30),dry_run=False)
                assert result["energy_kwh"]==1.0,result
                imported=await rows(3,1.8)
                assert imported[0]["start"]==earlier.timestamp(),imported
                assert [round(r["change"],3) for r in imported]==[0.4,0.6,0.8],imported
                # Restart and ordinary refresh retain imported hours and the first delta.
                hub.async_get_history.return_value=payload(START,200)
                importer=GeoHistoryImporter(hass,hub,"smoke")
                await importer._async_refresh()
                replayed=await rows(3,1.8)
                assert [round(r["change"],3) for r in replayed]==[0.4,0.6,0.8],replayed
                # A later correction changes later sums but not preceding hourly usage.
                hub.async_get_history.return_value=payload(START,250)
                await importer._async_refresh()
                corrected=await rows(3,2.0)
                assert [round(r["change"],3) for r in corrected]==[0.4,0.6,1.0],corrected
                # Exercise the published action schema and dry-run default through HA.
                hass.data["geo_home"]={"smoke":SimpleNamespace(history=importer)}
                async_register_history_service(hass)
                response=await hass.services.async_call("geo_home","import_history",{
                    "entry_id":"smoke","start_date":"2026-10-29","end_date":"2026-10-30"},
                    blocking=True,return_response=True)
                assert response["dry_run"] is True,response
                await rows(3,2.0)
            flow=HistoryOptionsFlow()
            flow.hass=hass
            flow.handler="smoke"
            with patch.object(type(hass.config_entries),"async_get_known_entry",
                              return_value=SimpleNamespace(options={"preserved":"value"})):
                form=await flow.async_step_init()
                assert form["step_id"]=="init",form
                confirmation=await flow.async_step_init({"history_start_date":"2026-10-29"})
                assert confirmation["step_id"]=="confirm",confirmation
                saved=await flow.async_step_confirm({})
                assert saved["data"]=={"preserved":"value","history_start_date":"2026-10-29"},saved
                invalid=await flow.async_step_init({"history_start_date":"2999-01-01"})
                assert invalid["errors"]=={"base":"future_date"},invalid
            print("Real HA: first-hour delta, no preceding-day rows, legacy cleanup, preview, backfill, restart, refresh, correction and action verified")
        finally:
            await hass.async_stop()


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(asyncio.wait_for(main(),timeout=90))
