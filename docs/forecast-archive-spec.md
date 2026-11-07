<!-- Nav: ← [Forecast Pipeline](forecast-pipeline-spec.md) | → [forecast_archive.py](../custom_components/climate_advisor/forecast_archive.py) | ↔ [Chart State Log](chart-log-spec.md) [State Persistence](state-persistence.md) [Thermal Model v3](thermal-model-v3-spec.md) -->

# Forecast Archive — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the archive record, and why? | One snapshot per local clock hour of what the provider's hourly forecast said (up to 72 h ahead), so forecast error can be scored at each lead time (1 h, 4 h, 12 h, 24 h...). chart_log `pred_outdoor` keeps only the current hour's value and cannot answer that. No automation decision, display or REST view reads it directly; its readers are training inputs: the v4 fit (`v4_shadow.run_fit`, forecast-correction learning, via `snapshots_fn=get_snapshots`), the nightly night-strategy replay (`night_replay.run_nightly`) and the offline `tools/predict_backtest.py` (raw file). A v4 PRIMARY then drives decisions from the model it learned (see [Prediction Engines Spec](prediction-engines-spec.md)). | [Scope](#scope) |
| When is a snapshot recorded, and when is it not? | Only on a successful fetch that returned entries, from the single choke point `_refresh_hourly_forecast()`. A kept-forecast reuse during an outage and the chart's synthetic extended days are never recorded. | [Record Rules](#record-rules), [Outage Behavior](#outage-behavior) |
| What if the forecast is fetched several times in one hour? | The latest fetch in a local clock hour REPLACES that hour's snapshot; a refetch with identical content (same source, grid start and temperatures, at any minute of the hour) changes nothing, keeps the earlier issue time and triggers no write. | [Record Rules](#record-rules) |
| What is on disk, and how big does it get? | `climate_advisor_forecast_archive_<entry_id>.json`, `{"snapshots": [{"i","s","h0","t":[...]}]}`, one hourly grid per snapshot; 365 days at 72 h is about 3.9 MB. | [On-Disk Schema](#on-disk-schema), [Retention and Size](#retention-and-size) |
| How do the v4 fit and the backtest read it? | `ForecastArchive.get_snapshots(start, end)` returns snapshots issued in `[start, end)`, ascending, as `{"issued","source","hours":[[iso_hour, temp_f], ...]}`; never raises. | [R1 Consumer Contract](#r1-consumer-contract) |
| Can the archive break a coordinator cycle or block the event loop? | No. `record()` and `get_snapshots()` never raise; the coordinator also wraps the call; `load()`/`save()` run via the executor and are registered in `_BLOCKING_METHODS`. | [Failure Handling](#error-conditions) |
| What temperature unit is stored? | Internal °F, 1 decimal, verbatim. The list is already normalised at its source (Issue #1015); the archive never converts. | [Invariants](#invariants) |

## Scope

The persisted hourly-forecast snapshot archive added by Issue #1230 R0 and its coordinator wiring.

- **File:** `custom_components/climate_advisor/forecast_archive.py` (class `ForecastArchive`); constants in `const.py` (`FORECAST_ARCHIVE_FILE`, `FORECAST_ARCHIVE_HORIZON_HOURS = 72`, `FORECAST_ARCHIVE_MAX_DAYS = 365`)
- **Coordinator wiring:** construction (`coordinator.py` `__init__`), `async_restore_state` load, `_refresh_hourly_forecast()` → `_record_forecast_archive()`; readers `_v4_run_fit` / the night replay pass `snapshots_fn=self._forecast_archive.get_snapshots`
- **Entry points:** `ForecastArchive.record()`, `.get_snapshots()`, `.load()`, `.save()`

**Out of scope:**

- Fetching, parsing and unit-normalising the forecast, and the outage keep/expire logic - see [Forecast Pipeline](forecast-pipeline-spec.md#weather-source-outage) and [Temperature Conversion](temperature-conversion.md)
- The backtest tool itself (`tools/predict_backtest.py`, which reads the raw file into the same read shape; see [Debugging Guide](09-DEBUGGING-GUIDE.md#prediction-backtest-toolspredict_backtestpy-issue-1230-r1)) and the v4 fit that consumes snapshots ([v4 Shadow Spec](v4-shadow-spec.md))
- The chart's synthetic extended forecast days - see [Chart Log Spec](chart-log-spec.md#chart-forecast-horizon-extension-issue-906)

Persistence mirrors `ChartStateLog` ([Chart Log Spec](chart-log-spec.md)): atomic tempfile + `os.replace`, `0o600`, time-based retention, prune at most once per hour, no public method raises.

## Pre-conditions

1. `ForecastArchive.__init__` has run (the coordinator builds it in `__init__`, entry-scoped by `entry_id`).
2. `load()` has been called once before the first `record()` in normal operation (coordinator `async_restore_state`); `record()` on an unloaded archive still works on an empty list.
3. `record(issued, source, hourly)`: `hourly` is a list of `(forecast_hour, temp_F)` pairs already in internal °F. A naive `issued` is read as UTC; a naive forecast hour is read in the issue time's timezone.
4. `load()` and `save()` are blocking file I/O and MUST be run through the executor (never directly on the event loop).
5. `get_snapshots(start, end)`: naive bounds are read as UTC.

## Post-conditions

1. After `record()` returns `True`, the in-memory list holds the new (or replaced) snapshot as its last item; nothing has been written to disk. The caller saves.
2. `record()` returns `False` (archive unchanged) when no usable point remains, a snapshot with identical content (`s`, `h0`, `t`; the issue minute is ignored) is already held for that hour, or an unexpected error occurred.
3. After `save()` without an `OSError`: the file holds `{"snapshots": [...]}` in compact JSON, permissions `0o600` on non-Windows, no `.tmp` left. On failure the in-memory list and the original file are unchanged and an ERROR is logged.
4. After `load()`: `_snapshots` holds only structurally valid snapshots issued within `max_days`, sorted ascending by issue time; any file-level problem leaves it `[]`.
5. `get_snapshots()` returns a list (possibly `[]`) ascending by issue time, each item decoded to the read shape.

## Invariants

1. **No direct decision reader:** no automation decision, sensor, REST view or frontend reads the archive; only the v4 fit, the nightly night replay (training inputs, in the executor) and the offline backtest do.
2. **°F verbatim:** values are `round(temp, 1)` of what the caller passed; the module never converts units. A Celsius install stores °F (the list is normalised at `_get_hourly_forecast_data()`).
3. **Real hours only:** entries flagged `synthetic` are filtered out by `_record_forecast_archive()`; no synthetic day, cosine curve or placeholder is ever stored, and a gap hour is `null`, never invented.
4. **One snapshot per local clock hour:** the latest issue in the hour wins when its content differs; an unchanged refetch keeps the earlier issue time. Replacement is checked against the most recent stored snapshot (issue times arrive in order). Equality uses aware datetimes so a DST fall-back repeat hour stays distinct from its first occurrence.
5. **Horizon `[floor_hour(issued), +72 h)`:** lead 0 is the hour containing the issue time; earlier hours are history and dropped; at most 72 hourly points (exclusive upper bound).
6. **Atomic write isolation:** the original file is untouched until `os.replace()` succeeds; `save()` serialises a `list()` copy so the executor thread never iterates the list the event loop appends to.
7. **Never breaks a cycle:** no public method raises, and `_refresh_hourly_forecast()` additionally wraps `_record_forecast_archive()` in try/except (WARNING). The forecast is installed on the coordinator and engine *before* the archive runs.

## Record Rules

Applied in order by `_record()`:

1. Keep only finite temperatures whose (aware) hour is in `[floor_hour(issued), floor_hour(issued) + 72 h)`; later duplicates of the same hour overwrite earlier ones. No points left → return `False`.
2. `h0` = the earliest kept hour (minute precision). Each hour is placed at index `round((hour − h0) / 1 h)`; an entry more than 60 s off the hourly grid (e.g. a 90-minute entry) is skipped and counted in the DEBUG line, never interpolated. No grid points left → `False`.
3. `t` = `[grid.get(i) for i in range(max_index + 1)]`: `null` for a missing hour inside the span.
4. If the last stored snapshot was issued in the same local clock hour as `issued`: same content as the new encoding (`s`, `h0` and `t` equal; `i` is not compared, so the earlier issue time is kept) → return `False` (no write); otherwise replace it, so the latest changed forecast in the hour wins. Else append.
5. After a change, `_maybe_prune()` (at most once per hour) evicts snapshots older than `max_days`. A DEBUG line is logged per recorded snapshot.

The coordinator then saves only when `record()` returned `True`: `await self._executor_job(archive.save)`.

## Outage Behavior

During a weather outage `_get_hourly_forecast_data()` returns nothing and `_refresh_hourly_forecast()` reuses the kept forecast (Issue #1337). That branch never calls the archive: re-recording kept data under a new issue time would stamp stale forecasts as fresh and corrupt lead-time error scoring. The archive therefore has a gap for the outage, and no synthetic day is ever added. On recovery the next successful fetch records normally. See [Forecast Pipeline - Kept forecast](forecast-pipeline-spec.md#kept-forecast-issue-1337).

## On-Disk Schema

File: `climate_advisor_forecast_archive_<entry_id>.json` in the HA config directory (via `resolve_entry_scoped_path`; with an empty `entry_id` the unscoped `climate_advisor_forecast_archive.json`). A new file, so there is no legacy file to migrate.

```json
{"snapshots": [
  {"i": "2026-11-06T14:05-07:00",
   "s": "weather.home",
   "h0": "2026-11-06T14:00-07:00",
   "t": [54.3, 53.1, null, 51.8]}
]}
```

| Key | Meaning |
|---|---|
| `i` | Issued: when the fetch happened (`dt_util.now()`), minute precision, local offset |
| `s` | Source: the configured `weather_entity` id |
| `h0` | First forecast hour of the grid, minute precision |
| `t` | °F (1 decimal) for `h0 + n hours`; `null` = no provider entry for that hour |

Decoded read shape (see below): `{"issued": "2026-11-06T14:05-07:00", "source": "weather.home", "hours": [["2026-11-06T14:00-07:00", 54.3], ["2026-11-06T15:00-07:00", 53.1], ["2026-11-06T17:00-07:00", 51.8]]}`. Null hours are omitted from `hours`.

## Retention and Size

- Time-based: `FORECAST_ARCHIVE_MAX_DAYS = 365`. No count cap. `load()` drops expired and structurally invalid snapshots (non-dict, missing `s`/`t`/`h0`/`i`, unparseable times); `_maybe_prune()` runs at most once per hour after a change.
- Cadence: at most one snapshot per hour, so up to 8,760 per year.
- Size: the compact grid keeps a full year at 72 h near 3.9 MB (the unit test bounds it under 10 MB). Writing one timestamp per hour instead would be several times larger.
- Each change rewrites the whole file (executor); a same-hour refetch with identical content writes nothing.

## Error Conditions

| Failure | Handling | Caller receives |
|---|---|---|
| File missing | INFO "no archive file yet", `_snapshots = []` | Empty archive |
| `OSError` / JSON decode error on read | WARNING, `[]` | Empty archive |
| Top-level not a dict, or `snapshots` missing/not a list | WARNING, `[]` | Empty archive |
| Individual snapshot invalid or expired | Dropped (DEBUG count) | Remaining kept |
| Any other exception in `load()` | WARNING with traceback, `[]` | Empty archive |
| `json.dumps` raises | ERROR, return; no `.tmp`, file unchanged | `None` |
| `OSError` on tempfile write or `os.replace` | ERROR, `.tmp` unlinked, file unchanged | `None` |
| Exception inside `record()` | WARNING with traceback | `False` |
| Exception in `_record_forecast_archive()` (incl. executor save failure) | WARNING in `_refresh_hourly_forecast()` | Cycle continues; forecast already installed |
| Exception inside `get_snapshots()` | WARNING | `[]` |

## R1 Consumer Contract

`get_snapshots(start: datetime, end: datetime) -> list[dict]`

- Snapshots whose issue time is in `[start, end)` (half-open), ascending by issue time. Naive bounds are read as UTC.
- Each item: `{"issued": iso_str, "source": str, "hours": [[iso_hour_str, temp_f], ...]}`. `issued` and hour strings are ISO-8601 with minute precision and the local offset in force at recording. `temp_f` is a float in internal °F (1 dp). Hours with no provider entry are absent.
- The lead time of an hour is `hour − issued`; the lead-0 hour is the hour containing `issued`.
- Reads are from the in-memory list (loaded at startup). An offline tool without a coordinator constructs `ForecastArchive(config_dir, entry_id=...)` and calls `load()` first. The method is a pure read and safe to call from any thread (it iterates a `list()` copy).
- Scoring against actual weather uses the `outdoor` series in the chart log ([Chart Log Spec](chart-log-spec.md)); this module does not provide actuals.

## Known Gaps

- **Zone removal (fixed in 0.9.1):** deleting a config entry deletes `climate_advisor_forecast_archive_<entry_id>.json` with the zone's other files (`async_remove_entry` → `storage_paths.remove_entry_scoped_files`, base name listed in `ENTRY_SCOPED_FILES`). A reload or unload keeps it. See [State Persistence](state-persistence.md#zone-removal-cleanup).

## Code Reference

- [`ForecastArchive`](../custom_components/climate_advisor/forecast_archive.py#L86) - class
- [`ForecastArchive.load`](../custom_components/climate_advisor/forecast_archive.py#L111) / [`_load`](../custom_components/climate_advisor/forecast_archive.py#L119) / [`_valid_issued`](../custom_components/climate_advisor/forecast_archive.py#L163)
- [`ForecastArchive.save`](../custom_components/climate_advisor/forecast_archive.py#L173) - atomic write
- [`ForecastArchive.record`](../custom_components/climate_advisor/forecast_archive.py#L203) / [`_record`](../custom_components/climate_advisor/forecast_archive.py#L216) - snapshot encoding and replace rule
- [`ForecastArchive._maybe_prune`](../custom_components/climate_advisor/forecast_archive.py#L282) - hourly-gated retention
- [`ForecastArchive.get_snapshots`](../custom_components/climate_advisor/forecast_archive.py#L298) / [`_decode`](../custom_components/climate_advisor/forecast_archive.py#L318) - R1 read path
- [`const.py`](../custom_components/climate_advisor/const.py) - `FORECAST_ARCHIVE_FILE`, `_HORIZON_HOURS`, `_MAX_DAYS`
- [`ClimateAdvisorCoordinator._refresh_hourly_forecast`](../custom_components/climate_advisor/coordinator.py#L5536) - single choke point
- [`ClimateAdvisorCoordinator._record_forecast_archive`](../custom_components/climate_advisor/coordinator.py#L5564) - filter synthetic, parse, record, save via executor
- `tests/test_forecast_archive.py` - unit and coordinator-wiring tests; `tests/test_executor_offload.py` (`_BLOCKING_METHODS`)
