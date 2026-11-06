<!-- Nav: ← [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md) | → [state.py](../custom_components/climate_advisor/state.py) -->

# State Persistence — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What file does operational state persist to, and where is the path defined? | `climate_advisor_state.json` in the HA config root; path assembled from `STATE_FILE` constant in `const.py`. | [JSON Serialization](#json-serialization) |
| How does state survive HA restarts without corruption from a mid-write crash? | Atomic write: serialize to a uniquely-named `.tmp` file in the same directory, then `os.replace()` to rename it over the target. The original is never modified until the new file is complete. | [Atomic Write Pattern](#atomic-write-pattern) |
| What happens to a zone's files when the zone is deleted, reloaded or unloaded? | Deleted: `async_remove_entry` (`__init__.py`) deletes the zone's state, learning, chart log and forecast archive files and its `weather_entity_not_found_<id>` / `reload_needed_<id>` Repairs issues. Reloaded or unloaded: nothing is deleted. | [Zone removal cleanup](#zone-removal-cleanup) |
| What happens when the state file has a mismatched version or is malformed? | `load()` logs a WARNING and returns an empty dict — the coordinator starts fresh rather than attempting partial migration. | [Version Guard](#version-guard) |
| Where does LearningState (thermal model, records, suggestions) persist, and is that this module's responsibility? | `LearningState` persists to `climate_advisor_learning.json` and is owned entirely by `LearningEngine` in `learning.py`. `state.py` does not touch that file. | [Scope](#scope) |
| What is STATE_VERSION and when does it change? | Currently `1`. It is compared on load; any mismatch discards the file and starts fresh. It must be incremented whenever the state dict schema changes in a breaking way. | [Version Guard](#version-guard) |
| What is the `temp_unit` key in the state file and what does it do? | A top-level key (config unit at save time) written by the coordinator's `_build_state_dict`. On same-day restore, a differing string value discards temp history, `pred_archive` and `classification`; a missing key changes nothing. Older loaders ignore it (checks `version` only). | [temp_unit key and same-day restore rule](#temp_unit-key-and-same-day-restore-rule-issue-1015) |

## Scope

**Owns:**
- `StatePersistence` class: atomic load and save of the coordinator's operational state dict to `climate_advisor_state.json`
- Temp-file lifecycle: creation, rename, and cleanup of `climate_advisor_state_*.tmp` files
- Version guard on load: reject files whose `version` field does not match `STATE_VERSION`
- `delete()`: removes the state file and any leftover temp files. **Not called anywhere in production** (an earlier version of this page said `async_unload_entry` called it; it never did). Zone removal uses `storage_paths.remove_entry_scoped_files()` instead, because `delete()`'s `climate_advisor_state_*.tmp` glob would also remove another zone's in-flight temp file — see [Zone removal cleanup](#zone-removal-cleanup)

**Explicitly does NOT own:**
- `LearningState` — lives in `learning.py`; persists to `climate_advisor_learning.json` via `LearningEngine`
- The chart log (`climate_advisor_chart_log_<entry_id>.json`, [chart-log-spec.md](chart-log-spec.md)) and the forecast archive (`climate_advisor_forecast_archive_<entry_id>.json`, Issue #1230 R0, [forecast-archive-spec.md](forecast-archive-spec.md)) - separate entry-scoped files with their own persistence contracts. Both are deleted with the zone's other files when the config entry is deleted ([Zone removal cleanup](#zone-removal-cleanup))
- The schema of the state dict — the coordinator (`coordinator.py`) is responsible for what keys are written and read
- Nothing else — file permissions (`0o600` via `os.chmod` after `os.replace()`, guarded by `hasattr(os, "chmod")` for cross-platform safety) ARE set by this module; see Invariants
- Migration logic — version mismatches are handled by discarding and starting fresh; no field-level upgrade path exists in `state.py`

## Responsibilities

- Provide `load() -> dict`: read `climate_advisor_state.json`, validate it is a JSON object, enforce `STATE_VERSION`, return `{}` on any failure
- Provide `save(state: dict)`: inject `version`, serialize to JSON, write atomically via `.tmp` + `os.replace()`
- Provide `delete()`: remove the state file and any orphaned `.tmp` files from the same directory
- Log `WARNING` for non-fatal failures (corrupt file, wrong version) and `ERROR` for write failures

## Interfaces

```python
class StatePersistence:
    def __init__(self, config_dir: Path) -> None: ...

    def load(self) -> dict[str, Any]:
        """Load state from disk. Returns empty dict on missing or corrupt file."""

    def save(self, state: dict[str, Any]) -> None:
        """Write state to disk atomically. Mutates state in place to add 'version'."""

    def delete(self) -> None:
        """Remove state file and any leftover .tmp files."""
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `StatePersistence.__init__` | `ClimateAdvisorCoordinator.__init__` | Constructed once with the HA config directory path |
| `StatePersistence.load` | `_async_save_state` (coordinator, on startup) | Restore coordinator state after HA restart |
| `StatePersistence.save` | `_async_save_state` (coordinator, periodic + on change) | Persist operational state to survive restarts |
| `StatePersistence.delete` | _(none — not called in production)_ | Zone removal goes through `remove_entry_scoped_files()` instead |

## Zone removal cleanup

Added in 0.9.1 (Issue #1230 R0 follow-up). Occupant view: deleting a zone leaves nothing of it behind: no state,
learning, chart log or forecast archive file in the config dir (several MB per zone on an SD card), and no Repairs
card offering to "fix" a zone that no longer exists. Before 0.9.1 every one of these stayed forever.

- **When:** only `async_remove_entry` (`__init__.py`), which HA calls on deletion, after `async_unload_entry` has
  run the final save. A reload or settings save (unload + setup) never deletes anything.
- **Files:** `storage_paths.remove_entry_scoped_files(config_dir, entry_id)`, run in the executor, deletes the
  entry-scoped name of each base in `storage_paths.ENTRY_SCOPED_FILES` (`STATE_FILE`, `CHART_LOG_FILE`,
  `LEARNING_DB_FILE`, `FORECAST_ARCHIVE_FILE`). Never another zone's file, never a legacy unscoped file (it may still
  belong to an entry that has not migrated), and nothing for a falsy `entry_id` (that would resolve to the shared
  unscoped names). Leftover `.tmp` files are not removed: their `mkstemp` names cannot be tied to one zone. A missing
  file is skipped; an `OSError` on one file is logged at WARNING and the rest are still deleted; one INFO line lists
  what was deleted. Never raises into the removal.
- **Repairs:** the zone's two entry-scoped persistent issues, `weather_entity_not_found_<entry_id>` and
  `reload_needed_<entry_id>`, are deleted. Domain-wide issues (`zone_resolution_ambiguous`) keep their own lifecycle
  in `async_unload_entry`.
- **Guard:** `tests/test_zone_removal_cleanup.py::test_every_entry_scoped_file_in_the_package_is_cleaned_up` fails if
  any `resolve_entry_scoped_path()` caller in the package uses a base name missing from `ENTRY_SCOPED_FILES`, so a new
  per-zone file cannot be forgotten. Real HA: `tests_ha/test_zone_removal_cleanup.py` (deletion removes files and
  Repairs issues and keeps other zones' and legacy files; reload and unload keep them).

## Data Structures

`StatePersistence` itself holds no domain data — it is a pure I/O adapter. The state dict it serializes is assembled by the coordinator. Its on-disk format is:

```json
{
  "version": 1,
  "<coordinator-key>": "<value>",
  "..."
}
```

**File:** `{hass.config.config_dir}/climate_advisor_state.json`
**Constant:** `STATE_FILE = "climate_advisor_state.json"` (defined in `const.py`)
**Format:** UTF-8 JSON, 2-space indent
**Serialization fallback:** `default=str` — non-serializable values (e.g., `datetime`) are coerced to strings rather than raising

### Version Guard

`STATE_VERSION = 1` (module-level constant in `state.py`). On `load()`:
- If the file does not exist: return `{}`
- If the JSON is not a dict: log WARNING, return `{}`
- If `data["version"] != STATE_VERSION`: log WARNING with both versions, return `{}`

There is no migration path in `state.py`. Any breaking schema change requires bumping `STATE_VERSION` and accepting that existing state is discarded on first load.

### Atomic Write Pattern

`save()` sequence:
1. Inject `state["version"] = STATE_VERSION`
2. Serialize to JSON string (fail fast: log ERROR and return if serialization fails)
3. `tempfile.mkstemp(dir=same_dir, prefix="climate_advisor_state_", suffix=".tmp")` — unique temp file in the same directory ensures `os.replace()` is atomic on POSIX (same filesystem)
4. Write serialized JSON to the temp fd via `os.fdopen`
5. `os.replace(tmp_path, state_path)` — atomic rename; original file untouched until this line succeeds
6. On `OSError` at step 4 or 5: log ERROR, attempt `os.unlink(tmp_path)` (suppressed if that also fails)

### `temp_unit` key and same-day restore rule (Issue #1015)

The schema belongs to the coordinator, but one key has restore-time semantics worth recording here. `_build_state_dict` adds a top-level `"temp_unit"` (the configured unit, `"fahrenheit"` or `"celsius"`, at save time). The options flow does **not** reload the integration (`_commit_section()` only writes the config entry and raises a `reload_needed` Repairs issue, Issue #573); the new unit applies after a Repairs "Fix", a manual reload or an HA restart. The running coordinator's config is fixed for its lifetime and no save path writes a new unit first, so the saved `temp_unit` is always the unit the coordinator was *running with* (the old unit when a change is pending). On a reload `async_shutdown()` saves it; on an HA restart `async_shutdown()` is not called (the STOP listener only saves learning diagnostics), so the file holds the last periodic save, in the same old unit.

On same-day restore (`async_restore_state`), the coordinator computes one boolean `unit_changed` = `saved_unit` is a `str` **and** differs from the current configured unit, and applies it to three restores:

| Section | Restored when unit unchanged | When `unit_changed` |
|---|---|---|
| `temp_history` (outdoor + indoor) | yes | **dropped** (may include samples taken while the configured unit disagreed with the provider's) |
| `pred_archive` | yes | **dropped** |
| `classification` | yes (bridge until first cycle) | **dropped** (recomputed on first cycle) |
| learning `pending_observations` | yes | **dropped** (an observation spanning the switch would mix units; other learning data and the legacy `pending_thermal_event` field untouched) |
| `today_record`, briefing, `automation_state`, occupancy, flags | yes | **kept** |

The restore converts nothing (history is internal °F); the discard rule fires on any unit change and cannot tell bad samples from good, so it also drops good pre-switch samples (harmless; buffers refill on the next polls).

A WARNING and a `unit_changed` event (persisted event log) are emitted. WARNING: `Temperature unit changed since last run: same-day readings discarded from=<saved_unit> to=<current_unit> dropped_outdoor=<n> dropped_indoor=<n> dropped_pred_archive=<n> dropped_classification=<True|False> dropped_pending_observations=<n>`. Event payload: `from`, `to`, `dropped_outdoor`, `dropped_indoor`, `dropped_pred_archive`, `dropped_classification`, `dropped_pending_observations`. A missing or non-string `temp_unit` is a no-op, so existing installs and the first upgrade to 0.7.83 behave as before (a unit change on that same restart is not detected). `STATE_VERSION` is unchanged: the key is additive, the loader checks only `version`, and older code ignores it, so the change is **downgrade-safe**. Not repaired: chart-log entries and learning/thermal records written while the unit was wrong.

See [temperature-conversion.md](temperature-conversion.md#unit-change-handling-issue-1015).

### Persisted end instants are UTC (Issue #1199)

`automation_state.grace_end_time`, `fan_off_hold_until` and `fan_override_until` are ISO-8601 strings of **UTC** instants (`2026-11-01T09:01:30+00:00`), written from datetimes built by `time_utils.instant_after()`. Before #1199 they were local wall-clock plus a timedelta with a local offset (`2026-11-01T02:01:30-08:00`), which is one hour wrong when the span crosses a daylight-saving change. Restore reads either form with `datetime.fromisoformat()` (a `fan_off_hold_until` / `fan_override_until` string with no offset is read as UTC; `grace_end_time` has always been written with an offset and gets no such fallback), computes the remaining time against `dt_util.now()` as a true instant, and re-arms through the normal grace start, so the new string is UTC. A restart that restores a string written by the old code is wrong by at most the original one-hour DST error, once; no migration is needed. Displays convert with `dt_util.as_local()`.

## Invariants

1. **The original state file is never partially overwritten.** All writes go to a `.tmp` file; only `os.replace()` makes the new content visible.
2. **`load()` never raises.** All `json.JSONDecodeError` and `OSError` exceptions are caught; the method always returns a `dict`.
3. **`save()` never raises.** All exceptions are caught and logged; callers do not need try/except around `save()`.
4. **Version is always written by `save()`, never by the caller.** The coordinator passes a state dict without a `version` key; `save()` injects it.
5. **No leftover `.tmp` files after `delete()`.** `delete()` globs for `climate_advisor_state_*.tmp` and removes all matches.
6. **File permissions are set.** After `os.replace()`, `save()` calls `os.chmod(str(self._path), 0o600)` (guarded by `hasattr(os, "chmod")`, so it's a no-op on platforms without it, e.g. Windows) — satisfying the CLAUDE.md security rule requiring `0o600` on persisted state files. Fixed in Issue #384 (v0.4.53); this section previously described a gap that no longer exists — verified directly against `state.py` during the 2026-09-16 documentation staleness audit.
7. **Additive coordinator keys do not bump `STATE_VERSION`.** `temp_unit` (Issue #1015) is read defensively (type-checked, missing = no-op) so older and newer code can share one file.

### Not persisted: the memory of CA's own setpoint writes (Issue #1232)

`AutomationEngine._own_setpoint_writes` (entity, attribute, value, time, Context id of each setpoint CA wrote, kept 180 s so a late thermostat echo is recognised) is deliberately transient. It lives 180 s and is only about to be compared with a thermostat report that is itself a reaction to a write made moments ago; a restart ends both, and the startup coalescing window already suppresses override detection for the first 5 minutes after a restart (Issue #1235). Persisting it would add a state-file field, a version question and a stale-entry risk for no occupant benefit. No `STATE_VERSION` change.

## Disclosure Path

← Tier 1 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md) (see "Integration Version" and "Coordinator State Listeners" sections)
→ Tier 3 specs: none yet authored — candidates: `StatePersistence.save` atomic write contract, `StatePersistence.load` version guard contract
↔ Siblings: [docs/learning-engine.md (not yet written)] — `LearningEngine`/`LearningState` is the parallel persistence subsystem for the learning DB
