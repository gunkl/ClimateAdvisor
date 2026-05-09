<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [chart_log.py](../custom_components/climate_advisor/chart_log.py) | ↔ [State Persistence](state-persistence.md) [Thermal Model v3](thermal-model-v3-spec.md) -->

# Chart State Log — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Is retention time-based or count-based, and what is the cap? | Time-based: entries older than `max_days` (default 365) are evicted. At ~30-minute cadence that is ~17,520 entries. There is no separate count cap. | [Retention Model](#retention-model) |
| What does `append()` guarantee about entry ordering and eviction timing? | Entries are appended in call order; timestamps are NOT re-sorted. Pruning runs at most once per hour — the buffer may transiently exceed the window between prune passes. | [Append Contract](#append-contract) |
| What does `get_entries()` return when the log is empty or the range produces no matches? | An empty list `[]`. No error is raised. Downsampling buckets that receive zero entries simply produce no output rows. | [Query Contract](#query-contract) |
| What downsampling tier applies for each `range_str` value? | `6h`/`12h`/`24h`/`3d` → raw entries. `7d`/`30d` → hourly averages. `1y` → daily summaries. An unrecognised range string defaults to `24h` (1-day raw). | [Downsampling Rules](#downsampling-rules) |
| What fields does a raw entry always carry, and which are optional? | Nine core fields always present: `ts`, `hvac`, `fan`, `indoor`, `outdoor`, `windows_open`, `windows_recommended`, `pred_outdoor`, `pred_indoor`. The `event` field is only present when the marker argument is non-None. | [Entry Schema](#entry-schema) |
| What happens when `load()` finds a missing, corrupt, or structurally wrong file? | Any of these three error paths resets `_entries` to `[]` and logs a WARNING. The coordinator continues with an empty log; no exception propagates to the caller. | [Load Contract](#load-contract) |
| What is the atomic write contract for `save()`? | Serializes to a temp file in the same directory, then `os.replace()` into the final path. On non-Windows the final file is `chmod`-ed to `0o600`. The original file is never touched until `os.replace()` succeeds. | [Save / Persistence Contract](#save--persistence-contract) |
| How do daily summary buckets differ from raw and hourly entries in their field names? | Daily summaries use `indoor_avg`/`indoor_min`/`indoor_max`, `outdoor_avg`/`outdoor_min`/`outdoor_max`, `fan_minutes`, and `events` (plural list). Raw and hourly entries use `indoor`, `outdoor`, `fan` (bool), and `event` (singular string). | [Downsampled Entry Schemas](#downsampled-entry-schemas) |

## Scope

This spec covers the `ChartStateLog` class in its entirety: construction, `load()`, `save()`, `append()`, `_maybe_prune()`, `get_entries()`, and the three internal bucketing helpers.

- **File:** `custom_components/climate_advisor/chart_log.py`
- **Approximate line range:** L41 – L318 (full class)
- **Entry point:** `ChartStateLog.__init__()` for construction; `append()` and `get_entries()` for runtime use

**Out of scope:**

- When the coordinator calls `append()` (wiring lives in `coordinator.py`)
- How `get_chart_data()` in `coordinator.py` assembles the full dashboard payload from `get_entries()` output, target-band schedule, and prediction curve — see [Architecture Reference §Coordinator Chart Helper Functions](02-ARCHITECTURE-REFERENCE.md#coordinator-chart-helper-functions)
- State persistence for the coordinator's own state — see [State Persistence](state-persistence.md)
- Thermal model parameters referenced in `pred_indoor`/`pred_outdoor` values — see [Thermal Model v3](thermal-model-v3-spec.md)

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
| `fan` | `bool` | Always | Whether the fan is active |
| `indoor` | `float \| null` | Always | Indoor temperature in the user's configured unit; `null` if unavailable |
| `outdoor` | `float \| null` | Always | Outdoor temperature; `null` if unavailable |
| `windows_open` | `bool` | Always | Whether any window/door sensor reports open |
| `windows_recommended` | `bool` | Always | Whether natural ventilation was recommended at this tick |
| `pred_outdoor` | `float \| null` | Always | Forecast outdoor temperature for this timestamp; `null` if not computed |
| `pred_indoor` | `float \| null` | Always | Predicted indoor temperature for this timestamp; `null` if not computed |
| `event` | `str` | Optional | Event marker label (e.g., `"hvac_mode_changed"`, `"windows_opened"`). Present only when `event` argument was non-None. |

**Invariant:** `ts` is always present and always a string. Monotonic non-decrease of `ts` values is the caller's responsibility — the class does not enforce it.

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

`get_entries(range_str: str = "24h") -> list[dict[str, Any]]`

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
