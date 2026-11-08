<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [chart_log.py](../custom_components/climate_advisor/chart_log.py) | ↔ [State Persistence](state-persistence.md) [Thermal Model v3](thermal-model-v3-spec.md) [Predictor v4](predictor-v4-spec.md) [v4 Shadow](v4-shadow-spec.md) -->

# Chart State Log — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Is retention time-based or count-based, and what is the cap? | Time-based: entries older than `max_days` (default 365) are evicted. At ~30-minute cadence that is ~17,520 entries. There is no separate count cap. | [Retention Model](#retention-model) |
| What does `append()` guarantee about entry ordering and eviction timing? | Entries are appended in call order; timestamps are NOT re-sorted. Pruning runs at most once per hour — the buffer may transiently exceed the window between prune passes. | [Append Contract](#append-contract) |
| What does `get_entries()` return when the log is empty or the range produces no matches? | An empty list `[]`. No error is raised. Downsampling buckets that receive zero entries simply produce no output rows. | [Query Contract](#query-contract) |
| How does `get_entries()` support historical navigation? | Pass `before: datetime` to anchor the query window to that point in time instead of the current clock. The window is then `[before − range_days, before)`. Used by `get_chart_data(before_ts=...)` to serve past data windows. | [Query Contract](#query-contract) |
| What downsampling tier applies for each `range_str` value? | `6h`/`12h`/`24h`/`3d` → raw entries. `7d`/`30d` → hourly averages. `1y` → daily summaries. An unrecognised range string defaults to `24h` (1-day raw). | [Downsampling Rules](#downsampling-rules) |
| What fields does a raw entry always carry, and which are optional? | Nine core fields always present: `ts`, `hvac`, `fan`, `indoor`, `outdoor`, `windows_open`, `windows_recommended`, `pred_outdoor`, `pred_indoor`. The `event` field is only present when the marker argument is non-None; `indoor_primary` only on `hvac_action_change` rows written since v0.8.34 (Issue #1234); `comfort_target` only on rows written while the remote-sensor outer loop holds an offset (Issue #1194 d); `indoor_sensors` on every coordinator-written row since 0.9.1 (Issue #1230 R0). |
| What is `indoor_sensors` and which sensors does it cover? | A dict `{entity_id: °F (2 dp) \| null}` of every indoor source - the climate entity's `current_temperature`, `indoor_temp_entity`, `sleep_indoor_temp_entity`, then (0.9.2) every enabled `sensor.*` with `device_class: temperature` on the climate entity's own device (de-duplicated, in that order) - read on every row under the resolver's validation rules and identical whether or not the sleep window is active. `null` = that sensor failed the read (missing, non-numeric, outside 40-110 F, or stale >12 h). | [Indoor Sensors Field](#indoor-sensors-field-issue-1230-r0) |
| How does `indoor_sensors` appear in the 7d/30d/1y views, and does the chart ever see it? | Hourly and daily buckets carry the per-entity mean (2 dp) under the same `indoor_sensors` key. `get_chart_data()` STRIPS it from `state_log` (not converted), so a Celsius frontend never receives a Fahrenheit value; the thermal tools read the raw file. | [Downsampled Entry Schemas](#downsampled-entry-schemas), [Temperature Units](#temperature-units-and-display-conversion-issue-1015) | [Entry Schema](#entry-schema) |
| Which chart-log fields carry humidity and feels-like, where does the humidity come from, and does anything but the chart read them (Issue #1414)? | `humidity` / `feelslike` (indoor, the room whose temperature is `indoor`) and `outdoor_humidity` / `outdoor_feelslike`, each written only when it has a value. RH is % at native resolution; feels-like is internal °F: the NWS heat index only where NWS uses its full regression, outdoors else the NWS wind chill (<= 50 °F, wind > 3 mph). Chart display only: no decision reads them. | [Humidity and Feels-Like Fields](#humidity-and-feels-like-fields-issue-1414) |
| What two fields were added for the Vent bar (Issue #331), and what do they mean? | `fan_running: bool` — fan is physically on (compressor-class running). `nat_vent_active: bool` — nat-vent session is armed (includes paused-between-cycles). Legacy `fan` field is still emitted for back-compat. | [Vent Bar Fields (Issue #331)](#vent-bar-fields-issue-331) |
| How does the frontend decide the Vent bar color for each entry? | Blue if `fan_running`; green if `nat_vent_active || windows_recommended`; else off. Back-compat: if `fan_running` absent, fall back to legacy `fan` field → blue. | [Vent Bar Fields (Issue #331)](#vent-bar-fields-issue-331) |
| What does the HVAC bar show after Issue #331? | Compressor-only: `heating` (red) / `cooling` (blue). `fan_only` and `off` produce no bar. | [Vent Bar Fields (Issue #331)](#vent-bar-fields-issue-331) |
| How is `pred_outdoor` populated in each chart log entry? | The hourly forecast temperature for the current local hour, extracted by `_extract_current_hour_forecast_temp()` from `_hourly_forecast_temps`, which is already normalised to internal °F at its source (Issue #1015). `null` when hourly forecast has no entry for the current hour. | [Coordinator Chart Log Wiring](#coordinator-chart-log-wiring) |
| What unit is each temperature field stored in a chart-log entry? | Every temperature field is internal °F regardless of the install's display unit: `indoor`, `outdoor`, `setpoint`, `nat_vent_target`, `lower`, `upper`, `hold_lower`, `hold_upper`, `pred_indoor`, and (entries written since 0.7.82) `pred_outdoor`. Entries written before 0.7.82 on Celsius installs hold `pred_outdoor` in the weather provider's native unit. | [Temperature Units and Display Conversion](#temperature-units-and-display-conversion-issue-1015) |
| How is the past band drawn between rows, and where does the Away strip start and end? | Each past row holds flat until the next row (Issue #1425, 0.9.7+), so a one-row Away is a block, not a spike; the Away strip runs from its row to the next, and rows are written at each occupancy moment. | [Drawing the Past Band](#drawing-the-past-band-and-the-away-strip-issue-1425) |
| How does a past time-of-use bank draw on the chart history? | As the hold (Issue #1406, 0.9.6+): rows where the chart drew a band other than the band schedule carry `hold_lower`/`hold_upper` (internal °F, written only then); the history draws them, else `lower`/`upper`, which keep their meaning for the replay tools. | [Hold Fields](#hold-fields-issue-1406) |
| Which chart-log fields does `get_chart_data()` convert to the display unit in `state_log`, and which does it leave raw? | It converts exactly the keys in `_CHART_LOG_CONVERTED_TEMP_KEYS` (`pred_outdoor`, `pred_indoor`, `pred_outdoor_avg`, `pred_indoor_avg`, `lower`, `upper`, `hold_lower`, `hold_upper`, `indoor_primary`, and since Issue #1414 `feelslike`, `outdoor_feelslike`; `humidity` / `outdoor_humidity` are % and never converted). `indoor`, `outdoor`, `setpoint`, `nat_vent_target` stay raw °F because they are consumed through separately-converted series, and converting them would double-convert `effective_target_history`. | [Temperature Units and Display Conversion](#temperature-units-and-display-conversion-issue-1015) |
| Why does a Celsius install's historical Predicted Outdoor line still look wrong after the #1015 fix? | Chart-log entries written before the fix hold `pred_outdoor` in the provider's native unit and were deliberately not migrated, so the line stays wrong for windows still containing them until they age out (24h/3d raw views; up to 7/30/365 days for the 7d/30d/1y averages). Entries written after the fix are correct. | [Known limitation: pre-fix history](#known-limitation-pre-fix-history-not-migrated) |
| How is `pred_indoor` populated in each chart log entry? | Read from `_pred_archive` (first-write-wins prediction archive): the value for time T was written ~4 hours before T arrived, producing a genuine advance prediction. Falls back to `_last_predicted_indoor[0]["temp"]` only during the warmup period (first 4h after HA restart). `null` only if both archive and cache are empty. **Since #1230 R6 (X5) this is also the normal case when v3 is neither the primary nor the shadow engine** (the default, v4 primary with no comparison): v3's modelled curve and its 4 h archive are not built, so `_pred_archive` and `_last_predicted_indoor` stay empty and `pred_indoor` is `null` on every new row. Every consumer treats null as "no value": chart-log hourly/daily aggregation (`pred_indoor` / `pred_indoor_avg` null), the chart's past Predicted line (v3 line hidden unless v3 is primary/shadow), `prediction_accuracy` (v3 series empty, v4's live archive unaffected), `control_quality` (`archive_lead` n=0), and the backtest's `pred_indoor` sanity row (skipped when no row has a value). Pinned by `tests/test_control_feed_r6_x2_x5.py::test_null_pred_indoor_breaks_no_consumer`. Selected by `_select_pred_indoor(now_dt) -> (value, source)`; written regardless of whether `indoor` or `pred_outdoor` is null (Issue #1039). | [First-Write-Wins Prediction Archive](#first-write-wins-prediction-archive) |
| What fallback does `_build_future_forecast_outdoor()` use when hourly forecast is empty? | When hourly forecast is empty or yields no future entries and a `classification` is provided, generates a cosine curve using `classification.today_high`/`classification.today_low`. Returns `[]` only if no classification is provided. | [Coordinator Chart Log Wiring](#coordinator-chart-log-wiring) |
| What fallback does `_build_predicted_indoor_future()` use when hourly forecast is empty? | When `classification` is provided, synthesises a cosine-based hourly list from `classification.today_high`/`classification.today_low` and proceeds normally. Returns `[]` only if no classification is provided. | [Coordinator Chart Log Wiring](#coordinator-chart-log-wiring) |
| How does the chart show forecast days beyond the weather integration's native hourly horizon (e.g. met.no's ~48h)? | `get_chart_data()` calls `_build_extended_hourly_forecast()`, which appends synthetic cosine-shaped hourly days (via `_synthesize_hourly_day()`) built from the same integration's longer-range daily forecast, tagged `"synthetic": True`. Chart-display-only — never written back to `self._hourly_forecast_temps`, so it has zero effect on live automation decisions. | [Chart Forecast Horizon Extension (Issue #906)](#chart-forecast-horizon-extension-issue-906) |
| What happens when `load()` finds a missing, corrupt, or structurally wrong file? | Any of these three error paths resets `_entries` to `[]` and logs a WARNING. The coordinator continues with an empty log; no exception propagates to the caller. | [Load Contract](#load-contract) |
| What is the atomic write contract for `save()`? | Serializes to a temp file in the same directory, then `os.replace()` into the final path. On non-Windows the final file is `chmod`-ed to `0o600`. The original file is never touched until `os.replace()` succeeds. | [Save / Persistence Contract](#save--persistence-contract) |
| How do daily summary buckets differ from raw and hourly entries in their field names? | Daily summaries use `indoor_avg`/`indoor_min`/`indoor_max`, `outdoor_avg`/`outdoor_min`/`outdoor_max`, `fan_minutes`, and `events` (plural list). Raw and hourly entries use `indoor`, `outdoor`, `fan` (bool), and `event` (singular string). | [Downsampled Entry Schemas](#downsampled-entry-schemas) |

## Scope

This spec covers the `ChartStateLog` class in its entirety: construction, `load()`, `save()`, `append()`, `_maybe_prune()`, `get_entries()`, and the three internal bucketing helpers.

- **File:** `custom_components/climate_advisor/chart_log.py`
- **Approximate line range:** L81 – L465 (full class)
- **Entry point:** `ChartStateLog.__init__()` for construction; `append()` and `get_entries()` for runtime use

**Out of scope:**

- Full `get_chart_data()` payload assembly (target-band schedule, actual temperature series) — see [Architecture Reference §Coordinator Chart Helper Functions](02-ARCHITECTURE-REFERENCE.md#coordinator-chart-helper-functions)
- State persistence for the coordinator's own state — see [State Persistence](state-persistence.md)
- Thermal model parameters and ODE derivation — see [Thermal Model v3](thermal-model-v3-spec.md)

**In scope (coordinator wiring):** How `pred_outdoor` and `pred_indoor` are computed before `append()` is called, and the fallback behavior of `_build_future_forecast_outdoor()` and `_build_predicted_indoor_future()` — see [Coordinator Chart Log Wiring](#coordinator-chart-log-wiring) at the end of this file.

## Pre-conditions

### For `load()`
1. `ChartStateLog.__init__()` has been called; `self._path` is a valid `Path` object.
2. The caller has not yet called any other method that depends on `self._entries`.
3. If the file exists, the HA config directory is readable by the integration process.

### For `append()`
1. `load()` has been called at least once since construction (so `self._entries` is initialised to a list, not uninitialised).
2. `hvac` is a non-None string.
3. `fan`, `windows_open`, `windows_recommended` are booleans.
4. `indoor`, `outdoor`, `pred_outdoor`, `pred_indoor` are `float | None`.
5. `ts`, if supplied, is an ISO-8601 string parseable by `datetime.fromisoformat()`. If not supplied, the current HA wall-clock time is used.

### For `save()`
1. `self._path.parent` (the HA config directory) is writable by the integration process.
2. All entries in `self._entries` are JSON-serializable dicts.

### For `get_entries()`
1. `load()` has been called at least once since construction.
2. `range_str` is one of the seven documented values, or an arbitrary string (which maps to the `24h` default).
3. `before`, if supplied, is a timezone-aware `datetime`. When `None`, the window anchor defaults to `datetime.now(UTC)`.

## Post-conditions

### After `load()`
1. `self._entries` is a list (possibly empty).
2. All entries in `self._entries` have timestamps within the `max_days` window relative to the time `load()` was called.
3. Non-dict items in the on-disk `entries` array are silently discarded.
4. Entries whose `ts` field is absent or unparseable are silently discarded during load pruning.

### After `append()`
1. Exactly one new dict has been appended to `self._entries`.
2. The new entry always contains the nine core fields (`ts`, `hvac`, `fan`, `indoor`, `outdoor`, `windows_open`, `windows_recommended`, `pred_outdoor`, `pred_indoor`).
3. The `event` key is present in the new entry if and only if the `event` argument was non-None. The same rule holds for `indoor_primary` and `comfort_target` (non-None) and `indoor_sensors` (`indoor_sensors is not None`, so an empty `{}` is kept; the mapping is copied, never aliased).
4. If the hourly prune gate passes, all entries older than `max_days` are evicted from `self._entries`.
5. `save()` is NOT called by `append()` — the caller is responsible for persistence.

### After `save()`
1. If serialization succeeds and no OSError occurs: the file at `self._path` contains the full current `self._entries` list as a JSON object with shape `{"entries": [...]}`.
2. No `.tmp` file remains in the config directory (either renamed into place or deleted on error).
3. On non-Windows platforms: the file permissions are `0o600`.
4. If serialization or I/O fails: `self._entries` in memory is unchanged; the original on-disk file is unchanged; an ERROR is logged.

### After `get_entries()`
1. Returns a list of dicts in ascending timestamp order (earliest first).
2. All returned entries have a `ts` field.
3. If the log is empty or no entries fall within the requested range: returns `[]`.
4. Returned entries never contain data outside the requested time window.
5. When `before` is supplied, no returned entry has `ts ≥ before`; the window is `[before − range_days, before)` exactly.

## Invariants

1. **Append order preserved:** `self._entries` is ordered by insertion order. `get_entries()` returns bucketed results in ascending timestamp order (via `sorted(buckets)`), but the raw path returns insertion order — which is effectively ascending if callers always pass chronological timestamps.
2. **`ts` always present:** every entry appended by `append()` carries a `ts` field. Entries loaded from disk that lack a parseable `ts` are discarded, so after `load()` all in-memory entries have valid timestamps.
3. **Retention is time-based only:** there is no count cap. The upper bound at default settings is approximately 17,520 entries (48 ticks/day × 365 days), but short coordinator ticks or back-filled data can produce more entries between prune passes.
4. **Prune at most once per hour:** `_maybe_prune()` compares `datetime.now(UTC)` against `self._last_prune`. If the gap is less than one hour the prune is skipped, so the buffer may transiently hold entries outside the window between passes.
5. **Entries whose `ts` cannot be parsed survive pruning:** `_maybe_prune()` keeps entries with `ts is None` (unparseable) rather than dropping them silently, to surface bugs rather than hide them.
6. **Atomic write isolation:** the original file is never partially overwritten. All writes go to a tempfile in the same directory; `os.replace()` is the single commit point.
7. **No in-process sorting on append:** entries are not re-sorted by timestamp when appended. Callers are expected to call `append()` in chronological order.

## Retention Model

Retention is **time-based**. On `load()` and on each `append()` call that passes the one-hour prune gate, entries with `ts < now - max_days` are evicted.

- Default: `max_days = 365`
- Effective entry cap at ~30-minute coordinator cadence: **~17,520 entries**
- Expected file size: **~2 MB** (uncompressed JSON) before 0.9.1; `indoor_sensors` (Issue #1230 R0) adds roughly 40-45 bytes per configured indoor sensor per row, about **+2.3 MB a year** with three sensors
- Humidity and feels-like (Issue #1414) add **0 bytes** to a row with no humidity source. Measured live: about **105 rows a day per zone** (the 30-minute poll plus event rows), not 48. A typical row with both humidities from 1-2 dp sensors adds about **45 bytes** (`,"humidity":52.4,"outdoor_humidity":58.25`), about **+1.6 MB a year per zone**; the worst case, all four keys at full float precision (feels-like is never rounded), is at most **140 bytes**, about **+5.1 MB a year per zone**. Pinned by `tests/test_chart_humidity_1414.py::TestChartLogFields::test_size_budget`
- `hold_lower`/`hold_upper` (Issue #1406, 0.9.6) add **0 bytes** to an ordinary row (written only when the drawn band differs from `lower`/`upper`) and at most **42 bytes** to a bank / widened-period row (`,"hold_lower":NN.NN,"hold_upper":NN.NN` in the compact JSON `save()` writes). A daily 4-hour bank (8 rows a day) is about **+0.12 MB a year**; an install with no time-of-use schedule adds nothing. Pinned by `tests/test_chart_hold_1406.py::TestChartLogField::test_size_budget`
- No count-based cap exists. Long periods of sub-30-minute ticks can produce more entries.

Entries with unparseable `ts` fields are treated differently in the two prune contexts:
- **`load()`** discards them (strict: bad data from disk is not imported)
- **`_maybe_prune()`** keeps them (lenient: just appended by `append()`, dropping silently would hide bugs)

## Entry Schema

### Raw entry (produced by `append()`)

| Field | Type | Required | Description |
|---|---|---|---|
| `ts` | `str` (ISO-8601) | Always | Timestamp; defaults to `dt_util.now().isoformat()` |
| `hvac` | `str` | Always | HVAC mode string (e.g., `"heat"`, `"cool"`, `"off"`, `"fan_only"`) |
| `fan` | `bool` | Always | Whether the fan is active (legacy field — still emitted for back-compat with historical entries; see `fan_running` below) |
| `fan_running` | `bool` | Always (Issue #331+) | Fan is **physically running** — `_compute_fan_status()` ∈ `{"active", "running (manual override)", "running (untracked)"}`. Absent on entries written before Issue #331; frontend falls back to legacy `fan` field when absent. |
| `nat_vent_active` | `bool` | Always (Issue #331+) | Nat-vent session is **armed** — `automation_engine._natural_vent_active` is `True`. True even when the fan is paused between cycles (session alive, compressor not running). Absent on pre-#331 entries; frontend treats absent as `False`. |
| `indoor` | `float \| null` | Always | Indoor temperature in internal °F (regardless of display unit); `null` if unavailable. A weather-service outage does not blank it: the 30-min poll uses the live indoor reading when there is no forecast (Issue #1329) |
| `outdoor` | `float \| null` | Always | Outdoor temperature in internal °F; `null` if unavailable. During a weather-service outage a dedicated outdoor sensor (`outdoor_temp_source` sensor / input_number) still supplies it (`_live_dedicated_outdoor_temp()`, a successful read only, never a placeholder); a weather-service install has none and records `null` (Issue #1329). No 30-min poll is written during start-up coalescing while the thermostat has not reported yet (`_chart_poll_waits_for_startup()`), so a half-loaded start-up moment is never charted as a reading |
| `windows_open` | `bool` | Always | Whether any window/door sensor reports open |
| `windows_recommended` | `bool` | Always | Whether natural ventilation was recommended at this tick |
| `pred_outdoor` | `float \| null` | Always | Hourly forecast temperature for the current local hour, extracted by `_extract_current_hour_forecast_temp()` from `self._hourly_forecast_temps`. Since Issue #1015 (0.7.82) that list is normalised to internal °F at its source, so this value is °F on every install; entries written before 0.7.82 on Celsius installs hold the provider's native unit (see [Known limitation](#known-limitation-pre-fix-history-not-migrated)). `null` when the hourly forecast has no entry matching the current hour, or when hourly forecast is unavailable. (Fixed in Issue #132: previously stored a normalised value from `_build_outdoor_curve()`, which caused spikes at classification boundaries.) |
| `pred_indoor` | `float \| null` | Always | Predicted indoor temperature: read from `_pred_archive` (first-write-wins archive). The archived value for time T was written when T was ~4 hours in the future — i.e., the prediction was made ~4 hours before T arrived. Falls back to `_last_predicted_indoor[0]["temp"]` only during the warmup period (first 4 hours after HA restart). Written on every entry regardless of `indoor` or `pred_outdoor` being null (Issue #1039: during an indoor-sensor outage the entry holds `indoor=null` with a real `pred_indoor`); `null` when both archive and cache are empty, which since #1230 R6 (X5) is every new row while v3 is neither the primary nor the comparison engine (the 0.9.4 default). Selection is `_select_pred_indoor()`. See [First-Write-Wins Prediction Archive](#first-write-wins-prediction-archive). |
| `indoor_primary` | `float \| null` | Optional (Issue #1234, v0.8.34+) | The always-primary indoor reading in internal °F (`IndoorTempReading.primary_value` - never the sleep-window swap that `indoor` may carry). Written only on `hvac_action_change` rows, and only when a value exists; absent on every other row and on older logs. Read by `tools/thermal_replay.py`'s swing backfill (which uses ONLY this field) and by `tools/swing_audit.py --chart-log`. Because it exists only on `hvac_action_change` rows, the coast overshoot (Issue #1233, peak/trough after a stop) cannot be backfilled from chart_log for rows written before 0.9.1; from 0.9.1 on, every row carries `indoor_sensors`, so the 30-minute series of the primary sensor is recoverable as `indoor_sensors[<primary entity>]` and that gap is closable (Issue #1230 R0). Not averaged into hourly/daily buckets. |
| `comfort_target` | `float \| null` | Optional (Issue #1194 d, v0.8.46+) | The room's comfort target in internal °F, written only on rows where the remote-sensor outer loop holds an offset, so `setpoint` (the COMMANDED value, target + offset) and the target the room should reach are charted apart (`AutomationEngine.remote_offset_state()` via `_read_chart_comfort_target()`). Absent on every other row and on older logs. Hourly/daily buckets carry it only when at least one row in the bucket has it: the average of each row's `comfort_target`, or its `setpoint` when it has none (`_bucket_comfort_target()`), so the 7d/30d/1y Target line stays at the room's target. |
| `occupancy` | `str` | Optional (Issue #1230 R2, v0.9.4+) | The zone's occupancy mode on the row (`home`, `away`, `vacation`, `guest`), written at all five `append` sites (`coordinator._read_chart_context()`). Draws the Status chart's Away / Vacation strip. Absent on older rows. Hourly/daily buckets keep the dominant mode. Not a temperature: nothing to convert. |
| `windows_planned` | `bool` | Optional (Issue #1436) | Whether the operation timeline expected the windows open at this row (`timeline_surfaces.planned_windows_open` on the surface timeline, written by `coordinator._read_chart_context()` at all five append sites; absent without a plan and on older rows). Buckets: `true` if any row had it true. Draws the PAST "Win plan" strip (older rows fall back to `windows_recommended`, the classifier's live flag, which is true at 4 AM on a warm day and so could not be that strip). |
| `override` | `bool` | Optional (Issue #1230 R2) | `true` while a manual override or its grace period is active on the row; written only when true (absent = no override). Buckets: `true` if any row had it. Draws the Override / grace strip. |
| `hold_lower`, `hold_upper` | `float` | Optional (Issue #1406, v0.9.6+) | The band the chart DREW on the row, internal °F, 2 dp, written as a pair and only when it differs from `lower`/`upper` (by 0.05 °F or more): a time-of-use bank that owns the thermostat (drawn as the hold), a deferred bank (the schedule band the thermostat is on), the Away/Vacation high-cost period widened, the home band while the 15-minute away timer has not applied the setback yet (Issue #1425). Absent on every other row and on older logs. Display only: the history draws it; nothing else reads it. See [Hold Fields](#hold-fields-issue-1406). |
| `band_sensor` | `str` | Optional (Issue #1230 R2) | Entity id of the sensor Climate Advisor was reading for comfort on the row (the sensor the Target Band applied to; the bedroom sensor in the sleep window). Lets the chart label the band. Buckets keep the dominant sensor. |
| `indoor_sensors` | `dict[str, float \| null]` | Optional (Issue #1230 R0, v0.9.1+) | `{entity_id: internal °F rounded to 2 dp, or null}` for every indoor source: the climate entity (`current_temperature`), `indoor_temp_entity`, `sleep_indoor_temp_entity`, then (0.9.2, Issue #1230 R1) the companion temperature sensors on the climate entity's device, de-duplicated, in that order. Written on every coordinator row (all five `append` sites) regardless of the sleep window or which source is in use, so `indoor` (comfort-facing, may swap to the bedroom sensor) and this map can differ. `null` = the sensor failed the resolver's parse / plausibility (40-110 F) / 12 h stale rules (an `input_number.*` helper is exempt from the stale rule; the climate entity is not stale-checked). Absent on rows written before 0.9.1. Informational: nothing reads it for a decision; the v4 fit learns its per-sensor nodes from it (control nodes by the whole-window rule, display-only nodes from each sensor's first row, Issue #1422); `get_chart_data()` strips it from `state_log`. See [Indoor Sensors Field](#indoor-sensors-field-issue-1230-r0). |
| `humidity` | `float` | Optional (Issue #1414) | Indoor relative humidity, %, at the sensor's native resolution, of the room whose temperature is `indoor` on the row (the bedroom while the sleep sensor is the comfort reading). Written only when a source has a usable value; see [Humidity and Feels-Like Fields](#humidity-and-feels-like-fields-issue-1414). Not converted (a percentage). Buckets: mean of the rows that carry it. |
| `feelslike` | `float` | Optional (Issue #1414) | Indoor feels-like, internal °F, not rounded: the NWS heat index of `indoor` and `humidity`, ONLY where NWS uses its full regression (the simple formula averaged with T is 80 °F or more). No wind chill indoors. Absent otherwise. In `_CHART_LOG_CONVERTED_TEMP_KEYS`. Buckets: mean of the rows that carry it. |
| `outdoor_humidity` | `float` | Optional (Issue #1414) | Outdoor relative humidity, %, native resolution. Not converted. Buckets: mean of the rows that carry it. |
| `outdoor_feelslike` | `float` | Optional (Issue #1414) | Outdoor feels-like, internal °F, not rounded: the NWS heat index (same rule) of `outdoor` and `outdoor_humidity`, else the NWS wind chill of `outdoor` and the weather entity's `wind_speed` in mph (T <= 50 °F, wind > 3 mph). Absent otherwise, and on `hvac_action_change` rows (their `outdoor` is null). In `_CHART_LOG_CONVERTED_TEMP_KEYS`. |
| `event` | `str` | Optional | Event marker label (e.g., `"hvac_mode_changed"`, `"windows_opened"`). Present only when `event` argument was non-None. Issue #1425 (0.9.7): `occupancy_change` (you left, came home, or guest/vacation changed) and `occupancy_setback` (the away timer expired; the setback was applied or skipped), so the Away strip and the band change at the real moment. |

**Invariant:** `ts` is always present and always a string. Monotonic non-decrease of `ts` values is the caller's responsibility — the class does not enforce it.

## Temperature Units and Display Conversion (Issue #1015)

**Storage rule:** every temperature field in a chart-log entry is stored in internal °F, independent of the install's `temp_unit`. This includes `indoor`, `outdoor`, `setpoint`, `nat_vent_target`, `lower`, `upper`, `hold_lower`, `hold_upper`, `pred_indoor`, and `pred_outdoor` (for entries written since 0.7.82). The hourly/daily bucketing keeps the same units (averages/min/max of °F values).

**Why `pred_outdoor` is °F:** `_hourly_forecast_temps` is normalised to °F at its single source, `_get_hourly_forecast_data()`, and refreshed only through `_refresh_hourly_forecast()` — see [Temperature Conversion](temperature-conversion.md#hourly-forecast-is-normalised-to-internal-f-at-its-source-issue-1015). Before 0.7.82 the list held the provider's native unit, so Celsius installs wrote °C into `pred_outdoor`.

**Read-time conversion in `get_chart_data()`:** `_conv_log_entry()` converts the keys listed in the module constant `_CHART_LOG_CONVERTED_TEMP_KEYS` in `coordinator.py` from °F to the display unit when building `state_log`:

| Converted in `state_log` | Reason |
|---|---|
| `pred_outdoor`, `pred_indoor`, `pred_outdoor_avg`, `pred_indoor_avg` | Read by the frontend straight from `state_log` for the past Predicted lines |
| `lower`, `upper` | Historical Target Band; read straight from `state_log` by the frontend. Not converted before Issue #1015, so Celsius installs drew the past band in °F on a °C axis |
| `hold_lower`, `hold_upper` | Issue #1406: the past band the frontend draws from `state_log` when a row has the pair, so the same unit as `lower`/`upper` |
| `feelslike`, `outdoor_feelslike` | Issue #1414: the frontend draws the indoor / outdoor feels-like lines straight from `state_log` |
| `indoor_primary` | No frontend or post-conversion reader (the swing backfill reads the raw file); converted so a Celsius `state_log` never carries a stray °F value (Issue #1234) |

| Left raw °F in `state_log` (deliberately) | Why |
|---|---|
| `indoor`, `outdoor` | Read from the raw entries into the `actual_*` series, which are converted separately |
| `setpoint`, `nat_vent_target`, `comfort_target` | `_extract_historical_effective_target()` (and `_extract_historical_commanded_setpoint()`, which feeds `commanded_setpoint_history`) reads them after `_conv_log_entry()` and its result is converted again; converting here would double-convert `effective_target_history` |

| Removed (not converted) from `state_log` | Why |
|---|---|
| `indoor_sensors` | A nested `{entity_id: °F}` map cannot be converted key-by-key, and nothing in the frontend or the post-conversion readers uses it (the thermal tools read the raw file). `_conv_log_entry()` pops every key in the module constant `_CHART_LOG_STATE_LOG_STRIPPED_KEYS` (`coordinator.py`, currently `("indoor_sensors",)`) before the conversion loop, so a Celsius `state_log` can never carry a stray °F value in any tier (raw, hourly, daily) and the 24h/3d raw payload stays small. The stored log keeps the field. |

The historical-band series built before `_conv_log_entry()` reads raw `lower`/`upper` (or `hold_lower`/`hold_upper`) and converts them once itself, so adding them to the constant does not double-convert it.

**Guard against drift:** `tests/test_chart_celsius.py` structurally requires every temperature field `ChartStateLog.append()` emits to be either in `_CHART_LOG_CONVERTED_TEMP_KEYS` or on the test's explicit allowlist, so a newly added chart-log temperature field fails CI until it is classified. The same test requires every dict-valued `append()` keyword (found by signature introspection) to be listed in `_CHART_LOG_STATE_LOG_STRIPPED_KEYS`, and `test_state_log_never_carries_indoor_sensors` asserts the field is absent from `state_log` for Celsius and Fahrenheit across the 24h, 7d and 1y tiers while still present in the stored log.

### Known limitation: pre-fix history not migrated

Chart-log entries written before 0.7.82 on Celsius installs hold `pred_outdoor` in the provider's native unit (°C). They were left as they are — no migration, no change to the persisted file format (so no downgrade hazard). `get_chart_data()` converts them as if they were °F, so on Celsius installs the Predicted Outdoor line for past windows that still contain pre-fix entries stays wrong until they age out: the 24h/3d raw views while the window covers them, and the 7d/30d/1y averages for up to 7/30/365 days. Entries written after the fix display correctly. Historical `pred_indoor` (computed from the raw outdoor input) is likewise not repaired. Fahrenheit installs are unaffected (the normalisation is a passthrough for `"fahrenheit"`).

## Indoor Sensors Field (Issue #1230 R0)

**Why:** the v4 thermal model fits one line per indoor sensor, but `indoor` carries only the comfort-facing value (it swaps to the bedroom sensor in the sleep window) and `indoor_primary` exists only on `hvac_action_change` rows. `indoor_sensors` records what every configured sensor read on every row, so a fit never has to guess which sensor `indoor` came from.

**Reader:** `indoor_temp.read_indoor_sensors_f(hass=, unit=, climate_entity=, indoor_temp_entity=None, sleep_indoor_temp_entity=None, now=None) -> dict[str, float | None]`. The coordinator calls it through `_read_chart_indoor_sensors()` at all five `_chart_log.append(...)` sites.

- Sources, in order and de-duplicated: climate entity (`current_temperature` attribute via `read_attribute_temp_f`), `indoor_temp_entity` and `sleep_indoor_temp_entity` (state via `read_sensor_state_f`). An unconfigured source is omitted from the map; a configured one that fails is present with `null` (never a placeholder).
- Validation matches the resolver: the unit is converted to °F, then `_check_plausible` (40-110 F), then the `INDOOR_SENSOR_STALE_HOURS` (12 h) stale rule for sensors. `input_number.*` helpers are exempt from the stale rule and the climate entity is not stale-checked. Value is rounded to 2 decimals so a 0.18 F-step sensor keeps its resolution.
- **Companion sensors (0.9.2, Issue #1230 R1):** after the configured sources, every `sensor.*` entity on the SAME device as the zone's climate entity whose `device_class` is `temperature` (the user's registry override, else the integration's original class). Occupant view: a thermostat whose climate entity reports whole degrees (69) but whose device also exposes a finer temperature sensor (68.54, ~0.18 F steps) now has that sensor logged, so the model can learn the heating cycle. Found by `indoor_temp.discover_companion_temp_sensors(hass, climate_entity)`: an in-memory entity-registry lookup (the climate entry's `device_id`, then `async_entries_for_device(..., include_disabled_entities=False)`), so disabled entities are skipped; humidity, motion and other classes are excluded; a temperature sensor whose OWN name (`original_name` / `translation_key`, without the device name) contains the word outdoor, outside, exterior or floor is excluded too (0.9.2 R1b: e.g. a Honeywell thermostat's "Outdoor temperature" or a floor-heating thermostat's "Floor temperature" is not the room air, and could otherwise pass the 1 F consistency rule by coincidence; a thermostat named "First Floor" keeps its room sensor); no climate entity, an unregistered one, or one with no device yields no companions (simulated zones). Sorted, so row key order is stable. Companions are read exactly like any other sensor (same unit conversion, 40-110 F band, 12 h stale rule, `null` on failure) and a companion that is also a configured entity appears once. **Refresh rule:** the coordinator (`_companion_temp_sensors()`) caches the result for `COMPANION_SENSOR_REFRESH_SECONDS` (900 s, monotonic) and re-resolves on first use, so every setup/reload resolves anew and an entity enabled, disabled or renamed later is picked up within 15 minutes with no registry-event listener to tear down. The same candidates feed the R1b control rule ([Bug D](08-COMPUTATION-REFERENCE.md#bug-d--thermostat-companion-sensor-issue-1230-r1b)); on their own they change no decision.
- Identical regardless of the sleep window and of which source the resolver is currently using.
- **First row per sensor (v4 display nodes, Issue #1422, 0.9.8):** a sensor's first row in this map is when its own history began. v4 judges a sensor that its whole-window rule leaves out over the span from that first row (at least 3 days, a reading on at least 50 % of those rows) and draws it on the Status chart as a display-only line, so a newly logged thermostat or bedroom sensor stops reading "v4 is still learning" after about 3 days instead of about 15. See [Predictor v4: Display-only nodes](predictor-v4-spec.md#display-only-nodes-issue-1422).
- Pure read: no events, nothing above DEBUG, never raises (a failing source reads `null`; if the whole read fails the coordinator wrapper returns `{}`).
- Hourly and daily buckets: `_bucket_indoor_sensors()` returns the per-entity mean (2 dp) of the rows that carried the field, under the same `indoor_sensors` key in both shapes. An entity with no numeric reading in the bucket stays in the map as `null`; legacy rows contribute nothing; the key is omitted when no row in the bucket had the field (the `comfort_target` precedent).

Example raw row fragment: `"indoor_sensors": {"climate.living_room": 71.5, "sensor.bedroom_temp": 69.82, "sensor.office_temp": null}`; with a companion: `{"climate.hallway": 69, "sensor.santa_maria_hallway_current_temperature": 68.54}`.

**`indoor` / `indoor_primary` source (0.9.2, R1b):** on the `climate_fallback` source with `thermostat_companion_sensor` = `auto`, these two fields may carry the thermostat's finer companion sensor (e.g. 68.54 instead of the thermostat's 69) once it is proven, per [Bug D](08-COMPUTATION-REFERENCE.md#bug-d--thermostat-companion-sensor-issue-1230-r1b); `indoor_sensors` still lists the thermostat's own attribute and the companion as separate keys, so a fit can tell them apart.

**Native resolution audit (0.9.2):** a raw row stores the value CA read, never coarser than the sensor's own precision. `indoor` / `indoor_primary` come from `IndoorTempReading` (`read_attribute_temp_f` / `read_sensor_state_f` -> `to_fahrenheit`, no rounding) through `ForecastSnapshot.current_indoor_temp` (`float(...)`) into `ChartStateLog.append()`, which stores them as given; `setpoint` and `comfort_target` are likewise unrounded `to_fahrenheit` values; `indoor_sensors` is rounded to 2 dp in `read_indoor_sensors_f`. The only roundings on the way to a raw row are that 2 dp (finer than a 0.18 F step or a C->F conversion needs). The hourly/daily buckets round to 1 dp (`indoor`, `indoor_avg`, ...) or 2 dp (`indoor_sensors`) for display; the forecast archive stores only outdoor temperatures (1 dp) and no indoor value. Whole-degree indoor values in a row therefore mean the climate entity itself reported whole degrees (hence the companion sensor), not that CA rounded them.

**Tests:** `tests/test_chart_indoor_sensors.py` (reader rules, companion discovery and reading, discovery cache and refresh, append-site coverage, round trip, bucketing), `tests_ha/test_companion_temp_sensor_1230.py` (real HA device and entity registries), `tests/test_chart_celsius.py` (strip guard).

## Humidity and Feels-Like Fields (Issue #1414)

**Occupant view:** the Status charts show how humid the room Climate Advisor is reading is and how humid it is outside, and on a hot humid afternoon how hot it feels (92 °F at 65 % feels like about 108 °F), or on a cold windy morning how cold (30 °F in a 20 mph wind feels like about 17 °F). A mild day shows the humidity and no feels-like line, never a stand-in number. **Chart display only:** no decision, setpoint or control path reads any of it (`tests/test_chart_humidity_1414.py::test_no_automation_or_control_module_reads_the_new_values`, `::test_in_the_coordinator_only_the_chart_paths_touch_it`).

**Writer:** `coordinator._read_chart_context(indoor=, outdoor=)` (spread into all five `append` sites, each passing the same `indoor` / `outdoor` it stores) calls `_read_chart_humidity()`. Never raises; a key is present only with a value.

**Humidity source order** (`humidity_sources.resolve_humidity`, first usable value wins):

| Role | 1st | 2nd | 3rd |
|---|---|---|---|
| `indoor_day` | `indoor_humidity_entity` (Settings) | a `device_class: humidity` sensor on the climate entity's device, then on the comfort temperature source entity's device | the climate entity's `current_humidity` attribute |
| `indoor_sleep` | `sleep_indoor_humidity_entity` | a humidity sensor on `sleep_indoor_temp_entity`'s device | none |
| `outdoor` | `outdoor_humidity_entity` | a humidity sensor on the outdoor temperature sensor's device (`outdoor_temp_entity`, sensor source only) | the weather entity's `humidity` attribute |

- **Which indoor role:** `indoor_sleep` when the row's comfort reading came from the sleep sensor (`_get_indoor_temp_with_provenance().source_entity == sleep_indoor_temp_entity`, sleep-window swapped), else `indoor_day`. So the bedroom row never carries the hallway's humidity: with no bedroom humidity it carries none.
- **Never used:** the climate `humidity` attribute (Home Assistant's TARGET humidity, `ClimateEntityStateAttribute.TARGET_HUMIDITY`; the measured value is `current_humidity`), weather humidity for an indoor role, `number.*` entities, and, for the indoor roles' auto-detection, a sensor whose own name says outdoor / outside / exterior / floor (`indoor_temp._names_non_room_sensor`).
- **A user's choice is trusted,** but the value must be finite and within 0-100 %, else the next source is tried.
- **Discovery:** `indoor_temp.discover_companion_sensors(hass, entity_id, device_class, *, exclude_non_room=True)` (the generalised `discover_companion_temp_sensors`, which is now a thin wrapper), cached per (entity, device class, exclusion) in `coordinator._companion_sensors_cache` for `COMPANION_SENSOR_REFRESH_SECONDS`.
- **Native resolution:** never rounded (a 52.43 % sensor is stored as 52.43).

**Feels-like rule** (`feels_like.py`, `feels_like_f(temp_f, rh, wind_mph=None)`):

- **Heat index**, [NWS WPC](https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml): the simple formula `HI = 0.5 * {T + 61.0 + [(T-68.0)*1.2] + (RH*0.094)}` averaged with T; if that average is 80 °F or more, the Rothfusz regression, minus `[(13-RH)/4]*SQRT{[17-ABS(T-95.)]/17}` when RH < 13 % and 80-112 °F, plus `[(RH-85)/10] * [(87-T)/5]` when RH > 85 % and 80-87 °F. Below that NWS calls the regression "not appropriate" and no value is written.
- **Wind chill**, [NWS](https://www.weather.gov/safety/cold-wind-chill-chart): `35.74 + 0.6215T - 35.75(V^0.16) + 0.4275T(V^0.16)`, T in °F, V in mph, "only defined for temperatures at or below 50 °F and wind speeds above 3 mph". Outdoor only; the weather entity's `wind_speed` is converted from its `wind_speed_unit` (mph, km/h, m/s, kn, ft/s) to mph (`feels_like.wind_speed_to_mph`; an unknown or missing unit gives no wind chill).
- The heat index wins when both could apply (they cannot at the same temperature).

**Buckets:** `chart_log._bucket_optional_means()` gives each of the four keys the mean over the rows in the bucket that carry it (not rounded), absent when none do; the same keys in the hourly and daily shapes.

**`get_chart_data()`:** `state_log` carries the four keys as stored, `feelslike` / `outdoor_feelslike` converted to the display unit. Live views (not historical) also carry `outdoor_humidity_forecast` and `outdoor_feelslike_forecast`, `[{"ts", "value"}]`, future only (ts > now), built from the REAL hourly forecast (`_hourly_forecast_temps`, never the synthetic extension) by `_build_outdoor_humidity_feelslike_forecast()`, present only when a future entry carries `humidity` (the feels-like list may then be empty: points with no defined value are omitted). Wind chill points use the entry's `wind_speed` in the weather entity's `wind_speed_unit`. Whether met.no's hourly entries carry humidity / wind is read from Home Assistant's `met` integration source (its `FORECAST_MAP` maps both) but not verified on a live install.

**Tests:** `tests/test_feels_like.py` (NWS values and boundaries), `tests/test_chart_humidity_1414.py` (source order per role, never-used sources, day / sleep pairing, rows, buckets, size, °C `state_log`, forecast lists, chart-only guard), `tests/test_config_flow.py::TestOptionsFlowClearing` (selectors clear), `tests_ha/test_chart_humidity_1414.py` (real registries and a real append site: hot humid, mild, cold windy rows).

## Drawing the Past Band and the Away Strip (Issue #1425)

**Every past activity strip (Issue #1436):** like the band and the Away strip, a row's state holds from its time until the next row and never past NOW (the last row holds up to 30 minutes). Before 0.9.8 the other strips were centred on each row (half-way to the previous and the next row), so a heat run logged at 05:48 drew from about 05:33, before it started. The past Win plan reads `windows_planned`.

A past row's band is the band in effect from its time until the next row: `chart_v2.js` `_stepHeld()` holds each past value flat and changes at the next row's time (two points at that instant), never a slope between rows. Before 0.9.7 a 30-minute Away recorded on one row (63-79 F between 68-74 F rows) drew as a tall triangle. The forecast band (`target_band`, after now) keeps its straight lines (its wake/sleep ramps are scheduled slopes). The Away / Vacation strip runs from an away row to the next row (the occupancy rows above make those the moments you left and came home); the other strips keep the midpoint convention. While the away timer runs (`OCCUPANCY_SETBACK_MINUTES`), `_chart_display_band()`'s `_chart_away_pending_band()` draws the hour in force as the home band (display only: the band schedule, the prediction and `lower`/`upper` are unchanged), so a row written when you leave carries the home band as its hold. Since 0.9.8 the thermostat matches it: no path sends the setback before the timer expires (Issue #1433, [Occupancy Dispatch](occupancy-dispatch-spec.md#state-transitions)); before, a 30-minute cycle inside the timer could send it while the chart drew the home band.

## Hold Fields (Issue #1406)

**Occupant view:** scrolling the chart back over an Away cool bank (cool to 77 °F inside the 60–80 °F Away band) drew the band schedule's shape, 77–80 °F, while the live chart (since #1404) drew what the thermostat held, 60–77 °F. History and live now draw the same afternoon the same way.

**Why a new field:** `lower`/`upper` persist `_target_band_lower_upper_now()`, the band SCHEDULE (`_build_target_band_for()`, the #786 shape: a cool-bank target on the lower edge), which the replay tools (`tools/predict_backtest.py`, `tools/timeline_replay.py`, ...), the prediction curve and the comfort score read. Their meaning is unchanged. The schedule edge a bank replaces is not in the row, so the hold cannot be rebuilt at display time; it is recorded instead.

**Writer:** `coordinator._read_chart_context()` (spread into all five `append` sites) calls `_read_chart_hold()`: `_chart_hold_lower_upper_now()` runs the live chart's own function, `_chart_display_band(now, control_thermal_model(...), raw_band=_target_band_schedule)` (bank layer `_chart_bank_band`, widened layer `_chart_widened_band`; both read `current_hold()` / the cached TOU resolution), and picks the entry in force at `now` with `_band_entry_at()`, the same picker `_target_band_lower_upper_now()` uses. The pair is written (2 dp) only when it differs from `lower`/`upper` by 0.05 °F or more, so it appears while a bank owns the thermostat (the hold, e.g. 60–77), while a bank is deferred (the schedule band the thermostat is on, e.g. 60–80 where `lower` holds the #786 77) and during the Away/Vacation widened period (e.g. 60–83). Never raises; display only (in `test_current_hold_1404.py`'s display-caller list).

**Readers:** `_extract_historical_target_band()` (a historical `get_chart_data()` view: `target_band`, `defense_lines`) and `chart_v2.js`'s past band (`state_log`) use the pair when a row has both keys, else `lower`/`upper` (every row before 0.9.6). Both are in `_CHART_LOG_CONVERTED_TEMP_KEYS`. Buckets: `_bucket_hold()`, see the hourly / daily tables.

**Known limitation:** rows written before 0.9.6 keep the old shape until they age out (no migration, no downgrade hazard: an older version ignores the keys).

**Tests:** `tests/test_chart_hold_1406.py` (the row field for a bank, no bank, deferred, Away widened, Home, °C; buckets; size; historical `get_chart_data()` in °F/°C for raw, 7d, 1y), `tests_ha/test_chart_history_hold_1406.py` (the real coordinator's rows and the real REST `chart_data` view, US and metric), `tests/ui/chart-history-hold.spec.js`, `tests/browser/check_status_final.py::tou_bank_history`.

## Prediction Engine v4 Consumers (Issue #1230 R1)

v4 trains on this log and nothing else changes in its schema. Since 0.9.4 v4 is the default primary engine (it drives the prediction-dependent decisions and is the Status chart's main line, [Prediction Engines Spec](prediction-engines-spec.md)); v3's `pred_indoor` is written only while v3 is the primary or comparison engine (otherwise `null`, see the `pred_indoor` row above). v4's own predictions are kept in its archive (`climate_advisor_v4_archive_<entry_id>.json`), not in this log.

- **Fit:** `v4_shadow.run_fit` passes `ChartStateLog.raw_entries()` (a shallow copy of every retained row, undownsampled; rows are never mutated after append, so the copy is safe on an executor thread) to `predictor_v4.samples_from_chart_log`, which resamples to a 15-minute grid. Fields read: `ts`, `hvac` (the action string), `fan_running`/`fan`, `indoor`, `indoor_sensors`, `outdoor`, `windows_open`, `pred_outdoor`, `setpoint`. Gaps over 2 h break continuity. `setpoint` is null in older Zone 1 history, so a house with only such rows keeps the learned-thermostat defaults ([Predictor v4: thermostat](predictor-v4-spec.md#thermostat-learning-and-emulation)).
- **Per cycle:** the coordinator reads `ChartStateLog.last_entry()` (the newest row appended, any kind; `chart_log.py`) right after it wrote this cycle's row and skips v4 for that cycle unless it is a non-event row less than 120 s old, so the live readings equal the training fields ([v4 Shadow: per-cycle inputs](v4-shadow-spec.md#per-cycle-inputs)).
- **Scoring:** the prediction-accuracy endpoint reads `raw_entries()` for the "actual" reading (`indoor` / `indoor_sensors[entity]`), v3's `pred_indoor` and heat runs ([REST API](rest-api.md#get-apiclimate_advisorprediction_accuracy-issue-1230-r1)).
- **Retention interaction:** the 365-day chart log bounds what a fit and the "no change" baseline can see; the v4 archive keeps its own 400 days.

## Vent Bar Fields (Issue #331)

_Added in Issue #331 (chart Vent bar + compressor-only HVAC bar)._

Prior to Issue #331 the chart had four status rows: HVAC, Fan, Win Rec, Windows. The
`fan` field conflated "fan physically on" with "nat-vent session armed (fan may be
paused between cycles)", and the HVAC bar lit green for `hvac_action=fan`, double-showing
the fan state. Issue #331 collapsed Fan + Win Rec into one **Vent** bar and restricted
the HVAC bar to compressor-only states.

### New fields added to every `state_log` entry

| Field | Source | Semantics |
|---|---|---|
| `fan_running` | `_compute_fan_status() in {"active", "running (manual override)", "running (untracked)"}` | Fan is **physically running**. False when nat-vent is armed but the fan is paused between cycles (#327 thermostatic control). |
| `nat_vent_active` | `automation_engine._natural_vent_active` | Nat-vent session is **armed**. True even when the fan is cycling off (session alive). |

The legacy `fan` field is still written at every tick to preserve backward compatibility
with historical chart log entries. New code should read `fan_running` and `nat_vent_active`.

### Frontend Vent bar rendering contract

The dashboard (`frontend/index.html` `drawActivityTimeline`) renders **one** Vent row
(replacing the prior Fan and Win Rec rows):

| Condition (evaluated in priority order) | Bar color | Label |
|---|---|---|
| `fan_running === true` | Blue (`rgba(66,165,250,0.75)`) | "Fan running" |
| `nat_vent_active === true \|\| windows_recommended === true` | Green (`rgba(102,187,106,0.70)`) | "Vent armed" |
| none of the above | Off (no bar drawn) | — |

**Back-compat for pre-#331 entries** (no `fan_running`/`nat_vent_active` fields):
- If `fan_running` is absent, fall back to legacy `fan` field → blue.
- If `nat_vent_active` is absent, treat as `false` and evaluate `windows_recommended` only.

### Frontend HVAC bar rendering contract

The HVAC bar draws **compressor-only** states:

| `hvac` value | Bar color |
|---|---|
| `"heating"` | Red |
| `"cooling"` | Blue |
| `"fan_only"`, `"off"`, any other | No bar |

The prior behavior — drawing a green bar for `hvac_action=fan` — is removed. Fan state
is now represented exclusively by the Vent bar.

### Net bar layout change

| Before Issue #331 | After Issue #331 |
|---|---|
| HVAC (heating/cooling/fan) | HVAC (compressor only) |
| Fan | Vent (fan running or nat-vent armed) |
| Win Rec | _(merged into Vent)_ |
| Windows (contact sensors) | Windows (contact sensors, unchanged) |

Legend labels updated accordingly in `drawActivityTimeline`.

## Append Contract

`append()` signature (keyword-only arguments):

```python
def append(
    self,
    *,
    hvac: str,
    fan: bool,
    indoor: float | None,
    outdoor: float | None,
    windows_open: bool = False,
    windows_recommended: bool = False,
    pred_outdoor: float | None = None,
    pred_indoor: float | None = None,
    setpoint: float | None = None,
    event: str | None = None,
    ts: str | None = None,
    fan_running: bool = False,
    nat_vent_active: bool = False,
    lower: float | None = None,
    upper: float | None = None,
    nat_vent_target: float | None = None,
    indoor_primary: float | None = None,
    comfort_target: float | None = None,
    indoor_sensors: dict[str, float | None] | None = None,
    occupancy: str | None = None,
    override: bool | None = None,
    band_sensor: str | None = None,
    hold_lower: float | None = None,
    hold_upper: float | None = None,
) -> None:
```

- If `ts` is `None`, the entry timestamp is set to `dt_util.now().isoformat()` (HA-aware wall clock).
- `event`, `indoor_primary`, `comfort_target`, `indoor_sensors`, `occupancy` and `band_sensor` are the conditionally included fields (each only when its argument is not `None`); `override` only when true; `hold_lower`/`hold_upper` only as a pair (both not `None`, Issue #1406). `indoor_sensors` is stored as a shallow copy (`dict(indoor_sensors)`) so later mutation by the caller cannot alter the log. Every other field is always written, even when its value is `None` or `False`.
- After appending, `_maybe_prune()` is called. If the prune gate is open (≥1 hour since last prune), old entries are evicted in-place.
- `save()` is not called. The caller (coordinator) is responsible for persisting after `append()`.

## Query Contract

`get_entries(range_str: str = "24h", before: datetime | None = None) -> list[dict[str, Any]]`

The optional `before` parameter anchors the query window to a past point in time. When supplied, the returned range is `[before − range_days, before)` instead of `[now − range_days, now)`. When `None` (default), the window ends at the current clock time.

This parameter is the building block for chart historical navigation: `get_chart_data(before_ts=...)` passes a `datetime` derived from `before_ts` straight through to `get_entries(before=...)`.

### Recognised `range_str` values

| `range_str` | Days covered | Downsampling tier |
|---|---|---|
| `"6h"` | 0.25 | Raw |
| `"12h"` | 0.5 | Raw |
| `"24h"` | 1.0 | Raw |
| `"3d"` | 3.0 | Raw |
| `"7d"` | 7.0 | Hourly averages |
| `"30d"` | 30.0 | Hourly averages |
| `"1y"` | 365.0 | Daily summaries |
| _(anything else)_ | 1.0 | Raw (defaults to `"24h"`) |

### Edge cases
- Empty log → `[]`
- No entries in range → `[]`
- Single entry in an hourly or daily bucket → that entry is the sole member of its bucket; averages equal the single value; `fan_minutes` = 30 (one tick assumed to be 30 min)

## Downsampling Rules

The downsampling tier is selected by `_range_str_to_days()` against two thresholds:
- `_RAW_THRESHOLD_DAYS = 3` — at or below this, return raw entries
- `_HOURLY_THRESHOLD_DAYS = 30` — between 3 and 30 days (exclusive), return hourly averages; above 30 days, return daily summaries

## Downsampled Entry Schemas

### Hourly average entry (produced by `_bucket_hourly()`)

| Field | Type | Description |
|---|---|---|
| `ts` | `str` | Hour-truncated UTC ISO string (e.g., `"2025-01-15T14:00:00+00:00"`) |
| `hvac` | `str` | Dominant HVAC mode (most frequent non-empty value in the hour) |
| `fan` | `bool` | `True` if any entry in the hour had `fan=True` |
| `indoor` | `float \| null` | Mean of non-null indoor values in the hour, rounded to 1 decimal |
| `outdoor` | `float \| null` | Mean of non-null outdoor values in the hour, rounded to 1 decimal |
| `windows_open` | `bool` | `True` if any entry in the hour had `windows_open=True` |
| `windows_recommended` | `bool` | `True` if any entry in the hour had `windows_recommended=True` |
| `pred_outdoor` | `float \| null` | Mean of non-null `pred_outdoor` values, rounded to 1 decimal |
| `pred_indoor` | `float \| null` | Mean of non-null `pred_indoor` values, rounded to 1 decimal |
| `comfort_target` | `float \| null` | Issue #1194 (d). Present only when at least one entry in the bucket had `comfort_target`: mean over the entries of `comfort_target`, or `setpoint` when that entry has none, rounded to 1 decimal (`_bucket_comfort_target()`) |
| `indoor_sensors` | `dict[str, float \| null]` | Issue #1230 R0. Present only when at least one entry in the bucket had the field: per-entity mean of the numeric readings, rounded to 2 decimals; an entity with no numeric reading in the hour is `null` (`_bucket_indoor_sensors()`) |
| `lower`, `upper` | `float \| null` | Mean of the non-null values, rounded to 1 decimal (the band schedule; unchanged by Issue #1406) |
| `hold_lower`, `hold_upper` | `float` | Issue #1406. Present only when at least one entry in the bucket had the pair: the mean, 1 dp, of each entry's drawn band (its `hold_lower`/`hold_upper`, else its `lower`/`upper`), the same mean rule as `lower`/`upper` (`_bucket_hold()`) |
| `event` | `list[str]` | Collected event marker strings from the hour. Present only when at least one entry had an `event` field. |

### Daily summary entry (produced by `_bucket_daily()`)

| Field | Type | Description |
|---|---|---|
| `ts` | `str` | Midnight UTC ISO string for the day (e.g., `"2025-01-15T00:00:00+00:00"`) |
| `hvac` | `str` | Dominant HVAC mode for the day |
| `fan_minutes` | `int` | Count of entries with `fan=True` × 30 (assumes ~30-min tick cadence) |
| `indoor_avg` | `float \| null` | Mean of non-null indoor values, rounded to 1 decimal |
| `indoor_min` | `float \| null` | Minimum of non-null indoor values |
| `indoor_max` | `float \| null` | Maximum of non-null indoor values |
| `outdoor_avg` | `float \| null` | Mean of non-null outdoor values, rounded to 1 decimal |
| `outdoor_min` | `float \| null` | Minimum of non-null outdoor values |
| `outdoor_max` | `float \| null` | Maximum of non-null outdoor values |
| `windows_open` | `bool` | `True` if any entry in the day had `windows_open=True` |
| `windows_recommended` | `bool` | `True` if any entry in the day had `windows_recommended=True` |
| `pred_outdoor_avg` | `float \| null` | Mean of non-null `pred_outdoor` values, rounded to 1 decimal |
| `pred_indoor_avg` | `float \| null` | Mean of non-null `pred_indoor` values, rounded to 1 decimal |
| `comfort_target` | `float \| null` | Issue #1194 (d). Present only when at least one entry in the bucket had `comfort_target`: mean over the entries of `comfort_target`, or `setpoint` when that entry has none, rounded to 1 decimal (`_bucket_comfort_target()`) |
| `indoor_sensors` | `dict[str, float \| null]` | Issue #1230 R0. Same key and rule as the hourly shape: per-entity mean over the day's rows that carried the field, 2 decimals, `null` when silent all day; omitted when no row had it |
| `lower`, `upper` | `float \| null` | Mean of the non-null values, rounded to 1 decimal (same as hourly) |
| `hold_lower`, `hold_upper` | `float` | Issue #1406. Present only when at least one entry in the bucket had the pair: the mean, 1 dp, of each entry's drawn band (its `hold_lower`/`hold_upper`, else its `lower`/`upper`), the same mean rule as `lower`/`upper` (`_bucket_hold()`) |
| `events` | `list[str]` | Collected event marker strings for the day (plural key). Present only when at least one entry had an `event` field. |

**Key schema difference summary:**

| Aspect | Raw | Hourly | Daily |
|---|---|---|---|
| Temperature fields | `indoor`, `outdoor` | `indoor`, `outdoor` (averaged) | `indoor_avg/min/max`, `outdoor_avg/min/max` |
| Fan field | `fan: bool` | `fan: bool` (OR) | `fan_minutes: int` |
| Event field key | `event: str` (optional) | `event: list[str]` (optional) | `events: list[str]` (optional) |
| Prediction fields | `pred_outdoor`, `pred_indoor` | `pred_outdoor`, `pred_indoor` (averaged) | `pred_outdoor_avg`, `pred_indoor_avg` |
| Per-sensor readings | `indoor_sensors` (per-row) | `indoor_sensors` (per-entity mean) | `indoor_sensors` (per-entity mean, same key) |

## Load Contract

`load()` reads from `self._path` (`/config/climate_advisor_chart_log.json` by default).

### Success path
1. File is read as UTF-8 text.
2. JSON is parsed; result must be a `dict` with an `"entries"` key pointing to a `list`.
3. Each item in `entries` that is a `dict` with a parseable, in-window `ts` is kept; all others are discarded.
4. `self._entries` is set to the pruned list.

### Error paths

| Condition | Handling | Result |
|---|---|---|
| File does not exist | Silent return | `self._entries = []`, no log message |
| File exists but `OSError` on read | Log WARNING with error string | `self._entries = []` |
| File exists but JSON decode fails | Log WARNING with error string | `self._entries = []` |
| JSON parses but top-level is not a `dict` | Log WARNING | `self._entries = []` |
| `"entries"` key missing or not a `list` | Log WARNING | `self._entries = []` |
| Individual entries that are not `dict` | Silently skip that entry | Remaining valid entries kept |
| Entry `ts` absent or unparseable | Entry is discarded (not kept) | Remaining valid entries kept |
| Entry `ts` older than `max_days` cutoff | Entry is discarded | Remaining valid entries kept |

In all error paths `load()` returns `None` and does not raise. The coordinator receives an empty log and continues normally.

## Save / Persistence Contract

`save()` writes `{"entries": self._entries}` to `self._path` using an atomic tempfile pattern:

1. `json.dumps()` serializes the entries list. Compact separators (`","`, `":"`) are used.
2. `tempfile.mkstemp()` creates a `.tmp` file in `self._path.parent` (the HA config dir).
3. The serialized content is written to the temp file.
4. `os.replace(tmp_path, str(self._path))` atomically promotes the temp file to the final path.
5. On non-Windows platforms: `os.chmod(str(self._path), 0o600)` restricts read access.

### Error conditions

| Failure | Handling | On-disk state |
|---|---|---|
| `json.dumps()` raises `TypeError`/`ValueError` | Log ERROR; return immediately | Original file unchanged; no `.tmp` created |
| `OSError` writing to `.tmp` or on `os.replace()` | Log ERROR; attempt `os.unlink(tmp_path)` (suppressing `OSError`) | Original file unchanged; `.tmp` deleted if possible |

**The original file is never modified until `os.replace()` succeeds.** If the process crashes between tempfile creation and `os.replace()`, a stale `.tmp` file may remain in the config directory but the original is intact.

## State Transitions

`ChartStateLog` is not a state machine. Its lifecycle is:

| Phase | Description |
|---|---|
| Constructed | `self._entries = []`; `self._last_prune = None` |
| Loaded | `load()` called; `_entries` populated from disk (or reset to `[]` on any error) |
| Append loop | `append()` called every ~30 min by the coordinator; `_maybe_prune()` called after each append |
| Persisted | `save()` called by the coordinator after appending (and on shutdown) |

## Code Reference

- [`ChartStateLog.__init__`](../custom_components/climate_advisor/chart_log.py#L103) — construction; sets `_path`, `_max_days`, `_entries`, `_last_prune`
- [`ChartStateLog.load`](../custom_components/climate_advisor/chart_log.py#L97) — disk read, parse, prune on startup
- [`ChartStateLog.save`](../custom_components/climate_advisor/chart_log.py#L157) — atomic write to disk
- [`ChartStateLog.append`](../custom_components/climate_advisor/chart_log.py#L181) — entry construction and in-memory append
- [`ChartStateLog._maybe_prune`](../custom_components/climate_advisor/chart_log.py#L286) — time-gated retention enforcement
- [`ChartStateLog.get_entries`](../custom_components/climate_advisor/chart_log.py#L308) — filtered, downsampled retrieval
- [`ChartStateLog.last_entry`](../custom_components/climate_advisor/chart_log.py#L342) / [`raw_entries`](../custom_components/climate_advisor/chart_log.py#L346) — newest row / shallow copy of all rows for the v4 shadow (Issue #1230 R1)
- [`ChartStateLog._bucket_hourly`](../custom_components/climate_advisor/chart_log.py#L390) — 1-hour average bucketing
- [`ChartStateLog._bucket_daily`](../custom_components/climate_advisor/chart_log.py#L449) — daily summary bucketing
- [`_bucket_indoor_sensors`](../custom_components/climate_advisor/chart_log.py#L62) - per-entity mean for hourly/daily buckets (Issue #1230 R0)
- [`read_indoor_sensors_f`](../custom_components/climate_advisor/indoor_temp.py#L335) - the per-row sensor snapshot
- [`_read_chart_indoor_sensors`](../custom_components/climate_advisor/coordinator.py#L5392) - coordinator wrapper used at all five `append` sites
- [`_CHART_LOG_STATE_LOG_STRIPPED_KEYS`](../custom_components/climate_advisor/coordinator.py#L528) and `_conv_log_entry()` (`coordinator.py#L15883`) - strip from `state_log`
- [`_parse_ts`](../custom_components/climate_advisor/chart_log.py#L33) — ISO-8601 parse helper; returns `None` on failure

## Coordinator Chart Log Wiring

This section covers the coordinator-side logic that computes `pred_outdoor` and `pred_indoor` before calling `ChartStateLog.append()`, and the helper functions used for the future predicted lines in `get_chart_data()`.

**Covered functions:** `_extract_current_hour_forecast_temp`, `_build_future_forecast_outdoor`, `_build_predicted_indoor_future` — all in `coordinator.py`.

### `pred_outdoor` and `pred_indoor` at append time

Located in `coordinator.py` inside `_async_update_data()` at the chart log append block.

**`pred_outdoor`** is the hourly forecast temperature (internal °F since Issue #1015) for the current local hour:

```python
_pred_outdoor_val = _extract_current_hour_forecast_temp(self._hourly_forecast_temps, now_dt)
```

- `_extract_current_hour_forecast_temp()` scans `self._hourly_forecast_temps` for an entry whose local datetime matches today's date and the current local hour.
- Returns the `temperature` field (rounded to 1 decimal) as it sits in `_hourly_forecast_temps` — already internal °F (Issue #1015) — with no curve-normalisation or further unit conversion.
- Returns `None` if hourly forecast is absent or has no entry for the current hour.
- Uses the same field name and timezone handling as `_build_future_forecast_outdoor()` so past and future predicted outdoor values come from the same data source.

**`pred_indoor`** is read from `_pred_archive` — the first-write-wins prediction archive (added in Issue #139):

```python
_archived_pred = self._lookup_pred_archive(_now_dt)
if _archived_pred is not None:
    _pred_indoor_val = _archived_pred
elif self._last_predicted_indoor:
    _pred_indoor_val = self._last_predicted_indoor[0].get("temp")  # warmup fallback
```

- `_lookup_pred_archive()` looks up `_pred_archive[_pred_archive_key(now_dt)]` — the prediction for this 30-min slot that was written ~4 hours ago.
- Falls back to `_last_predicted_indoor[0]["temp"]` (the current-tick ODE[0] value) only during the warmup period (first 4 hours after HA restart, before the archive has been populated 4 hours ahead).
- Written as `None` if both archive and cache are empty, or if `_pred_outdoor_val` is `None`.
- Log line emitted at every tick: `chart_log pred_indoor=X indoor=Y delta=+Z.Z (archive|ode-warmup|none)`.

**Guard:** the entire block is wrapped in `contextlib.suppress(Exception)` — any failure silently writes `null` for both fields rather than crashing the coordinator tick.

### First-Write-Wins Prediction Archive

Added in Issue #139. Resolves the structural convergence problem where re-seeding the ODE from actual indoor temp at every 30-min tick caused `pred_indoor ≈ actual_indoor` during low-delta periods.

**Root cause of the old behavior:** at T=10:00, ODE seeds from `actual(10:00)=69°F` → prediction for T+1h saved. At T=10:30, ODE seeds from `actual(10:30)=69.5°F` → new prediction for T+1h saved (overwrites intent). Historical chart showed pred running ~1.2°F above actual — but because every tick restarted from actual, the prediction never diverged from the actual trajectory. Fix: freeze the prediction at first write.

**Architecture:**

`self._pred_archive: dict[int, float]` — maps epoch_30min → predicted_temp.

- **Key:** Unix epoch timestamp rounded to the nearest **30 minutes** (via `_pred_archive_key(dt)`) — O(1) lookup.
- **Value:** predicted indoor temp for that 30-min slot, from the **first** ODE run that covered it.
- **Write:** at each 30-min tick, after computing `_last_predicted_indoor`, the coordinator iterates its entries for times up to `now + PRED_ARCHIVE_HORIZON_HOURS` (default: 4h) and calls `setdefault` — **first write wins, never overwritten**.
- **Read:** when writing chart_log at time T, `_select_pred_indoor(T)` calls `_lookup_pred_archive(T)` (source `archive`); falls back to `_last_predicted_indoor[0]` only on cache miss (warmup period, source `ode-warmup`); otherwise `(None, "none")`. It does not look at the current indoor reading.
- **Expiry:** entries where `key < epoch(now − 7 days)` are purged at each tick. Bounded at ≤ 336 entries at 30-min resolution (7 days × 48 ticks/day).
- **Persistence:** saved with the state file and restored on any restart, including one that comes back on a later day (Issue #1359); entries older than the 7-day expiry are pruned on restore, and the archive is dropped on a temperature-unit change (Issue #1360). With no usable state file the archive starts empty and the warmup after the restart is 4 hours.
- **Log source tag:** `(archive)` = archive hit; `(ode-warmup)` = warmup fallback active; `(none)` = both archive and cache empty.

**30-min resolution:** `_build_predicted_indoor_future()` was extended (Issue #139) to insert a linearly-interpolated midpoint between each consecutive pair of hourly ODE outputs before returning. This gives archive entries 30-min resolution and eliminates the step-function artifact on the historical chart.

**Concrete example (after fix):**

At T=6:00am, ODE seeded from `actual(6am)=68°F`, outdoor forecast rising to 85°F by 2pm:
```
archive[7am]=68.9, archive[7:30am]=69.5, archive[8am]=70.1, ..., archive[10am]=74.2  ← written, locked
```
At T=10:00am tick: `_pred_archive[epoch(10am)]` = **74.2°F** (from 6am ODE, 4h earlier). `indoor_temp` = 72.8°F. Log: `chart_log pred_indoor=74.2 indoor=72.8 delta=+1.4 (archive)` — meaningful divergence.

**Why not `_ode_single_step`? (regression history)**

Prior to Issue #137, `pred_indoor` was a single-step Euler approximation (`_ode_single_step`), which applied only the `k_passive` term over a half-hour window. This produced `pred_indoor ≈ actual_indoor` with delta ~0.38°F. The function was removed in Issue #137. Issue #137 introduced the full-ODE cache (`_last_predicted_indoor`), which diverged meaningfully during HVAC transitions — but the re-seeding problem remained. Issue #139 introduced the first-write-wins archive, making `pred_indoor` reflect a genuine advance prediction. The warmup fallback to `_last_predicted_indoor[0]` is now a temporary state, not the primary path.

### Thermal Model Refresh

Added in Issues #137/#138. On every 30-min coordinator cycle, the coordinator's `_thermal_model` (the only copy since Issue #1443; the engine reads it through its thermal-model accessor) is refreshed before the chart log append:

```python
self._thermal_model = self._learning.get_thermal_model(outdoor_temp_f=outdoor_temp_f, solar_factor=solar_factor)
```

- `get_thermal_model()` is pure computation — no I/O, no disk access. Calling it 48×/day is negligible cost.
- Before this fix: an HA restart left `_thermal_model = {}`, which made the physics gate falsy (`k_passive` absent), and the predicted curve fell back to ramp interpolation producing delta=0.0 for up to 18+ hours (until the next manual restart or observation commit).
- After this fix: any `_thermal_model = {}` state is repaired on the next 30-min cycle.
- Log line emitted on each refresh: `"thermal model refreshed (30-min cycle): confidence=X"`. Use this to confirm the physics gate is open.

### Regression Decision Tree

Use this tree when `pred_indoor ≈ actual_indoor` (delta ≈ 0) on the chart:

```
pred_indoor ≈ actual (delta ≈ 0)?
│
├── Step 1: Check archive source tag in log
│     python tools/ha_logs.py --filter "chart_log pred_indoor"
│     → "(archive)" in log?   → Archive is working. Go to Step 2.
│     → "(ode-warmup)" in log? → HA restarted < 4h ago; archive warming up. Auto-resolves. (Root cause A)
│     → "(none)" in log?       → Archive and ODE cache both empty. Go to Step 3.
│
├── Step 2 (archive active, delta still ≈ 0):
│     → delta=0 during temperature transitions? → outdoor forecast tracked actual closely (Root cause B)
│     → delta=0 all day? → check archive key rounding; see Root cause E
│
└── Step 3 ((none) tag — ODE cache empty):
      python tools/ha_logs.py --filter "thermal model refreshed"
      → confidence=none → fresh install / no observations yet (Root cause C)
      → confidence=solid/moderate → check _build_predicted_indoor_future fallback (Root cause D)
```

**Root causes:**

A. **`(ode-warmup)` in log** — HA restarted < 4h ago; archive warming up. Uses `_last_predicted_indoor[0]` fallback until the archive is populated 4h ahead. Auto-resolves within 4h. Not a bug.

B. **`(archive)` in log but delta ≈ 0 during temperature transitions** — outdoor forecast accuracy issue. The advance prediction (made 4h ago) matched the actual trajectory closely because outdoor conditions tracked the forecast. Rare during pronounced heating/cooling cycles. Not a bug.

C. **Fresh install / no observations** — `confidence=none` in refresh log. No thermal data collected yet. Resolves after 1–2 days of `k_passive` observations.

D. **`(none)` in log — ODE cache empty** — `_last_predicted_indoor` was empty when the chart_log entry was written and the archive had no entry for this slot. Root cause: thermal model empty, physics gate falsy, or indoor temp unavailable at tick time. Check `_build_predicted_indoor_future` log for "using fallback". The #137 thermal model refresh cycle should restore `confidence` within 30 min after restart.

E. **Archive populated but `pred_indoor = indoor` exactly** — potential bug. Check that `_pred_archive_key()` is rounding to the correct 30-min boundary. Check that archive epoch keys match the chart_log entry timestamps.

### `_build_future_forecast_outdoor(hourly_forecast, classification=None)`

Used by `get_chart_data()` to build the `forecast_outdoor` time series for the chart (future region only).

- Iterates `hourly_forecast`; for each entry whose local datetime is at or after `now`, appends `{"ts": local_dt.isoformat(), "temp": round(float(temp), 1)}`.
- Forecast temperatures as held in `hourly_forecast` (internal °F; no curve-normalisation). The caller converts the series to the display unit.
- Covers all available forecast days (typically 2–10+), not just today.

**Fallback (Issue #132):** when `hourly_forecast` is empty or yields no future entries:
- If `classification` is provided: generates a cosine curve using `_cosine_outdoor_curve(classification.today_high, classification.today_low)`. Each hour is projected to the next future occurrence from `now`. Result is sorted by `ts`.
- If `classification` is `None`: returns `[]` (chart future outdoor region is blank).

### `_build_predicted_indoor_future(hourly_forecast, config, now, current_indoor_temp, thermal_model, occupancy_mode, classification=None)`

Used by `get_chart_data()` to build the `predicted_indoor` time series for the chart (future region only).

- Runs a per-hour physics ODE (or setpoint-schedule fallback) over all future forecast hours to produce `{"ts": ISO_str, "temp": float}` entries.
- When `thermal_model` has "low" confidence or above, uses the full ODE with `k_passive`, `k_active_heat`/`k_active_cool`, `k_vent`, `k_solar`. Falls back to setpoint-schedule interpolation otherwise.

**Fallback (Issue #132):** when `hourly_forecast` is empty:
- If `classification` is provided: synthesises a cosine-based hourly list using `_build_outdoor_curve(high=classification.today_high, low=classification.today_low, hourly_forecast=None)`. Future datetimes are assigned hour-by-hour from `now_local`; the resulting synthetic list is used as `hourly_forecast` and the function proceeds normally.
- If `classification` is `None`: logs a debug message and returns `[]`.

**Important distinction:** the cosine fallback in `_build_future_forecast_outdoor` uses `_cosine_outdoor_curve()` (pure cosine, no normalisation). The cosine fallback in `_build_predicted_indoor_future` uses `_build_outdoor_curve(hourly_forecast=None)`, which returns a cosine via `_cosine_outdoor_curve()` as well (same underlying function; no hourly data to blend when `hourly_forecast` is None).

### Frontend "Now" cutoff (Issue #132)

The frontend enforces a strict cutoff at the current time:

- **Past predicted** (`pred_outdoor`, `pred_indoor` in chart log snapshots): only chart log entries with `ts ≤ now` are used.
- **Future predicted** (`forecast_outdoor`, `predicted_indoor` from `get_chart_data()`): only entries with `ts > now` are rendered.

This prevents the prior spike artifact where normalised values from past snapshots were blended with raw future forecast values across the classification boundary.

## Chart Forecast Horizon Extension (Issue #906)

**Problem:** the chart's forward-looking series (`predicted_indoor`, `forecast_outdoor`, Target Band, activity bars) are all ultimately sourced from `self._hourly_forecast_temps`, which comes from the configured weather integration's *hourly* forecast. Many weather integrations — met.no (this project's primary tested integration) among them — return only a short native hourly horizon (~48 hours for met.no). Selecting a 7d/30d chart range drew real historical data back that far, but every forward-looking series still stopped at ~2 days, making the chart look broken/stale for any multi-day view.

**Fix:** `get_chart_data()` computes a chart-only extended hourly forecast via `_build_extended_hourly_forecast(hourly_forecast, daily_forecast, now, unit)`. For each calendar day beyond the last real hourly entry that the weather integration's longer-range **daily** forecast covers (met.no's `daily` forecast typically returns several days further out than its `hourly` forecast), it synthesizes 24 hourly entries via `_synthesize_hourly_day(high, low, day_date)` — a cosine curve anchored to that day's forecast high/low, the same shape already used elsewhere in this codebase for cosine-based outdoor curves. Each synthetic entry is tagged `"synthetic": True` in the underlying dict so it remains distinguishable from a real hourly-sourced entry if a future feature (e.g. visually de-emphasizing lower-confidence days) needs to tell them apart; no such distinction is currently rendered by the frontend.

**Chart-display-only — deliberately not fed into automation:** the extended list is computed on demand inside `get_chart_data()` and is never assigned back onto `self._hourly_forecast_temps` or `self.automation_engine._hourly_forecast_temps`. Every live control decision — the ceiling guard, the nat-vent gate, and the nat-vent plan/cutoff window — continues to read only the real (unextended) hourly forecast. This is intentional: a multi-day cosine extrapolation from a daily high/low is materially lower-confidence than the real hourly forecast, and letting it influence real HVAC control would let that low-confidence data quietly drive setpoint decisions. The extension exists solely so the dashboard's chart doesn't visually truncate at the weather integration's short hourly horizon.

**Fails safe on unexpected data shapes:** `_build_extended_hourly_forecast()` checks the daily forecast's median entry spacing before treating it as day-spaced data (≥20 hours apart). If the spacing looks hourly instead — which can happen if the weather integration falls back to its deprecated non-typed `forecast` attribute, which isn't guaranteed to be day-shaped — the function returns the real hourly list unchanged (no synthetic entries added) and logs at DEBUG, rather than fabricating incorrect synthetic days from mis-shaped input.

**Not in scope for this feature:** any frontend visual distinction between real and synthetic chart days (e.g. dashed/lighter rendering for synthetic-day segments). The `"synthetic": True` tag makes this possible to add later without further backend rework, but no such styling change ships as part of Issue #906.
