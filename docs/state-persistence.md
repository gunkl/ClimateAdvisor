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
| What is the `temp_unit` key in the state file and what does it do? | A top-level key (config unit at save time) written by the coordinator's `_build_state_dict`. On any restore (Issue #1360), a differing string value discards pending thermal observations, `kept_forecast` and `pred_archive` (and, on a same-day restore, temp history and `classification`); a missing key changes nothing. Older loaders ignore it (checks `version` only). | [temp_unit key and restore rule](#temp_unit-key-and-restore-rule-issue-1015) |
| What survives a restart that comes back on a later day, e.g. an overnight outage (Issues #1349, #1354-#1360)? | Everything that is not "today's" data: observe-only, `automation_state` (grace/fan/override re-arms), backfill run-once flags, kept forecast, `pred_archive` (pruned to 7 days), the event log, restart-cause classification, investigation reports and the unit-change check. Same-day only: classification, temp history, today's record, briefing state, occupancy mode and away-since. | [Restore across the date boundary](#restore-across-the-date-boundary-issues-1349-1354-1360) |
| Does Vacation/Away already on when HA starts take effect immediately (Issue #1354)? | Yes. `async_setup()` syncs the live occupancy toggles into the engine at once (no 15-minute away grace) and starts counting away minutes; the setback reaches the thermostat through the normal plan application at the end of the startup window. | [Restore across the date boundary](#restore-across-the-date-boundary-issues-1349-1354-1360) |

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

### `temp_unit` key and restore rule (Issue #1015)

The schema belongs to the coordinator, but one key has restore-time semantics worth recording here. `_build_state_dict` adds a top-level `"temp_unit"` (the configured unit, `"fahrenheit"` or `"celsius"`, at save time). The options flow does **not** reload the integration (`_commit_section()` only writes the config entry and raises a `reload_needed` Repairs issue, Issue #573); the new unit applies after a Repairs "Fix", a manual reload or an HA restart. The running coordinator's config is fixed for its lifetime and no save path writes a new unit first, so the saved `temp_unit` is always the unit the coordinator was *running with* (the old unit when a change is pending). On a reload `async_shutdown()` saves it; on an HA restart `async_shutdown()` is not called (the STOP listener only saves learning diagnostics), so the file holds the last periodic save, in the same old unit.

On **any** restore (`async_restore_state`, before the date gate, Issue #1360), the coordinator computes one boolean `unit_changed` = `saved_unit` is a `str` **and** differs from the current configured unit, and applies it to the restores below. Before #1360 the check ran only on a same-day restore, so a unit switch that coincided with an overnight outage let old-unit pending observations, kept forecast and `pred_archive` through. On a cross-day restore only `pending_observations`, `kept_forecast` and `pred_archive` are affected; `temp_history` and `classification` are not restored across days anyway.

| Section | Restored when unit unchanged | When `unit_changed` |
|---|---|---|
| `temp_history` (outdoor + indoor) | yes | **dropped** (may include samples taken while the configured unit disagreed with the provider's) |
| `pred_archive` | yes | **dropped** |
| `kept_forecast` (Issue #1359) | yes | **dropped** |
| `classification` | yes (bridge until first cycle) | **dropped** (recomputed on first cycle) |
| learning `pending_observations` | yes | **dropped** (an observation spanning the switch would mix units; other learning data and the legacy `pending_thermal_event` field untouched) |
| `today_record`, briefing, `automation_state`, occupancy, flags | yes | **kept** |

`temp_history`, `classification` and `today_record` are same-day only, so they exist here only on a same-day restore.

The restore converts nothing (history is internal °F); the discard rule fires on any unit change and cannot tell bad samples from good, so it also drops good pre-switch samples (harmless; buffers refill on the next polls).

A WARNING and a `unit_changed` event (persisted event log) are emitted. WARNING: `Temperature unit changed since last run: same-day readings discarded from=<saved_unit> to=<current_unit> dropped_outdoor=<n> dropped_indoor=<n> dropped_pred_archive=<n> dropped_classification=<True|False> dropped_pending_observations=<n>`. Event payload: `from`, `to`, `dropped_outdoor`, `dropped_indoor`, `dropped_pred_archive`, `dropped_classification`, `dropped_pending_observations`. A missing or non-string `temp_unit` is a no-op, so existing installs and the first upgrade to 0.7.83 behave as before (a unit change on that same restart is not detected). `STATE_VERSION` is unchanged: the key is additive, the loader checks only `version`, and older code ignores it, so the change is **downgrade-safe**. Not repaired: chart-log entries and learning/thermal records written while the unit was wrong.

See [temperature-conversion.md](temperature-conversion.md#unit-change-handling-issue-1015).

### Restore across the date boundary (Issues #1349, #1354-#1360)

The person in the home expects a restart, including an overnight outage that comes back after midnight, to change nothing they set or relied on: observe-only stays off, a running grace period or fan hold still expires on time, and the activity report still shows what happened before the outage. Before this rule, `async_restore_state()` returned early ("starting fresh") when the state file's `date` was not today (the Issue #10 design: same-day state = full restore), which silently dropped all of that.

**Rule: the date gate guards only data whose meaning is "today".** That is what `_async_end_of_day()` resets at 23:59 plus today's classification. Everything time-stamped, self-expiring, run-once or the occupant's own setting is restored on any restart. Yesterday's `DailyRecord` recovery (the state file's `date` is exactly yesterday: recover it to learning, then start fresh) is unchanged and stays exactly-yesterday.

Order in `async_restore_state()`: load the state file; load investigation reports (own file, before the `not state` return); if there is no state, still classify the restart (below) and return; yesterday recovery; the any-restart block (the event log is restored before `automation_engine.restore_state()`, so the `grace_started` row of a re-armed fan-off grace is kept, Issue #1362); **the date gate**; the same-day-only block. Below the gate the INFO line is `Restoring same-day state from <last_saved>`; on a cross-day restore the coordinator logs INFO `Persisted state is from <date> (today is <today>) — settings and history restored; today's plan starts fresh`.

| State key / data | Restored on | Issue | Why |
|---|---|---|---|
| `ai_stats`, `promise_stats`, `sensor_last_real_reading`, coordinator health fields | any restart | #81 (`ai_stats`), #1128 (`promise_stats`), #1181 (`sensor_last_real_reading`), #480 (health) | Counters and health, not "today's" plan |
| `automation_enabled` + engine `dry_run` | any restart | #1349 | The occupant's setting; losing it let CA command the thermostat after an overnight outage |
| `unit_changed` check (`temp_unit`) | any restart | #1360 | A unit switch that coincides with an outage must still discard old-unit pending observations, kept forecast and `pred_archive` |
| `automation_state` (engine `restore_state`: economizer/fan hints and HVAC recency (#835/#843), the `fan_off` manual grace (#1006), the fan-off hold (#1205) and the stashed fan manual-override re-arm (#1045); override, other grace and pause flags are still deliberately cleared, Issues #282/#327) | any restart | #1358 | Timestamps self-expire against `dt_util.now()`; without it a re-arm (#1006/#1045/#1205) was lost across midnight |
| Backfill run-once flags (8) + `last_solar_phase_fit_date` | any restart | #1355 | Run-once guards; losing them re-ran the backfills and wrote duplicate thermal observations |
| `kept_forecast` | any restart (dropped on unit change) | #1359 | Time-stamped and self-expiring; keeps forecast continuity |
| `pred_archive` | any restart (dropped on unit change; pruned to the same 7-day expiry as the live archive) | #1359 | Time-stamped history feeding chart/accuracy; pruned on restore so an old file cannot resurrect expired entries |
| Event log (`_prune_event_log`) | any restart | #1357 | The activity report is the occupant's record of what CA did; it is time-stamped and pruned by age |
| Restart-cause classification, `system_restarted` event, `clean_shutdown` / `last_shutdown_kind` reset | every restore, **including no state file** | #1357 | The occupant should see that a restart happened; `recovered_events` is 0 when no state file existed |
| Investigation reports (own file) | every restore, before the `not state` return | #1356 | Separate file, not tied to the state file's date or existence |
| Vacation/Away toggles | applied at `async_setup()`, not restored | #1354 | See below |
| `classification` (+ `classification_fetched_at`) | same-day only | #10 | "Today's" day-type; recomputed on the first cycle |
| `temp_history` (+ #540 peak mirror) | same-day only | #10 | Buffers of today's readings; yesterday's would pollute today's trend |
| `today_record` | same-day only | #10 | Today's `DailyRecord` in progress; reset at 23:59 |
| `briefing_state` | same-day only | #10 | Today's briefing sent/pending |
| `occupancy_mode` (+ `set_occupancy_mode` bridge until `async_setup`) | same-day only | #28 (persisted), #85 (engine sync) | Superseded by the live toggles at setup (#1354); not trusted across a day |
| `occupancy_away_since` | same-day only | #28 | Away-minute accounting for today |

Vacation/Away (Issue #1354): the person who left on vacation and whose house restarted expects the setback at once, not after a 15-minute away grace. `async_setup()` computes the mode from the live occupancy toggles, syncs it into the engine with `set_occupancy_mode()` (the live toggles win over a restored mode; INFO `Occupancy at startup: engine <old> -> live toggles <new>` when they differ), and, when the mode is not home/guest and `occupancy_away_since` is unset, starts counting away minutes. It does not start the away timer or call `handle_occupancy_away/vacation()`: the setback reaches the thermostat through the normal plan application at the end of the startup coalescing window, like every other plan command after a restart.

Restart classification (Issue #1357, `_classify_restart_and_mark()`): `version_changed`, then reload / `user_restart` / `unknown` cause, then a `system_restarted` event with `recovered_events` = number of events restored into the log, then the `clean_shutdown` reset. It now runs on every restore, so a first run or a deleted/corrupt state file still emits `system_restarted` (with `recovered_events` 0 and the cause derived as before), and a cross-day restart reports the events it recovered. See [activity-report-table.md](activity-report-table.md).

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
8. **Every persisted key is classified any-restart or same-day.** A new coordinator state key must be placed deliberately in the table in [Restore across the date boundary](#restore-across-the-date-boundary-issues-1349-1354-1360): restored above the date gate if it is time-stamped, self-expiring, run-once or the occupant's own setting; below it only if its meaning is "today". Adding it below the gate by default re-creates the Issue #1349 family of bugs.

### Not persisted: the memory of CA's own setpoint writes (Issue #1232)

`AutomationEngine._own_setpoint_writes` (entity, attribute, value, time, Context id of each setpoint CA wrote, kept 180 s so a late thermostat echo is recognised) is deliberately transient. It lives 180 s and is only about to be compared with a thermostat report that is itself a reaction to a write made moments ago; a restart ends both, and the startup coalescing window already suppresses override detection for the first 5 minutes after a restart (Issue #1235). Persisting it would add a state-file field, a version question and a stale-entry risk for no occupant benefit. No `STATE_VERSION` change.

## Disclosure Path

← Tier 1 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md) (see "Integration Version" and "Coordinator State Listeners" sections)
→ Tier 3 specs: none yet authored — candidates: `StatePersistence.save` atomic write contract, `StatePersistence.load` version guard contract
↔ Siblings: [docs/learning-engine.md (not yet written)] — `LearningEngine`/`LearningState` is the parallel persistence subsystem for the learning DB
