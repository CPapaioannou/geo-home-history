# Geo Home

Based on [mmillmor/geo_home](https://github.com/mmillmor/geo_home), the original
Geo Home integration by [mmillmor](https://github.com/mmillmor) and its
contributors. This independently maintained fork adds timestamped electricity
history for Home Assistant's Energy dashboard. The original project history is
retained, and the code remains under the [Apache License 2.0](LICENSE).

## Timestamped energy history fork (beta)

This fork adds **Geo electricity — timestamped history**, a separate external
energy statistic. Existing entities, entity IDs, live polling and Energy dashboard
preferences are retained. Home Assistant **2026.4 or later** is required.

### What it fixes

The upstream integration records changes to Geo's current kWh counter when they
arrive. A delayed update can place consumption in a different hour from the smart
plugs, producing alternating over-reported and untracked consumption. This fork
reads `/epochservice/v1/system/{systemId}/readings` with the existing connection,
and assigns quarter-hour import energy to its original UTC hour.

### Import behaviour

- Initial import covers today and the previous two UTC dates.
- Refreshes every 15 minutes; only completed hours with all four quarter-hours
  are imported. The newest hour may appear later if Geo has not published it yet.
- Rechecks the last three UTC dates for late or corrected readings. Missing
  hours stay missing; they are never treated as measured zero consumption.
- A private HA-managed cache stores the validated hourly Wh ledger and catch-up
  cursor, not credentials. After downtime, requests catch up in batches of at most
  seven dates per refresh. It does not estimate consumption during missing data.
- Duplicate intervals are deduplicated. Conflicting duplicates and unsupported
  interval formats reject the refresh. Corrections recompute subsequent sums.
- A zero baseline is inserted one hour before the initial import window so the
  first imported hour has a correct delta. It is an accounting baseline, not a
  meter reading. Cumulative totals start at that window, not at meter installation.
- Import failures retain existing statistics and retry. They do not fail the live
  coordinator refresh. History requests reuse the existing token and are serialized
  with live requests; no second login/account is created.
- Missing/corrected readings older than the three-date refresh window are not
  revisited automatically unless they fall in an outage catch-up window.

The statistic ID is `geo_home:<system_uuid_with_underscores>_electricity_import`.
It is a recorder statistic, **not a sensor entity**; select it in Energy settings.
Each meter has a separate stable ID. Never add both the old counter and the new
statistic as simultaneous grid consumption sources: that would double-count.

### Installation and validation

1. Keep a copy of the currently installed `custom_components/geo_home` directory.
2. In HACS, open **Custom repositories**, add
   `https://github.com/CPapaioannou/geo-home-history` with type **Integration**,
   and download **Geo Home — Timestamped Energy History**. For this beta,
   enable pre-release versions and select `v1.17.0-beta.1`.
   When migrating from the original Geo Home repository, remove its HACS
   download tracking so it cannot overwrite this version, then download this
   repository before restarting. The integration domain remains `geo_home`.
   Do not delete the configured Geo integration or its entities.
3. Restart Home Assistant to load the new Python code. Existing credentials are
   reused. Allow the initial import and recorder queue to finish.
4. Confirm that **Geo electricity — timestamped history** is available in the
   Energy statistic selector, and compare completed hours with Geo's app.
5. Only after validation, replace the existing grid consumption statistic with
   the new statistic in Energy settings. Keep the existing cost source if desired.
   Earlier history remains stored but is not automatically combined into the new
   source. No dashboard source is changed automatically by this integration.

To roll back: first reselect the original grid consumption source in Energy
settings, restore the previous integration files/release (or download the original
upstream version through HACS), then restart HA.
The original sensors and their history were never rewritten. The separate external
statistics and cache remain stored; rollback does not delete them.

### Tests and limits

Run `python -m unittest discover -s tests -v`. Offline tests cover parsing,
deduplication, corrections, restart replay, incomplete hours, DST and failures.
Importer boundary tests use HA storage/recorder stubs. CI also tests imports,
duplicate replay and corrections against an isolated Home Assistant recorder;
in-instance verification is a separate required step before changing the main
Energy source. CI also runs HACS validation and hassfest.

The full cached ledger is reimported so corrected cumulative sums stay consistent.
This favours correctness for a household meter; import work grows with history.
Geo's endpoint is unofficial and may change. Only 900-second electricity IMPORT
readings are currently supported; export and historical gas/cost imports are out of
scope. Upstream live-connection behaviour is otherwise unchanged.

---

Integrate with Geo Home smart meters in Home Assistant

## Instructions

To collect Geo Home smart meter data, you must have a WiFi connected
smart meter from Geo Together. If you have a smart meter that is not
WiFi connected by default, you can buy a WiFi module from 
Geo Together at https://geotogether.com/product-category/accessories/.

Once you have a WiFi connected smart meter, create an account on the Geo Home app. The username and password for this account are used to
connect the Geo Home integration.

After downloading the Geo Home integration into HACS, remember to install it by navigating to Settings -> Devices & services -> Add integration, then
select "Geo Home". You will be prompted for
a username and password. Enter the ones you created from the app. This will create Gas and Electricity sensors, tracking the lifetime
energy usage of your meters. If you get a 401 error, you may have entered the wrong credentials, or your account may be blocked for remote access. Unfortunately there is nothing we can do to resolve that.

## Energy Dashboard Configuration

To display Electricity in the energy dashboard, add sensor.electricity as your _Electricity Consumed Energy (kWh)_, and set _Use an entity tracking the total costs_ with a cost sensor of sensor.electricity_cost_today

To display Gas in the energy dashboard, add sensor.gas or sensor.gas_m3 as your _Gas usage_, and set _Use an entity tracking the total costs_ with a cost sensor of sensor.gas_cost_today


## Upgrading

If you are upgrading from version 1.2, the units of your cost sensors will have been £/kWh instead of GBP/kWh. You can go to https://my.home-assistant.io/redirect/developer_statistics to correct that
