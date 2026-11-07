<!-- Nav: ← [Predictor v4](predictor-v4-spec.md) | → [Prediction Engines](prediction-engines-spec.md) | → [v4_shadow.py](../custom_components/climate_advisor/v4_shadow.py) [v4_archive.py](../custom_components/climate_advisor/v4_archive.py) [prediction_accuracy.py](../custom_components/climate_advisor/prediction_accuracy.py) [coordinator.py](../custom_components/climate_advisor/coordinator.py) (block "Prediction engine v4 in shadow") | ↔ [Chart State Log](chart-log-spec.md) [Forecast Archive](forecast-archive-spec.md) [State Persistence](state-persistence.md) [REST API](rest-api.md) [Thermal Model v3](thermal-model-v3-spec.md) -->

# Prediction Engine v4 Shadow - Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant experience from the v4 shadow? | The shadow cycle described here (post-chart predict, archive, chart lines) changes nothing in the house. Since 0.9.4 v4 is the default PRIMARY engine (Settings -> Prediction engine): as primary its own pre-decision predict (`_v4_control_cycle`) and fitted rates drive the pre-heat/pre-cool time-of-use lead, the ODE ceiling guard, the nat-vent window-close time and the setback depth ([Prediction Engines Spec: Control feed](prediction-engines-spec.md#control-feed-r6b)); as the comparison (shadow) engine it only draws a line. A broken or missing v4 primary means those decisions use the "not learned yet" defaults (never v3) and the accuracy page stays empty. | [Invariants](#invariants), [No-decision-path guarantee](#nothing-on-a-decision-path-reads-v4) |
| When does v4 fit, and when does it predict? | Fit: every night at 03:15 local, plus once after startup when no model exists or the stored model has confidence `none` (retried by the cycle at most every 6 h). Predict: once per 30-minute coordinator cycle, after the chart row is written. Never two fits at once. | [Scheduling](#scheduling) |
| What does a cycle feed `predict()`? | The chart row this cycle just wrote (live readings per sensor, `_ca`), the raw hourly forecast, the target-band schedule as setpoints (heat sp on a heat day, cool sp on a cool day), planned windows, fan off, and the hidden-mass state stepped once per elapsed 15 min. | [Per-cycle inputs](#per-cycle-inputs) |
| Which files does the shadow write, and what is in them? | Three entry-scoped files: `climate_advisor_v4_archive_<id>.json` (1/4/12/24 h predictions per sensor, 400 days), `climate_advisor_v4_model_<id>.json` (fitted model, hidden-mass state, last fit) and `climate_advisor_v4_backtest_<id>.json` (written only by `tools/predict_backtest.py --publish`, which `tools/deploy.py` runs after every deploy since Issue #1400; the integration only reads it). | [Files](#files), [Backtest file](#climate_advisor_v4_backtest_entry_idjson-read-only-here) |
| Why does a `_ca`-only model archive and draw anything (Issue #1400)? | Its only line IS `_ca`: with no primary node line to derive it from, `record()` stores `_ca` as its own line (still inside the M4 memory limit), and the Status chart draws it as "Climate Advisor reading", broken at every bedtime/wake-up sensor swap. | [Archive](#climate_advisor_v4_archive_entry_idjson-v4archive-v4_archivepyl51), [Status chart](#invariants) |
| How often is the archive written? | At most hourly from the cycle (monotonic clock), and once on unload with one retry. A crash loses at most about an hour of shadow predictions, never anything v3 or the house depends on. | [Persistence timing](#persistence-timing), [Error Conditions](#error-conditions) |
| Is a rollback to a release without v4 safe? | Yes. The model is NOT in the learning file, so an older release never sees it; and this release's `LearningEngine.load()` ignores unknown top-level keys instead of resetting the history. The three v4 files are simply left on disk. | [Rollback safety](#rollback-safety) |
| What guards "no decision module reads v4"? | `tests/test_v4_shadow_structural.py` parses the package source (AST): only the shadow modules (`v4_shadow`, `v4_archive`, `prediction_accuracy`, `predictor_v4`, `night_replay`), the display registry `prediction_engines.py`, the advisory `night_strategy.py`, `const.py`, `api.py` and named `_v4*`/`v4_*` coordinator methods plus the fixed wiring points may mention `v4`; `learning.py` may not at all. Since R6b it also proves the decisions get the primary only through the control feed and never read the shadow. | [No-decision-path guarantee](#nothing-on-a-decision-path-reads-v4) |

## Scope

- **Files:** `custom_components/climate_advisor/v4_shadow.py` (pure/executor halves, 637 lines in 0.9.4), `v4_archive.py` (`V4Archive` and `V4ModelStore`, 560 lines), `prediction_accuracy.py` (endpoint and chart payloads, 488 lines), the coordinator block "Prediction engine v4 in shadow" (`_v4_*` / `v4_*` methods) plus its wiring points (constructor, nightly timer in `async_setup`, restore, per-cycle call in `_async_update_data_impl`, debug payload in `get_debug_state`, unload in `async_shutdown`), the two views in `api.py` (`ClimateAdvisorEnginesView`, `ClimateAdvisorPredictionAccuracyView`), the `V4_*` constants and `ENTRY_SCOPED_FILES` in `const.py`, `ChartStateLog.last_entry()` / `raw_entries()` in `chart_log.py`, and the unknown-key-tolerant load in `learning.py` (`LearningEngine.load`).
- **Entry points:** `V4Archive.load/save/record`, `V4ModelStore.load/save`, `v4_shadow.run_fit/run_cycle/build_inputs/model_summary`, `Coordinator._v4_start_fit`, `_v4_shadow_cycle`, `_v4_flush_on_shutdown`, `v4_accuracy_inputs`, `v4_model_summary`, `prediction_accuracy.build_prediction_accuracy`.
- **Not covered here:** the model equations, fit, gates and `predict()` contract ([Predictor v4 Spec](predictor-v4-spec.md)); the walk-forward backtest that writes the third file ([Debugging Guide: prediction backtest](09-DEBUGGING-GUIDE.md#prediction-backtest-toolspredict_backtestpy-issue-1230-r1)); the endpoint's request/response reference ([REST API](rest-api.md#get-apiclimate_advisorprediction_accuracy-issue-1230-r1)); the preview page ([Architecture Reference](02-ARCHITECTURE-REFERENCE.md#prediction-accuracy-preview-page-issue-1230-r1)). Which engine drives the decisions (the primary, v4 by default, or v3) is the [Prediction Engines Spec](prediction-engines-spec.md); v3's model is [Thermal Model v3](thermal-model-v3-spec.md).

## Scheduling

| Trigger | Code | What happens |
|---|---|---|
| Nightly timer, 03:15 local plus a stable per-zone offset of up to 30 min (`V4_FIT_HOUR`/`V4_FIT_MINUTE` + `v4_shadow.fit_offset_s(entry_id, V4_FIT_STAGGER_S)`, a CRC of the entry id) | `async_track_time_change` registered in `async_setup`, kept in `_unsub_listeners` (cancelled on unload), callback wrapped by `_zone_scoped` -> `_v4_nightly_fit` -> `_v4_start_fit("nightly")` | Refit this zone from the last 30 days of chart_log rows plus forecast-archive snapshots, warm-started from the stored model |
| First fit / retry | `_v4_shadow_cycle_impl`: no stored model dict, or a stored model with `confidence == "none"`; startup coalescing over (`_startup_coalesce_active` is not `True`) | `_v4_start_fit("first")` (no model) or `_v4_start_fit("retry")` (a `none` model; the cycle then continues and only advances the hidden node, since `predict()` returns `None`). If this is not the first attempt since startup, only when the last attempt is at least `V4_FIT_RETRY_MIN_INTERVAL_S` (6 h) old (monotonic) |
| 30-minute cycle | `_async_update_data_impl` calls `await self._v4_shadow_cycle()` right after the chart row and `_pred_archive` purge | Predict 24 h, archive 1/4/12/24 h |
| Unload | `async_shutdown` | `_v4_flush_on_shutdown()` (inside `try`), then `_v4_cancel_fit()` (in `finally`) |

- **Staggered and serialised (R1b, M5).** Zones do not all fit at 03:15:00: each fits at 03:15 plus its own offset (0-30 min, stable across restarts). Every fit also takes one domain-wide `asyncio.Lock` (`hass.data["climate_advisor_v4_fit_lock"]`, `_v4_fit_lock()`) around the executor job, so after startup coalescing - when every zone starts its first fit at once - only one zone fits at a time (the others wait on the lock; cancelling on unload is safe).
- **No concurrency within a zone.** `_v4_start_fit` check-and-sets `_v4_fit_running` on the event loop; a second request while one runs is skipped (DEBUG "skipped, a fit is already running"). It also refuses after `_shut_down` and when `v4_shadow.predictor_available()` is false (the engine failed to import).
- **Off the event loop.** The fit runs as a background task (`self._spawn(..., background=True)`, so HA cancels it on unload) and its CPU work, `v4_shadow.run_fit`, runs in the executor through `_executor_job`. Inputs (`chart_log.raw_entries()` bounded to the last `V4_FIT_TRAIN_DAYS` = 30 days inside `run_fit`, `forecast_archive.get_snapshots`, sensor ids, previous model, the climate entity as the primary's alias and the sleep-window predicate) are captured on the loop before the job starts. `run_fit` picks the nodes with `predictor_v4.resolve_nodes` (shared with the backtest) and maps rows older than the entity primary onto it, so a log that predates `indoor_sensors` is not ignored.
- **Result handling.** After the job: if `_shut_down` the result is dropped. Otherwise the model dict replaces `V4ModelStore.v4_model`; when the primary sensor changed (or there was no previous model) `v4_mass_f`/`v4_mass_at` are cleared so the hidden node re-seeds from the reading; `last_fit` = `{at, reason, seconds, ok: true}`; one INFO line (`v4 fit (<reason>): <summary_line> span=..d primary=.. sensors=N took ..s`); `V4ModelStore.save` in the executor.
- **The "6 h retry" covers a missing model and a `none` model.** A fit that raised leaves no model (or the previous one); a fit that completes with `confidence == "none"` (thin data, no skill) stores a model dict but is treated like no model for the retry rule, so a new install or a newly added sensor gets a usable model within about 6 h of enough history instead of waiting for 03:15. A failed fit saves the model file right away so `last_fit_error` survives a restart.
- **Cost.** A 30-day fit is 5-8 s typical on a Raspberry Pi 4 class CPU (worst case 25 s), budget 60 s ([Predictor v4: Performance](predictor-v4-spec.md#performance)); a cycle predict is about 1 ms; the cycle logs its measured cost at DEBUG and in `last_cycle.ms`.

## Per-cycle inputs

`_v4_shadow_cycle_impl` runs after the cycle's decisions and reads, never writes, decision state. Order and rules:

1. **No model -> return** (after the first-fit rule above). A stored model of any confidence continues: `predict()` itself returns `None` for `confidence == "none"`.
2. **The chart row.** `chart_log.last_entry()` must be a non-event row whose `ts` is within 120 s of now; otherwise the cycle is skipped (DEBUG "no chart row for this cycle"). The fit trains on exactly these row fields, so the live inputs cannot drift from the training inputs.
3. **Readings** = `row["indoor_sensors"]` (entity id -> degrees F) plus `"_ca"` = `row["indoor"]` (the reading Climate Advisor itself used).
4. **Step sample fields** (for the hidden-mass filter): `outdoor_f` = `row["outdoor"]`, `forecast_outdoor_f` = `row["pred_outdoor"]`, `hvac_action` = `row["hvac"]`, `fan_running`, `windows_open`, `heat_sp_f`/`cool_sp_f` = `row["setpoint"]` when the climate entity's state is `heat` / `cool`, else `None`.
5. **Future inputs** (`v4_shadow.build_inputs`): one `V4Input` every 15 minutes for up to `V4_PREDICT_HORIZON_HOURS` (24 h) after now, stepping in UTC so DST never repeats or skips a point. The same plan v3 follows:
   - outdoor = linear interpolation of the RAW hourly forecast (the engine corrects it inside `predict()`); the list stops 30 minutes after the last forecast hour (no extrapolation) and is empty with fewer than two forecast points;
   - setpoints from the target-band schedule: on a `heat` day (`_compute_day_hvac_modes`) the heat setpoint is the band floor interpolated at that time, on a `cool` day the cool setpoint is the band ceiling, an `off` day has none;
   - windows open inside the classification's window span when `windows_recommended`, otherwise closed; fan always off (v3 has no forecast fan schedule).
6. **Hidden thermal-mass node** (`step_mass`): `mass_f`/`mass_at` are stored in `V4ModelStore`. If there is no stored mass, no timestamp, a negative gap, or a gap over `V4_MASS_MAX_GAP_S` (2 h), the mass is re-seeded from the primary reading (one step). Otherwise it is stepped `max(1, round(gap / 900 s))` times, so a normal 30-minute cycle steps twice. Each catch-up step reuses the current reading and sample fields (the filter only depends on the primary reading). A missing reading returns NaN and the previous mass is kept.
7. **Predict** (`v4_shadow.run_cycle` in the executor): `from_dict(stored model)` (an unreadable model raises `ValueError`, caught by the wrapper), step the node, `predict(model, now, readings, mass, inputs, tz, outdoor_now_f=row["outdoor"], forecast_now_f=row["pred_outdoor"], hvac_action_now=, run_elapsed_min=, ca_sleep_node=, ...)`. `hvac_action_now` / `run_elapsed_min` come from the newest chart rows (`v4_shadow.run_state_from_rows`: a heating or cooling run in progress is continued, not restarted); `ca_sleep_node` is the configured sleep sensor when the model has it as a node, so the result carries the `_ca` composite. The expected-openness prior needs no input: it is in the model, so the live line and the backtest apply the same fractional windows / fan. With no inputs it returns `(None, mass)`.
8. **Store and archive.** `v4_mass_f`, `v4_mass_at` updated and the store marked dirty; if predictions exist, `V4Archive.record(now, predictions, model["primary"])`. Then the throttled save below. `last_cycle` = `{at, ms, predicted, archived, inputs}` is kept for the Debug tab.

## Files

All three are entry-scoped (`climate_advisor_v4_<kind>_<entry_id>.json`, resolved by `storage_paths.resolve_entry_scoped_path`; a brand-new family, so there is no legacy unscoped file to migrate), listed in `const.ENTRY_SCOPED_FILES`, and deleted with the zone ([State Persistence: Zone removal cleanup](state-persistence.md#zone-removal-cleanup)). Temp files use the prefix `climate_advisor_v4_archive_` / `climate_advisor_v4_model_` and the suffix `.tmp`.

### `climate_advisor_v4_archive_<entry_id>.json` (`V4Archive`, `v4_archive.py`)

What v4 said, per sensor and look-ahead. First write wins, exactly like the v3 `_pred_archive` ([Chart State Log](chart-log-spec.md)): the value kept for a 30-minute slot and look-ahead is the one written when the slot was that far ahead; a later prediction for the same slot never overwrites it.

```
memory: sensor -> columnar line (base epoch + float32 column per lead)      # `data` property = {sensor: {epoch_30min: {"1h": v, ...}}} (degrees F)
disk:   {"v": 1, "leads": ["1h","4h","12h","24h"],
         "sensors": {"_ca": {"1768000000": [68.1, 68.4, null, null]}, "sensor.hallway_temp": {...}}}
```

- Sensor keys are the indoor entity ids (node lines), plus `"_ca"` for a `_ca`-only model (below). While the model has a primary NODE line `"_ca"` is NOT stored (R1b, M4): it is derived on read - the sleep-sensor node's value for a slot inside the sleep window when that node exists, else the primary's - from the `ca` source the coordinator sets each cycle (`set_ca_source(primary, sleep=, sleep_time=, wake_time=, tz=)`, persisted as the file's optional `"ca"` object). An older file with a stored `_ca` line is read, and the duplicate dropped once the source is known.
- **`_ca`-only model (Issue #1400).** When the fit keeps no entity sensor (`primary == "_ca"`, `sensors == ["_ca"]`, e.g. a new install whose per-sensor history is a day old) there is no node line to derive from, so `record()` stores `"_ca"` as its own line (rule: `"_ca"` is skipped only when `primary` is a node this cycle also predicted). Before the fix the cycle logged `archived: 0` forever and the accuracy page had nothing to score. When a later model gains a primary node, the stored `"_ca"` slots at or after the node line's first slot are dropped (`_trim_stored_ca`, derivable again) and the older ones kept, so the live accuracy history survives the switch; the derived read falls back to them (`_ca_value`, `_ca_slots`). Memory: one more 4-lead float32 line, ~0.3 MB at 400 days; `tests/test_v4_visible_1400.py` pins four lines x 400 days under the same 5 MB M4 budget.
- **Memory (R1b, M4):** each line is columnar - a base epoch plus one float32 `array` per look-ahead indexed by 30-min slot, NaN = not written (4 bytes per slot and look-ahead). Three lines at 400 days cost about 0.9 MB in RAM (the dict-per-slot layout cost ~27 MB per zone). `data` is still available as a materialised read-only copy of the legacy shape for tools and tests; `put()` stores one value, `slot_count()` and `memory_bytes()` feed the Debug tab.
- `record()`: for each sensor line and each lead, take the predicted point nearest `now + lead` (must be within 600 s) and store it, rounded to 2 dp, in the slot `slot_key()` = Unix time rounded to the nearest 1800 s of that point's time. Present leads are never replaced. Non-finite values are skipped.
- **Retention:** `V4_ARCHIVE_MAX_DAYS = 400` days, time based. Eviction runs at most once per hour, on a `record()` that added something, and removes empty sensor lines; also applied on `load()`.
- **Size:** about 45 bytes per sensor slot (roughly 17,500 slots a year per sensor line).
- **Load:** missing file -> empty (INFO). Unreadable JSON, or a shape without a `sensors` dict and `leads` list -> WARNING and empty. Per row: non-integer keys, non-list values, expired slots and non-finite or non-numeric values are dropped; nothing raises.
- **Save:** serialise from list() copies (the executor thread must not iterate a dict the loop is adding to), `tempfile.mkstemp` in the same directory, `os.replace`, `chmod 0o600` off Windows, `dirty = False` only on success. Returns `False` and logs an ERROR on serialise or OS failure; the temp file is removed.
- **Readers:** `series(sensor, lead, start, end)` and `snapshot()` iterate copies and are safe from an executor thread.
- **Status chart `_ca` line (Issue #1400).** `build_chart_v4` draws `"_ca"` (the reading Climate Advisor uses) as v4's "Climate Advisor reading" line - past (archive/backtest, per look-ahead), future and the no-plan baseline; a node model without a sleep node takes the future `_ca` from its primary node's line - next to every per-sensor line. The owner's no-swap rule is kept by breaking, not by hiding: `breaks["_ca"]` lists the swap times from `prediction_accuracy.ca_swap_epochs` (the chart log's `band_sensor` changes between rows, snapped to the configured bedtime/wake time between them, which also catches a stale bedroom sensor falling back to the hallway; outside the rows' span - the future, and rows older than `band_sensor` - every bedtime and wake time while `sleep_indoor_temp_entity` is set), and `chart_v2.js` `_withBreaks` ends the line just before each, so no segment crosses a swap. The same times for the whole chart window are the top-level `ca_swaps` key (built by `get_chart_data()` via `_chart_ca_swaps`, independent of which engine runs), which also breaks the actual CA reading and v3's line - with v4 switched off too. None at daily resolution (a daily mean is no one sensor's reading).

### `climate_advisor_v4_model_<entry_id>.json` (`V4ModelStore`, `v4_archive.py`)

```
{"v": 1, "model": <predictor_v4.to_dict(...)> | null, "mass_f": 66.2 | null, "mass_at": iso | null,
 "last_fit": {"at", "reason", "seconds", "ok"} | null, "last_fit_error": str | null}
```

- `model` is exactly the predictor's own serialisation (schema in [Predictor v4: Schema](predictor-v4-spec.md#schema-schema_version--1)); `from_dict` rejects a wrong schema, so an old model is refitted, not migrated.
- **Load:** missing file -> no model, silently; unreadable JSON or a non-dict -> WARNING "a new fit will run". Wrong-typed fields individually fall back to `None`. Never raises.
- **Save:** same atomic, `0o600`, executor contract as the archive; `json.dumps(..., allow_nan=False)` so a NaN in the model fails the save (ERROR, previous file intact) instead of writing invalid JSON.
- **Why a separate file, not `LearningState`:** see [Rollback safety](#rollback-safety).

### `climate_advisor_v4_backtest_<entry_id>.json` (read-only here)

Written by `tools/predict_backtest.py --publish` over SSH ([Debugging Guide](09-DEBUGGING-GUIDE.md#prediction-backtest-toolspredict_backtestpy-issue-1230-r1)); the integration never writes it. **Since Issue #1400 `tools/deploy.py` runs it after every successful deploy** (`predict_backtest.py --all-zones --publish --batch-ssh -q` on the deploying machine; `--no-backtest` skips it; a failure only warns and prints the command to run after the SSH add-on's cooldown). `--batch-ssh` reads every zone's chart log, learning DB, forecast archive and the HA time zone over ONE connection and writes every zone's file over ONE more (`publish_remote_batch`: tar on stdin, unpacked in `/config/.ca_backtest_publish`, each file `chmod 600` then `mv`), so a deploy costs 5 SSH connections instead of 3. Why not in the integration: the backtest replays production v3 by importing the coordinator with Home Assistant stubbed (`tools/sim_harness/ha_stubs.py`, and it re-points the HA default time zone), lives in `tools/` (not deployed) and refits v4 once per test week (about 50 fits of 5-8 s each on a Pi per zone per year of history: minutes of GIL-holding CPU on the HA host, the starvation M5 exists to avoid). Reversal: pass `--no-backtest` or delete the deploy step; the file and the page's handling of a missing file are unchanged. `prediction_accuracy.load_backtest()` returns `None` for a missing or unreadable file (WARNING for unreadable) or a non-dict. Keys consumed (all optional): `points` = sensor -> lead -> `[{t, actual, flat, v4, v3}]`, `gate` (rows), `gate_summary`. See [Payload and merge rule](rest-api.md#get-apiclimate_advisorprediction_accuracy-issue-1230-r1).

## Window Plan Rule And Night Evidence (R6)

- **One window assumption** (`window_span.planned_window_span` (engine-neutral module, re-exported by `v4_shadow`), shared by the live shadow and `tools/predict_backtest.py`): windows recommended -> open over the engine's planned open/close times on the anchor's local day (live: the classification; backtest: the learning DB's daily record `window_open_time` / `window_close_time`, the same plan source), never repeated at the same clock time on the next day; **tomorrow's own planned period** (Issue #1391) is added for tomorrow's hours by `planned_window_spans` (`planned_window_span(..., day_offset=1)`, no anchor fallback, unknown plan = closed): live from `_tomorrow_classification_plan()` (the timeline's `plan_days[1]`) in `_v4_cycle_kwargs`, which the plan cycle AND the control cycle (`run_cycle(baseline=True)`, R6) share; backtest from the NEXT day's learning DB record only for anchors at or after `--tomorrow-plan-hour` local (default 18, 24 = never) with `--window-plan-source record` and a record holding both times (no hindsight: earlier anchors keep tomorrow closed; the record is the plan as run that day, a small residual approximation). v3 (`_build_predicted_indoor_future`, which drives decisions when v3 is the primary) takes the same rule through `tomorrow_window=` (live: `_tomorrow_window_plan()`; backtest: `v3_curve(tomorrow_plan=...)`, the same no-hindsight guard); its window-open hours are now date-aware (the old time-of-day check repeated today's clock times on tomorrow's hours, and ignored an overnight span); recommended with no planned times -> the first `WINDOW_PLAN_FALLBACK_HOURS` (4 h) from the anchor. `--window-plan-source fallback` restores the pre-R6 backtest rule. Zone 1 backtest before/after (DP-8 rows, information only since 0.9.4): unchanged, 3 of 6 rows pass; `_ca` 4 h v4 MAE 1.03 F both ways, bias +0.12 -> +0.14 F.
- **Night-strategy evidence:** after every successful fit the coordinator runs `night_replay.run_nightly` (executor, same domain lock) and stores its record in the model file under `night_validation` (see [Night Strategy](night-strategy-spec.md)). An older model file without the key reads as none; the next cycle starts the replay.

## Persistence timing

- **Throttle.** After each cycle, if the archive or the store is dirty and `V4_ARCHIVE_SAVE_MIN_INTERVAL_S` (3600 s, monotonic clock) has passed since the last cycle-triggered save (or there was none since start), the archive (if dirty) and then the store (if dirty) are saved in the executor. The first cycle after a restart therefore saves immediately. The store is dirty after every cycle (the mass moved), so the model file is rewritten on the same hourly beat.
- **Fit.** A successful fit saves the model file at once. A failed fit marks the store dirty (`last_fit` + `last_fit_error`) and leaves the write to the next hourly save or the shutdown flush.
- **Shutdown flush** (`_v4_flush_on_shutdown`, `coordinator.py#L6595`): if dirty, save the archive; on failure retry once; a second failure logs a WARNING with the slot count that is lost. Same for the model file. The whole block is wrapped in `try/except` so unload continues.
- **Restore** (`coordinator.py`): `V4Archive.load` then `V4ModelStore.load`, both in the executor; `_v4_last_fit` is restored from the file.

## Nothing on a decision path reads v4

> **Superseded by Issue #1230 R6b for the PRIMARY engine.** When v4 is the primary engine the decisions read its baseline path and rates through the coordinator's control feed ([Prediction Engines Spec: Control feed](prediction-engines-spec.md#control-feed-r6b)); the shadow cycle described in this spec is unchanged (it still runs after the decisions and reads the chart row), a v4 that is only the comparison engine still never acts, and no decision MODULE names v4 or the registry (structural tests below).

**The invariant (R1 shadow; since R6b true of the shadow cycle and of a comparison engine, not of a v4 primary's control feed).** No classification, setpoint, fan, window, nat-vent, briefing, notification, Status sensor or chart line may read a v4 value, a v4 attribute, or call into the v4 modules. A bad v4 fit must be unable to change how the house is run.

**Structural test.** `tests/test_v4_shadow_structural.py` checks the source, not behaviour, so a future edit that wires v4 into a decision fails before it ships:

| Test | Asserts |
|---|---|
| `test_no_module_outside_the_shadow_wiring_mentions_v4` | Every `*.py` in the package other than the shadow modules (`v4_shadow`, `v4_archive`, `prediction_accuracy`, `predictor_v4`), `const.py`, `api.py` and `coordinator.py` contains no identifier containing the token `v4` (names, attributes, imports, def/class names; comments, docstrings and log text are ignored) |
| `test_shadow_modules_import_nothing_that_decides` | The shadow modules do not import `automation`, `classifier`, `briefing`, `desired_state`, `swing`, `nat_vent_fsm` or `fan_fsm` |
| `test_learning_py_knows_nothing_about_v4` | `learning.py` has zero v4 references (the model lives in its own file) |
| `test_api_py_only_exposes_v4_read_only` | `api.py` mentions v4 only inside `ClimateAdvisorPredictionAccuracyView` and `ClimateAdvisorEnginesView` |
| `test_coordinator_mentions_v4_only_in_the_shadow_methods_and_fixed_wiring_points` | In `coordinator.py` v4 appears only in `_v4*`/`v4_*` methods and the fixed wiring points `__init__`, `async_setup`, `async_restore_state`, `async_shutdown`, `_async_update_data_impl`, `get_debug_state` |
| `test_the_cycle_hook_is_a_discarded_await_and_the_debug_field_is_output_only` | The per-cycle call is a bare awaited expression statement (its result is discarded), and the only v4 attribute in `get_debug_state` is `v4_model_summary` (output only) |
| `test_shadow_methods_write_only_v4_state_and_never_touch_the_engine` (parametrised) | The six shadow coordinator methods assign only `self._v4*` attributes and never reference `automation_engine`, `_emit_event`, `_emit_incident`, `services` or `async_call` |

Behavioural companions: `tests/test_v4_shadow_wiring.py` (scheduling, throttle, flush, failure paths), `tests/test_v4_shadow_archive.py`, `tests/test_v4_shadow_endpoint.py`, `tests/test_v4_shadow_real_engine.py`, and `tests_ha/test_v4_shadow_1230.py`. `tests/test_executor_offload.py` registers `(_v4_archive, load|save)` and `(_v4_store, load|save)` in `_BLOCKING_METHODS`, so a call from the event loop without the executor fails the suite. Goldens must not change: v4 emits no engine event and no service call.

## Rollback safety

User view: if the owner rolls back to a release without the shadow, their learned thermal model, history and suggestions are untouched and the house runs as before.

1. The fitted model is stored in `climate_advisor_v4_model_<id>.json`, NOT in `LearningState`. An older release would reject a learning file with an extra field (`LearningState(**data)` raises) and reset the whole learning history; it never opens the v4 files.
2. This release also hardens the other direction: `LearningEngine.load()` drops top-level keys it does not know before building `LearningState` (`learning.py#L980`, logged once at INFO as "ignoring unknown keys from a newer release", at WARNING when no known field is present at all), so a future field added to the learning file cannot wipe history on a downgrade.
3. The learning file is never written by the v4 code (`learning.py` has no v4 reference; structural test above).
4. The three v4 files stay on disk after a rollback and are picked up again on a roll-forward; deleting the config entry removes them.

## Pre-conditions

1. `V4Archive.load()` and `V4ModelStore.load()` ran (restore) before the first cycle; both are blocking and run through the executor.
2. `save()` on either store is only called from the executor; `record()` is memory only and runs on the loop.
3. `build_inputs` receives the same hourly forecast (`_hourly_forecast_temps`, internal degrees F) and target-band schedule v3 uses.

## Post-conditions

1. After a cycle that returned normally, `v4_mass_f`/`v4_mass_at` reflect this cycle, and (when v4 predicted) each sensor line has a value for every lead whose target slot was empty.
2. After a successful `save()`: the file holds the whole in-memory state, no `.tmp` is left, `dirty` is false.
3. After `_v4_cancel_fit()`: no fit is flagged running and the stored model is whatever was last committed.

## Invariants

1. The SHADOW CYCLE has zero effect on decisions and Status sensors (above); a v4 primary reaches decisions only through the separate control feed ([Prediction Engines Spec: Control feed](prediction-engines-spec.md#control-feed-r6b)). The Status chart DISPLAYS v4 since Issue #1230 R2; from 0.9.4 there is no accuracy gate and v4 is the default primary engine (Settings -> Prediction engine): it runs only while it is the primary or comparison engine, and the chart payload never reaches a decision (`prediction_accuracy.build_chart_v4`, called from `get_chart_data()` via `v4_chart_series`); `test_get_chart_data_only_displays_v4` pins that the result flows only into the `v4` payload key. The cycle keeps `_v4_last_future` (newest 24 h lines, the corrected outdoor forecast and the no-plan baseline from `v4_shadow.chart_extras`) for it; after a restart the future lines are absent until the first cycle (up to 30 minutes).
2. A v4 failure never propagates: `_v4_shadow_cycle` catches everything but `CancelledError` (first failure WARNING with traceback, repeats DEBUG, reset by the next success); `_v4_run_fit` keeps the previous model on any exception; `V4Archive.record/load/save` and `V4ModelStore.load/save` never raise.
3. The archive is first-write-wins and in internal degrees F; unit conversion happens only in the endpoint.
4. Never two fits at once; a fit result arriving after unload is dropped.
5. Time arithmetic is absolute (epoch/UTC), never wall-clock on aware local datetimes (DST).

## Error Conditions

| Failure | Handling | Occupant / caller sees |
|---|---|---|
| v4 engine fails to import | `predictor_available()` false: fits skipped (DEBUG); `engine_available: false` in the Debug view | Nothing; preview page shows its empty state |
| Fit raises | WARNING with traceback; `last_fit.ok = false`, `last_fit_error = "fit failed; see the log"`; previous model kept; `_v4_last_fit_attempt` set so the cycle retries a missing first model only after 6 h; the model file is saved at once (`last_fit_error` persists) | Nothing |
| Fit completes with `confidence == "none"` | Model stored; `predict()` returns `None`; nothing archived; the cycle retries the fit at most every 6 h (and 03:15) | Page empty state until a fit reaches `low` or `ok` |
| Stored model unreadable (`from_dict` -> `None`) | `run_cycle` raises `ValueError`, caught by the cycle wrapper (WARNING once) | Nothing |
| No chart row for this cycle (event row, or older than 120 s) | Cycle skipped (DEBUG) | A 30-minute hole in the archive |
| Cycle exception | Caught, WARNING once, DEBUG after; the mass update for that cycle is lost | Nothing |
| Archive/model `save()` fails | ERROR; `dirty` stays true so the next hourly beat retries; unload retries once, then WARNING with the lost slot count | Up to about an hour of shadow predictions lost |
| Archive/model file corrupt | WARNING, starts empty / fits again | Accuracy history restarts; the backtest file (if published) still fills older ranges |
| Backtest file missing/corrupt | Ignored (`None`) | Live archive only |
| Unload during a fit | Task cancelled; executor thread finishes, its result is dropped (`_shut_down`) | Nothing |

## Debug visibility

- `GET /api/climate_advisor/engines` -> `v4` key = `coordinator.v4_model_summary()` (`api.py#L1370`). `get_debug_state()` -> `v4_model` (`coordinator.py#L14883`). Same dict, temperatures and errors in the user's unit: `has_model`, `confidence`, `fitted_at`, `span_days`, `n_anchors`, `mae_by_lead` (training MAE for `1h`/`4h`/`12h`), `primary`, `sensors`, `schema_version`, `unit`, `engine_available`, `mass`, `fit_running`, `last_fit`, `last_cycle`, `archive_slots` (slot count per sensor line). A failure yields `{has_model: false, confidence: "none", error: true}`.
- Log lines: INFO `v4 fit (nightly|first): ...`; WARNING `v4 fit (...) failed; keeping the previous model`; WARNING `v4 shadow cycle failed (v3 and the house are unaffected)`; DEBUG `v4 shadow: predicted=.. inputs=.. archived=.. cost=..ms`.
- Fetch order for a "v4 page is empty" report: Debug `v4_model` (has_model? confidence? last_fit?), then `python tools/ha_logs.py` filtered on `v4`, then the file sizes on the host.

## Doc and code notes

Details a reader of the contract (`ca-group-briefs/1230-r1-v4-contract.md`) would not expect:

- The contract put the model in `LearningState.v4_model`/`v4_mass_f`; the build uses `V4ModelStore` and its own file (rollback safety above).
- The contract's `v3` is "only for `_ca`"; the endpoint further limits it to look-ahead `4h`, the only lead v3 archives.
- `step_mass` catch-up steps reuse the current cycle's reading for every elapsed step.

## Code Reference

Line numbers as of 0.9.4:

- `v4_shadow.py`: `predictor_available` L50, `run_fit` L103, `build_inputs` L239, `run_cycle` L299, `model_summary` L458
- `v4_archive.py`: `slot_key` L60, `V4Archive` L141 (`record` L351, `_maybe_prune` L393, `series` L425), `V4ModelStore` L466
- `prediction_accuracy.py`: `RANGES` L42, `load_backtest` L51, `build_prediction_accuracy` L181, `build_chart_v4` L392
- `coordinator.py`: `_v4_start_fit` L5595, `_v4_run_fit` L5740, `_v4_shadow_cycle` L6309, `_v4_shadow_cycle_impl` L6488, `_v4_flush_on_shutdown` L6595, `v4_accuracy_inputs` L6621, `v4_model_summary` L6942
- `const.py`: `V4_*` L572-L588, `ENTRY_SCOPED_FILES` L597
- Tests: see [the structural test table](#nothing-on-a-decision-path-reads-v4)
