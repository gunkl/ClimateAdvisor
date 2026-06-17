<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [chart_log.py](../custom_components/climate_advisor/chart_log.py) | ↔ [State Persistence](state-persistence.md) [Thermal Model v3](thermal-model-v3-spec.md) -->

# Chart State Log — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Is retention time-based or count-based, and what is the cap? | Time-based: entries older than `max_days` (default 365) are evicted. At ~30-minute cadence that is ~17,520 entries. There is no separate count cap. | [Retention Model](#retention-model) |
| What does `append()` guarantee about entry ordering and eviction timing? | Entries are appended in call order; timestamps are NOT re-sorted. Pruning runs at most once per hour — the buffer may transiently exceed the window between prune passes. | [Append Contract](#append-contract) |
| What does `get_entries()` return when the log is empty or the range produces no matches? | An empty list `[]`. No error is raised. Downsampling buckets that receive zero entries simply produce no output rows. | [Query Contract](#query-contract) |
| How does `get_entries()` support historical navigation? | Pass `before: datetime` to anchor the query window to that point in time instead of the current clock. The window is then `[before − range_days, before)`. Used by `get_chart_data(before_ts=...)` to serve past data windows. | [Query Contract](#query-contract) |
| What downsampling tier applies for each `range_str` value? | `6h`/`12h`/`24h`/`3d` → raw entries. `7d`/`30d` → hourly averages. `1y` → daily summaries. An unrecognised range string defaults to `24h` (1-day raw). | [Downsampling Rules](#downsampling-rules) |
| What fields does a raw entry always carry, and which are optional? | Nine core fields always present: `ts`, `hvac`, `fan`, `indoor`, `outdoor`, `windows_open`, `windows_recommended`, `pred_outdoor`, `pred_indoor`. The `event` field is only present when the marker argument is non-None. | [Entry Schema](#entry-schema) |
| What two fields were added for the Vent bar (Issue #331), and what do they mean? | `fan_running: bool` — fan is physically on (compressor-class running). `nat_vent_active: bool` — nat-vent session is armed (includes paused-between-cycles). Legacy `fan` field is still emitted for back-compat. | [Vent Bar Fields (Issue #331)](#vent-bar-fields-issue-331) |
| How does the frontend decide the Vent bar color for each entry? | Blue if `fan_running`; green if `nat_vent_active || windows_recommended`; else off. Back-compat: if `fan_running` absent, fall back to legacy `fan` field → blue. | [Vent Bar Fields (Issue #331)](#vent-bar-fields-issue-331) |
| What does the HVAC bar show after Issue #331? | Compressor-only: `heating` (red) / `cooling` (blue). `fan_only` and `off` produce no bar. | [Vent Bar Fields (Issue #331)](#vent-bar-fields-issue-331) |
| How is `pred_outdoor` populated in each chart log entry? | Raw hourly forecast temperature for the current local hour, extracted by `_extract_current_hour_forecast_temp()` — no normalisation. `null` when hourly forecast has no entry for the current hour. | [Coordinator Chart Log Wiring](#coordinator-chart-log-wiring) |
| How is `pred_indoor` populated in each chart log entry? | Read from `_pred_archive` (first-write-wins prediction archive): the value for time T was written ~4 hours before T arrived, producing a genuine advance prediction. Falls back to `_last_predicted_indoor[0]["temp"]` only during the warmup period (first 4h after HA restart). `null` if both archive and cache are empty, or `pred_outdoor` is null. | [First-Write-Wins Prediction Archive](#first-write-wins-prediction-archive) |
| What fallback does `_build_future_forecast_outdoor()` use when hourly forecast is empty? | When hourly forecast is empty or yields no future entries and a `classification` is provided, generates a cosine curve using `classification.today_high`/`classification.today_low`. Returns `[]` only if no classification is provided. | [Coordinator Chart Log Wiring](#coordinator-chart-log-wiring) |
| What fallback does `_build_predicted_indoor_future()` use when hourly forecast is empty? | When `classification` is provided, synthesises a cosine-based hourly list from `classification.today_high`/`classification.today_low` and proceeds normally. Returns `[]` only if no classification is provided. | [Coordinator Chart Log Wiring](#coordinator-chart-log-wiring) |
| What happens when `load()` finds a missing, corrupt, or structurally wrong file? | Any of these three error paths resets `_entries` to `[]` and logs a WARNING. The coordinator continues with an empty log; no exception propagates to the caller. | [Load Contract](#load-contract) |
| What is the atomic write contract for `save()`? | Serializes to a temp file in the same directory, then `os.replace()` into the final path. On non-Windows the final file is `chmod`-ed to `0o600`. The original file is never touched until `os.replace()` succeeds. | [Save / Persistence Contract](#save--persistence-contract) |
| How do daily summary buckets differ from raw and hourly entries in their field names? | Daily summaries use `indoor_avg`/`indoor_min`/`indoor_max`, `outdoor_avg`/`outdoor_min`/`outdoor_max`, `fan_minutes`, and `events` (plural list). Raw and hourly entries use `indoor`, `outdoor`, `fan` (bool), and `event` (singular string). | [Downsampled Entry Schemas](#downsampled-entry-schemas) |

## Scope

This spec covers the `ChartStateLog` class in its entirety: construction, `load()`, `save()`, `append()`, `_maybe_prune()`, `get_entries()`, and the three internal bucketing helpers.

- **File:** `custom_components/climate_advisor/chart_log.py`
- **Approximate line range:** L41 – L318 (full class)
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
3. The `event` key is present in the new entry if and only if the `event` argument was non-None.
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
- Expected file size: **~2 MB** (uncompressed JSON)
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
| `indoor` | `float \| null` | Always | Indoor temperature in the user's configured unit; `null` if unavailable |
| `outdoor` | `float \| null` | Always | Outdoor temperature; `null` if unavailable |
| `windows_open` | `bool` | Always | Whether any window/door sensor reports open |
| `windows_recommended` | `bool` | Always | Whether natural ventilation was recommended at this tick |
| `pred_outdoor` | `float \| null` | Always | Raw hourly forecast temperature for the current local hour, extracted by `_extract_current_hour_forecast_temp()`. No normalisation is applied. `null` when the hourly forecast has no entry matching the current hour, or when hourly forecast is unavailable. (Fixed in Issue #132: previously stored a normalised value from `_build_outdoor_curve()`, which caused spikes at classification boundaries.) |
| `pred_indoor` | `float \| null` | Always | Predicted indoor temperature: read from `_pred_archive` (first-write-wins archive). The archived value for time T was written when T was ~4 hours in the future — i.e., the prediction was made ~4 hours before T arrived. Falls back to `_last_predicted_indoor[0]["temp"]` only during the warmup period (first 4 hours after HA restart). Only written when `pred_outdoor` is non-null; otherwise `null`. See [First-Write-Wins Prediction Archive](#first-write-wins-prediction-archive). |
| `event` | `str` | Optional | Event marker label (e.g., `"hvac_mode_changed"`, `"windows_opened"`). Present only when `event` argument was non-None. |

**Invariant:** `ts` is always present and always a string. Monotonic non-decrease of `ts` values is the caller's responsibility — the class does not enforce it.

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
    event: str | None = None,
    ts: str | None = None,
) -> None:
```

- If `ts` is `None`, the entry timestamp is set to `dt_util.now().isoformat()` (HA-aware wall clock).
- `event` is the only field that is conditionally included. All other nine fields are always written to the dict, even when their value is `None` or `False`.
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
| `events` | `list[str]` | Collected event marker strings for the day (plural key). Present only when at least one entry had an `event` field. |

**Key schema difference summary:**

| Aspect | Raw | Hourly | Daily |
|---|---|---|---|
| Temperature fields | `indoor`, `outdoor` | `indoor`, `outdoor` (averaged) | `indoor_avg/min/max`, `outdoor_avg/min/max` |
| Fan field | `fan: bool` | `fan: bool` (OR) | `fan_minutes: int` |
| Event field key | `event: str` (optional) | `event: list[str]` (optional) | `events: list[str]` (optional) |
| Prediction fields | `pred_outdoor`, `pred_indoor` | `pred_outdoor`, `pred_indoor` (averaged) | `pred_outdoor_avg`, `pred_indoor_avg` |

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

- [`ChartStateLog.__init__`](../custom_components/climate_advisor/chart_log.py#L44) — construction; sets `_path`, `_max_days`, `_entries`, `_last_prune`
- [`ChartStateLog.load`](../custom_components/climate_advisor/chart_log.py#L55) — disk read, parse, prune on startup
- [`ChartStateLog.save`](../custom_components/climate_advisor/chart_log.py#L95) — atomic write to disk
- [`ChartStateLog.append`](../custom_components/climate_advisor/chart_log.py#L119) — entry construction and in-memory append
- [`ChartStateLog._maybe_prune`](../custom_components/climate_advisor/chart_log.py#L154) — time-gated retention enforcement
- [`ChartStateLog.get_entries`](../custom_components/climate_advisor/chart_log.py#L176) — filtered, downsampled retrieval
- [`ChartStateLog._bucket_hourly`](../custom_components/climate_advisor/chart_log.py#L234) — 1-hour average bucketing
- [`ChartStateLog._bucket_daily`](../custom_components/climate_advisor/chart_log.py#L274) — daily summary bucketing
- [`_parse_ts`](../custom_components/climate_advisor/chart_log.py#L31) — ISO-8601 parse helper; returns `None` on failure

## Coordinator Chart Log Wiring

This section covers the coordinator-side logic that computes `pred_outdoor` and `pred_indoor` before calling `ChartStateLog.append()`, and the helper functions used for the future predicted lines in `get_chart_data()`.

**Covered functions:** `_extract_current_hour_forecast_temp`, `_build_future_forecast_outdoor`, `_build_predicted_indoor_future` — all in `coordinator.py`.

### `pred_outdoor` and `pred_indoor` at append time

Located in `coordinator.py` inside `_async_update_data()` at the chart log append block.

**`pred_outdoor`** is the raw hourly forecast temperature for the current local hour:

```python
_pred_outdoor_val = _extract_current_hour_forecast_temp(self._hourly_forecast_temps, now_dt)
```

- `_extract_current_hour_forecast_temp()` scans `self._hourly_forecast_temps` for an entry whose local datetime matches today's date and the current local hour.
- Returns the raw `temperature` field (rounded to 1 decimal), not a normalised value.
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
- **Read:** when writing chart_log at time T, `_lookup_pred_archive(T)` is called; falls back to `_last_predicted_indoor[0]` only on cache miss (warmup period).
- **Expiry:** entries where `key < epoch(now − 7 days)` are purged at each tick. Bounded at ≤ 336 entries at 30-min resolution (7 days × 48 ticks/day).
- **Persistence:** in-memory only (v1). Warmup after HA restart = 4 hours. Restarts are rare; the 4h warmup period is acceptable.
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

Added in Issues #137/#138. On every 30-min coordinator cycle, `automation_engine._thermal_model` is refreshed before the chart log append:

```python
self.automation_engine._thermal_model = self._learning.get_thermal_model(
    outdoor_temp_f=outdoor_temp_f, solar_factor=solar_factor
)
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
- Raw forecast temperatures — no normalisation.
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
