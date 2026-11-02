"""Offline tests of interval validation and importer recovery contracts."""
import asyncio
import copy
from datetime import UTC, date, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components/geo_home"
package = types.ModuleType("geo_test")
package.__path__ = [str(PKG)]
sys.modules["geo_test"] = package


def load(name):
    spec = importlib.util.spec_from_file_location("geo_test." + name, PKG / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


data = load("history_data")
START = datetime(2026, 10, 31, tzinfo=UTC)


def payload(wh=100, start=START):
    return [{"startTimestamp": int((start + timedelta(minutes=15 * i)).timestamp()),
             "readings": [{"energyType": "IMPORT", "tierType": "VARIABLE",
                           "duration": 900, "energyWattHours": wh}]} for i in range(4)]


class ValidationTests(unittest.TestCase):
    def test_complete_hour_and_units(self):
        hours = data.complete_hours(payload(), START + timedelta(hours=1))
        rows = data.cumulative_rows(hours, START)
        self.assertEqual([r["sum"] for r in rows], [0, 0.4])
        self.assertEqual(rows[1]["start"], START)

    def test_incomplete_hour_not_imported(self):
        self.assertEqual(data.complete_hours(payload()[:3], START + timedelta(hours=1)), {})

    def test_current_hour_not_imported(self):
        self.assertEqual(data.complete_hours(payload(), START + timedelta(minutes=59)), {})

    def test_duplicate_intervals_do_not_double_count(self):
        self.assertEqual(data.complete_hours(payload() * 2, START + timedelta(hours=1)),
                         {str(int(START.timestamp())): 400})

    def test_conflicting_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            data.complete_hours(payload() + payload(200), START + timedelta(hours=1))

    def test_tariff_tiers_and_gas(self):
        rows = payload()
        for row in rows:
            row["readings"].extend([
                {"energyType": "IMPORT", "tierType": "OFF_PEAK", "duration": 900,
                 "energyWattHours": 25},
                {"energyType": "GAS_ENERGY", "energyWattHours": 999},
            ])
        self.assertEqual(list(data.complete_hours(rows, START + timedelta(hours=1)).values()), [500])

    def test_malformed_readings_rejected(self):
        for field, value in [("duration", 1800), ("energyWattHours", -1),
                             ("energyWattHours", True), ("energyWattHours", float("nan"))]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                rows = payload()
                rows[0]["readings"][0][field] = value
                data.complete_hours(rows, START + timedelta(hours=1))

    def test_bad_timestamp_rejected(self):
        for value in [None, "yesterday", float("inf"), int(START.timestamp()) + 1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                rows = payload()
                rows[0]["startTimestamp"] = value
                data.complete_hours(rows, START + timedelta(hours=1))

    def test_correction_recalculates_later_sums(self):
        hours = {str(int(START.timestamp())): 400,
                 str(int(START.timestamp()) + 3600): 200}
        first = data.cumulative_rows(hours, START)
        hours[str(int(START.timestamp()))] = 500
        second = data.cumulative_rows(hours, START)
        self.assertEqual(first[-1]["sum"], 0.6)
        self.assertEqual(second[-1]["sum"], 0.7)

    def test_dst_repeated_local_hour_remains_two_utc_hours(self):
        start = datetime(2026, 10, 25, tzinfo=UTC)
        rows = payload(start=start) + payload(start=start + timedelta(hours=1))
        self.assertEqual(len(data.complete_hours(rows, start + timedelta(hours=2))), 2)

    def test_zero_usage_is_a_valid_completed_hour(self):
        self.assertEqual(list(data.complete_hours(payload(0), START + timedelta(hours=1)).values()), [0])


class FakeStore:
    saved = None
    fail_save = False

    def __init__(self, *args):
        pass

    async def async_load(self):
        return copy.deepcopy(self.saved)

    async def async_save(self, value):
        if self.fail_save:
            raise OSError("disk failure")
        FakeStore.saved = copy.deepcopy(value)


def fake_module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


# Boundary stubs test scheduling/storage/import contracts, not HA itself.
fake_module("homeassistant")
fake_module("homeassistant.components")
fake_module("homeassistant.components.recorder")
fake_module("homeassistant.components.recorder.models", StatisticMeanType=types.SimpleNamespace(NONE=0))
IMPORT = Mock()
fake_module("homeassistant.components.recorder.statistics", async_add_external_statistics=IMPORT)
fake_module("homeassistant.helpers")
fake_module("homeassistant.helpers.storage", Store=FakeStore)
NOW = datetime(2026, 11, 2, 10, 15, tzinfo=UTC)
fake_module("homeassistant.util", dt=types.SimpleNamespace(utcnow=lambda: NOW))
history = load("history")

# Isolate the synchronous HTTP boundary; tests never log in or contact Geo.
fake_module("homeassistant.core", HomeAssistant=object)
HTTP_GET = Mock()
fake_module("requests", get=HTTP_GET)
fake_module("requests.auth", HTTPBasicAuth=object)
hub_module = load("geohome")


class ImporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeStore.saved = None
        FakeStore.fail_save = False
        IMPORT.reset_mock()
        self.hub = types.SimpleNamespace(deviceId="test-meter", async_get_history=AsyncMock(return_value=payload()))
        self.importer = history.GeoHistoryImporter(object(), self.hub, "entry")

    async def test_repeat_and_restart_are_idempotent(self):
        await self.importer.async_refresh()
        expected = copy.deepcopy(IMPORT.call_args.args[2])
        await self.importer.async_refresh()
        self.assertEqual(IMPORT.call_args.args[2], expected)
        restarted = history.GeoHistoryImporter(object(), self.hub, "entry")
        await restarted.async_refresh()
        self.assertEqual(IMPORT.call_args.args[2], expected)
        self.assertEqual(IMPORT.call_args.args[1]["statistic_id"], "geo_home:test_meter_electricity_import")

    async def test_failure_retains_cache_and_retries(self):
        await self.importer.async_refresh()
        before = copy.deepcopy(FakeStore.saved)
        self.hub.async_get_history.side_effect = TimeoutError()
        await self.importer.async_refresh()
        self.assertEqual(FakeStore.saved, before)
        self.assertFalse(self.importer.running)
        self.hub.async_get_history.side_effect = None
        await self.importer.async_refresh()
        self.assertEqual(IMPORT.call_count, 2)

    async def test_save_failure_does_not_import(self):
        FakeStore.fail_save = True
        await self.importer.async_refresh()
        IMPORT.assert_not_called()

    async def test_incomplete_refresh_preserves_complete_cached_hour(self):
        await self.importer.async_refresh()
        self.hub.async_get_history.return_value = payload()[:2]
        await self.importer.async_refresh()
        self.assertEqual(len(FakeStore.saved["hours"]), 1)

    async def test_late_correction_replaces_hour(self):
        await self.importer.async_refresh()
        self.hub.async_get_history.return_value = payload(200)
        await self.importer.async_refresh()
        self.assertEqual(IMPORT.call_args.args[2][-1]["sum"], 0.8)

    async def test_downtime_catchup_is_bounded(self):
        FakeStore.saved = {"system_id": "test-meter", "origin": "2026-10-01",
                           "next_date": "2026-10-01", "hours": {}}
        self.hub.async_get_history.return_value = []
        await self.importer.async_refresh()
        self.hub.async_get_history.assert_awaited_once_with(date(2026, 10, 1), date(2026, 10, 7))
        self.assertEqual(FakeStore.saved["next_date"], "2026-10-08")

    async def test_changed_meter_rejects_cached_data(self):
        FakeStore.saved = {"system_id": "another-meter"}
        await self.importer.async_refresh()
        IMPORT.assert_not_called()
        self.hub.async_get_history.assert_not_awaited()

    async def test_overlapping_refresh_is_skipped(self):
        self.importer.running = True
        await self.importer.async_refresh()
        self.hub.async_get_history.assert_not_awaited()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = hub_module.GeoHomeHub("unused", "unused", None, object())
        self.hub.deviceId = "meter"
        self.hub.accessToken = "test-token"
        HTTP_GET.reset_mock()
        response = Mock(status_code=200)
        response.json.return_value = payload()
        HTTP_GET.return_value = response

    def test_history_reuses_session_and_passes_dates_and_timeout(self):
        rows = self.hub._get_history(date(2026, 10, 31), date(2026, 11, 2))
        self.assertEqual(rows, payload())
        self.assertEqual(HTTP_GET.call_args.kwargs["params"],
                         {"startDate": "2026-10-31", "endDate": "2026-11-02"})
        self.assertEqual(HTTP_GET.call_args.kwargs["timeout"], 20)
        self.assertTrue(HTTP_GET.call_args.args[0].endswith("/epochservice/v1/system/meter/readings"))

    def test_no_session_does_not_attempt_competing_login(self):
        self.hub.accessToken = None
        with self.assertRaises(RuntimeError):
            self.hub._get_history(date.today(), date.today())
        HTTP_GET.assert_not_called()

    def test_expired_token_is_left_for_live_refresh_to_renew(self):
        HTTP_GET.return_value.status_code = 401
        HTTP_GET.return_value.raise_for_status.side_effect = RuntimeError("401")
        with self.assertRaises(RuntimeError):
            self.hub._get_history(date.today(), date.today())
        self.assertIsNone(self.hub.accessToken)

    def test_permission_error_does_not_discard_live_token(self):
        HTTP_GET.return_value.status_code = 403
        HTTP_GET.return_value.raise_for_status.side_effect = RuntimeError("403")
        with self.assertRaises(RuntimeError):
            self.hub._get_history(date.today(), date.today())
        self.assertEqual(self.hub.accessToken, "test-token")


if __name__ == "__main__":
    unittest.main()
