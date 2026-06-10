<!-- Nav: ← [sim-harness-brief.md](sim-harness-brief.md) | → [tools/sim_harness/](../tools/sim_harness/) | ↔ [simulation-feedback-loop.md](simulation-feedback-loop.md) -->

# Production Simulation Harness — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| How does `FakeHass` prevent stale thermostat read-backs? | `_FakeServices._apply_state_feedback()` reflects every `climate`, `fan`, and `switch` command into `_FakeStates` immediately after recording it in `action_log`, before the engine's next read. | [FakeHass — State-Feedback Loop](#fakehass--state-feedback-loop) |
| How does the virtual clock fire grace-period timers and fan-cycle chains? | `FakeScheduler.advance_to(target)` pops due callbacks from a min-heap, fires each, drains `async_create_task` coroutines, then loops — so self-recursive grace→convergence chains complete without manual intervention. | [FakeScheduler — Virtual Clock](#fakescheduler--virtual-clock) |
| What symbols does `FakeScheduler.installed()` patch, and in which module? | `async_call_later`, `callback` (identity), `dt_util.now`, `dt_util.utcnow`, `dt_util.as_local` — all patched on `custom_components.climate_advisor.automation`, not the originating HA module. | [FakeScheduler — Patching Contract](#fakescheduler--patching-contract) |
| What does `run_production_scenario()` do with `thermal_model` and `hourly_forecast`? | If present as top-level keys in the scenario dict, they are injected directly onto the engine instance (`engine._thermal_model`, `engine._hourly_forecast_temps`) before event dispatch begins. | [run_production — ODE Inputs](#run_production--ode-inputs) |
| How does `predicted_indoor` in a `temp_update` event exercise the ODE ceiling guard? | After injecting temperatures and calling `check_natural_vent_conditions()`, `run_production` checks for `event["predicted_indoor"]`; if present and a classification exists, it calls `apply_classification(classification, predicted_indoor=predicted)` again — modelling the coordinator's periodic re-classification cycle. | [run_production — predicted_indoor Re-Classification](#run_production--predicted_indoor-re-classification) |
| Which event types have no clean production entry point? | `fan_cycle_on`, `fan_cycle_off`, `grace_start`, `grace_end` — all driven internally by scheduler timers in production. The adapter skips them; `advance_to()` drives the timers that cover the same transitions. | [run_production — Unmapped Event Types](#run_production--unmapped-event-types) |
| How are legacy `expect:` strings matched when production emits different event types? | `outcomes.production_decisions()` maps production event types to the legacy vocabulary (e.g. `bedtime_setback` → `setback_applied`, `sensor_opened result=natural_ventilation` → `natural_ventilation`). Unmapped production-only events are collected in `UNMAPPED_PRODUCTION_EVENTS` and silently skipped. | [outcomes — Event-to-Outcome Mapping](#outcomes--event-to-outcome-mapping) |
| What custom assertion types exist and what guarantee do they test? | `nat_vent_not_active`, `nat_vent_still_active`, `override_active`, `override_cleared`, `nat_vent_fan_preserved`, `dual_setback_applied`, `ceiling_guard_fires_cool`, `ceiling_guard_dormant*`, `setpoint_consistent_with_mode`. Each tests the occupant-visible guarantee, not a label. | [outcomes — Custom Assertion Types](#outcomes--custom-assertion-types) |
| How does `ha_stubs.py` avoid metaclass conflicts between MagicMock and HA base classes? | It replaces MagicMock entries with real minimal base classes for `DataUpdateCoordinator`, `CoordinatorEntity`, `SensorEntity`, `RepairsFlow`, `ConfirmRepairFlow`, and enum types. These are the only bases that cause metaclass conflicts when subclassed by production code. | [ha_stubs — Idempotent Stub Installer](#ha_stubs--idempotent-stub-installer) |

---

## Scope

This spec covers the five modules in `tools/sim_harness/` and the CLI shell `tools/simulate.py`:

- **`tools/sim_harness/ha_stubs.py`** — idempotent HA sys.modules stub installer
- **`tools/sim_harness/fake_hass.py`** — `FakeHass`, `FakeState`, `_FakeServices`, `_FakeStates`
- **`tools/sim_harness/fake_scheduler.py`** — `FakeScheduler`, `_ScheduledCallback`
- **`tools/sim_harness/build_engine.py`** — `build_headless_engine()`, `_DEFAULT_CONFIG`
- **`tools/sim_harness/run_production.py`** — `run_production_scenario()`, `ProductionRunResult`, event dispatcher, `_SensorTracker`
- **`tools/sim_harness/outcomes.py`** — `production_decisions()`, `check_assertion()`, `ProductionDecision`, `UNMAPPED_PRODUCTION_EVENTS`
- **`tools/simulate.py`** — CLI, MANIFEST, report shell; delegates engine work to `run_production.py`
- **`tests/test_production_harness.py`** — Tier A enforcement: parametrized golden run

This spec does NOT cover:
- `automation.py` production logic (covered by `08-COMPUTATION-REFERENCE.md`)
- Scenario JSON authoring conventions (see scenario files in `tools/simulations/`)
- The simulation feedback loop (`tools/simulation_loop.py`; see `simulation-feedback-loop.md`)

---

## Pre-conditions

1. `install_ha_stubs()` must be called before any `custom_components.climate_advisor.*` import
2. The project root must be on `sys.path` so `custom_components.climate_advisor.automation` resolves
3. `FakeScheduler.installed()` context manager must be active before the engine can register timers
4. `FakeHass.set_scheduler(scheduler)` must be called before the engine's first `async_create_task`
5. Scenario JSON must be a valid dict with an `"events"` list; each event must have `"type"` and `"time"` keys

---

## Post-conditions

1. `run_production_scenario()` returns a `ProductionRunResult` containing all decisions, service calls, engine flag snapshot, and any callback errors — regardless of whether assertions pass
2. `advance_to()` leaves the virtual clock at exactly `target`; all callbacks due at or before `target` have fired
3. `FakeHass.action_log` contains one entry per `hass.services.async_call()` the engine issued, in order
4. `engine_state` snapshot reflects the engine's live attribute values after the final event was processed
5. `install_ha_stubs()` leaves `sys.modules` with all `_HA_MODULES` entries present; subsequent calls do not overwrite existing entries

---

## Invariants

1. `FakeScheduler` clock never moves backwards — `advance_to()` raises `ValueError` if `target < self._clock`
2. No coroutine is silently dropped: `async_create_task` enqueues to the scheduler if wired, otherwise runs immediately
3. `_apply_state_feedback()` fires for every `async_call` that matches `climate`, `fan`, or `switch` domains — before the engine's next `hass.states.get()` call
4. `install_ha_stubs()` is idempotent: the check `if mod_name not in sys.modules` guards every injection
5. `track: "integration"` assertions are never evaluated in Tier A; they receive `skipped: True` with `reason: "integration-track assertion — deferred to Tier B"`
6. `simulator_support: false` assertions are evaluated normally — they are not skipped in the production harness path

---

## ha_stubs — Idempotent Stub Installer

**File:** `tools/sim_harness/ha_stubs.py`

`install_ha_stubs()` installs MagicMock entries for the 23 HA and aiohttp module names in `_HA_MODULES`, then replaces the MagicMock slots that need real base classes:

| Replacement | Module | Real class |
|---|---|---|
| `DataUpdateCoordinator` | `homeassistant.helpers.update_coordinator` | `_MockDataUpdateCoordinator` |
| `CoordinatorEntity` | `homeassistant.helpers.update_coordinator` | `_MockCoordinatorEntity` |
| `SensorEntity` | `homeassistant.components.sensor` | `_MockSensorEntity` |
| `SensorStateClass` | `homeassistant.components.sensor` | `_SensorStateClass` (StrEnum) |
| `SensorDeviceClass` | `homeassistant.components.sensor` | `_SensorDeviceClass` (StrEnum) |
| `UnitOfTemperature` | `homeassistant.const` | `_UnitOfTemperature` (StrEnum) |
| `RepairsFlow` | `homeassistant.components.repairs` | `_MockRepairsFlow` |
| `ConfirmRepairFlow` | `homeassistant.components.repairs` | `_MockConfirmRepairFlow` |

**Why real base classes for these?** Production modules subclass `DataUpdateCoordinator`, `SensorEntity`, etc. A subclass whose `__bases__` contains a `MagicMock` instance raises `TypeError: metaclass conflict`. Real (plain Python) base classes avoid this. The MagicMock layer is sufficient for all attributes the engine reads at runtime, but cannot serve as a base class.

**`voluptuous` handling:** If the real `voluptuous` package is importable it is used; otherwise a MagicMock is injected. This prevents schema-validation code from failing at import time in environments without `voluptuous`.

**Code reference:** `tools/sim_harness/ha_stubs.py#L124` (`install_ha_stubs`)

---

## FakeHass — Service-Bus Interception

**File:** `tools/sim_harness/fake_hass.py`

`FakeHass` is the minimal HA stand-in the `AutomationEngine` constructor and methods call into. It provides:

- `hass.services.async_call(domain, service, data, ...)` — appends to `action_log`, then applies state feedback
- `hass.states.get(entity_id)` / `hass.states.set(...)` / `hass.states.set_simple(...)` — backed by `_FakeStates` dict
- `hass.async_create_task(coro)` — hands coroutine to scheduler if wired, otherwise runs immediately
- `hass.async_add_executor_job(fn, *args)` — runs `fn(*args)` synchronously (no thread pool needed)
- `hass.config.config_dir` — returns `"/tmp/fake_ha_config"` (stub only)

**State-Feedback Loop**

`_FakeServices._apply_state_feedback()` reflects commands back into `_FakeStates` immediately:

| Domain + service | Effect on FakeState |
|---|---|
| `climate.set_hvac_mode` | `state = data["hvac_mode"]` |
| `climate.set_temperature` | `attributes["temperature"]`, `["target_temp_low"]`, `["target_temp_high"]` updated as present |
| `climate.set_fan_mode` | `attributes["fan_mode"] = data["fan_mode"]` |
| `fan.turn_on` / `switch.turn_on` | `state = "on"` |
| `fan.turn_off` / `switch.turn_off` | `state = "off"` |

Without this, production code that reads the thermostat back (e.g. `handle_occupancy_away` checking the actual HVAC mode before selecting setback_heat vs setback_cool) would see the stale initial state and diverge for the wrong reason — reproducing neither the real HA behavior nor the legacy `SimState` mutation pattern.

**Code reference:** `tools/sim_harness/fake_hass.py#L29` (`_FakeServices`), `#L118` (`FakeHass`)

---

## FakeScheduler — Virtual Clock

**File:** `tools/sim_harness/fake_scheduler.py`

`FakeScheduler` provides a deterministic priority-queue timer driver. It replaces the ad-hoc timer-capture pattern used in existing unit tests (test_override_confirmation.py, test_door_window.py) with a reusable, self-draining implementation.

**Core API:**

| Method | Description |
|---|---|
| `FakeScheduler(start)` | Create with initial virtual clock; default 2024-01-15 08:00:00 UTC |
| `scheduler.now()` | Return current virtual clock `datetime` |
| `scheduler.advance_to(target)` | Fire all due callbacks up to `target`; drain tasks after each callback |
| `scheduler.advance_by(seconds)` | Convenience: `advance_to(clock + timedelta(seconds=seconds))` |
| `scheduler.enqueue_task(coro)` | Add a coroutine to the task queue (called by `FakeHass.async_create_task`) |
| `scheduler.installed()` | Context manager: patches automation module symbols, yields `self` |

**`advance_to()` loop:**
1. Pop the earliest entry from the min-heap if `fire_at <= target`
2. Skip if `entry._cancelled`
3. Move clock to `entry.fire_at`, call `entry.callback(clock)`
4. Drain all enqueued `async_create_task` coroutines
5. Repeat — newly-scheduled callbacks (e.g. grace-expiry re-queuing fan cycles) are also fired
6. Set clock to `target`; one final task drain

**`callback_errors`:** Any exception during callback or task execution is caught, printed, and appended to `scheduler.callback_errors`. The clock never stops mid-timeline. Callers (assertions) can inspect this list; `run_scenario_production()` marks `passed = False` if any errors occurred.

### FakeScheduler — Patching Contract

`scheduler.installed()` applies five patches to `custom_components.climate_advisor.automation`:

| Symbol patched | Replacement |
|---|---|
| `async_call_later` | `scheduler._schedule(delay, cb)` — registers in the min-heap |
| `callback` | Identity (`lambda fn: fn`) — `@callback`-decorated inner functions remain real callables |
| `dt_util.now` | `lambda: self._clock` |
| `dt_util.utcnow` | `lambda: self._clock` |
| `dt_util.as_local` | `lambda x: x` (pass-through) |

**Why `automation` namespace, not `homeassistant.util.dt`?** `automation.py` does `from homeassistant.util import dt as dt_util`, binding the name in its own namespace. Patching the originating module would not affect the already-resolved name in `automation`. The `patch(...)` targets must be on the module that imported the symbol.

**Code reference:** `tools/sim_harness/fake_scheduler.py#L57` (`FakeScheduler`), `#L185` (`installed`)

---

## build_engine — Headless Engine Constructor

**File:** `tools/sim_harness/build_engine.py`

`build_headless_engine()` is the single assembly point. It:

1. Calls `install_ha_stubs()` (idempotent)
2. Imports `AutomationEngine` AFTER stubs are installed (so `automation.py` imports see the stub layer)
3. Merges `_DEFAULT_CONFIG` with the caller's `config` overrides
4. Creates `FakeScheduler(start_time)` and `FakeHass(clock_fn=scheduler.now)`
5. Calls `fake_hass.set_scheduler(scheduler)`
6. Injects the initial climate entity state into `FakeStates`
7. Creates a shared `event_log` list; wires `engine._emit_event_callback` to append to it
8. Wires `engine._revisit_callback` to an async no-op; `engine._sensor_check_callback` to `lambda: False`
9. Returns `(engine, fake_hass, scheduler, event_log)`

**`_DEFAULT_CONFIG`** (excerpt — see source for complete list):

| Key | Default | Notes |
|---|---|---|
| `climate_entity` | `"climate.test_thermostat"` | overridden per scenario |
| `comfort_heat` | `70.0` | °F |
| `comfort_cool` | `76.0` | °F |
| `automation_grace_seconds` | `300` | |
| `manual_grace_seconds` | `900` | |
| `fan_mode` | `"disabled"` | fan cycles off by default |
| `nat_vent_hysteresis_f` | `1.0` | mirrors production const (was 2.0 — wrong) |
| `nat_vent_reactivation_lockout_s` | `300` | mirrors production const |

**Why `nat_vent_hysteresis_f = 1.0`?** The production constant `NAT_VENT_HYSTERESIS_F` is 1.0°F. The old harness default was 2.0°F — a hardcoded departure that silently suppressed nat-vent activation for marginal temperature gaps, making scenarios behave unlike any real home. The harness must mirror the exact production constants.

**Code reference:** `tools/sim_harness/build_engine.py#L79` (`build_headless_engine`), `#L46` (`_DEFAULT_CONFIG`)

---

## run_production — Scenario Adapter

**File:** `tools/sim_harness/run_production.py`

`run_production_scenario(scenario)` is the top-level adapter. It:

1. Merges `scenario["config"]` over `_DEFAULT_CONFIG`
2. Determines `start_dt` from the first event time (minus 1 second)
3. Calls `build_headless_engine()` with merged config and initial thermostat mode
4. Creates a `_SensorTracker` and wires it to `engine._sensor_check_callback`
5. Injects `thermal_model` and `hourly_forecast` from top-level scenario keys if present
6. Enters `scheduler.installed()` context
7. For each event (sorted by time): `scheduler.advance_to(event_dt)`, then `_dispatch_event(...)`
8. Calls `_snapshot_engine_state(engine)` to capture final flags
9. Returns `ProductionRunResult`

### run_production — Event Dispatch Table

| Event type | Production method called |
|---|---|
| `temp_update` | `engine.update_outdoor_temp(outdoor_f)` + indoor inject + `engine.check_natural_vent_conditions()` + optional re-classification |
| `sensor_open` | `engine.handle_door_window_open(entity_id)` |
| `sensor_close` | If all closed: `engine.handle_all_doors_windows_closed()` |
| `classification` | `engine.apply_classification(classification)` |
| `occupancy_away` | `engine.set_occupancy_mode("away")` + `engine.handle_occupancy_away()` |
| `occupancy_home` | `engine.set_occupancy_mode("home")` + `engine.handle_occupancy_home()` |
| `occupancy_vacation` | `engine.set_occupancy_mode("vacation")` + `engine.handle_occupancy_vacation()` |
| `occupancy_change` | Dispatches to away/home/vacation by `event["mode"]` |
| `occupancy_change_with_override` | Sets `engine._manual_override_active = True`, then dispatches |
| `bedtime` | `engine.handle_bedtime()` |
| `wakeup` | `engine.handle_morning_wakeup()` |
| `economizer_check` | `engine.check_window_cooling_opportunity(...)` |
| `thermostat_state_changed` | Injects thermostat state + `engine.handle_manual_override(...)` if conditions met |

`_build_classification_from_event()` uses `object.__new__(DayClassification)` to bypass `__post_init__` (which would overwrite scenario-specified fields like `hvac_mode` by re-deriving them from `day_type`).

### run_production — ODE Inputs

Top-level scenario keys for ODE ceiling guard testing:

```json
{
  "thermal_model": {
    "confidence": "high",
    "k_passive": -0.18,
    "k_active_cool": -2.1,
    "k_active_heat": 3.2
  },
  "hourly_forecast": [
    {"datetime": "2024-01-15T08:00:00", "temperature": 72.0}
  ]
}
```

These are injected as `engine._thermal_model` and `engine._hourly_forecast_temps` before event dispatch. The legacy simulator had no ODE and ignored these keys; they only affect the production harness path.

### run_production — predicted_indoor Re-Classification

Within a `temp_update` event, an optional `predicted_indoor` array re-invokes `apply_classification()` with the current classification and the curve:

```json
{
  "type": "temp_update",
  "time": "2024-01-15T10:00:00",
  "outdoor_f": 82.0,
  "indoor_f": 73.0,
  "predicted_indoor": [
    {"ts": "2024-01-15T10:00:00", "temp_f": 73.0},
    {"ts": "2024-01-15T14:00:00", "temp_f": 79.5}
  ]
}
```

This models the coordinator's 30-minute re-classification cycle so the ODE ceiling guard (which lives inside `apply_classification`) can be tested in Tier A without requiring the full coordinator loop.

### run_production — Unmapped Event Types

These event types have no clean production entry point and are silently skipped by the adapter:

| Event type | Reason |
|---|---|
| `fan_cycle_on` | Internal timer callback only; production triggers via `start_min_fan_runtime_cycles()` + scheduler timers |
| `fan_cycle_off` | Same; `advance_to()` drives the timer that covers this |
| `grace_start` | Production grace starts automatically from sensor events; no external `start_grace()` method |
| `grace_end` | Same; grace expiry fires via scheduler timer driven by `advance_to()` |

**Code reference:** `tools/sim_harness/run_production.py#L239` (`run_production_scenario`), `#L325` (`_dispatch_event`)

---

## outcomes — Event-to-Outcome Mapping

**File:** `tools/sim_harness/outcomes.py`

`production_decisions(result)` converts `ProductionRunResult.event_log` into a time-ordered list of `ProductionDecision` entries using the legacy outcome vocabulary, so scenario `expect:` strings continue to work.

### Mapped Event Types (production → legacy outcome)

| Production event type | Condition | Legacy outcome |
|---|---|---|
| `sensor_opened` | `result == "natural_ventilation"` | `natural_ventilation` |
| `sensor_opened` | `result == "paused"` | `paused` |
| `sensor_all_closed` | (always) | `resumed` |
| `classification_applied` | — | `classification_applied` |
| `warm_day_comfort_gap` | — | `warm_day_comfort_gap` |
| `warm_day_setback_applied` | — | `setback_applied` |
| `nat_vent_comfort_floor_exit` | — | `nat_vent_comfort_floor_exit` |
| `nat_vent_outdoor_rise_exit` | — | `nat_vent_outdoor_rise_exit` |
| `ceiling_guard_fired` | — | `ceiling_guard_fired` |
| `bedtime_setback` | — | `setback_applied` |
| `bedtime_setback_skipped` | — | `bedtime_setback_skipped` |
| `occupancy_setback` | — | `setback_applied` |
| `occupancy_comfort_restored` | — | `comfort_restored` |
| `morning_wakeup` | — | `comfort_restored` |
| `morning_wakeup_skipped` | — | `morning_wakeup_skipped` |
| `grace_expired` | `re_paused=True` | `paused` |
| `grace_expired` | `re_paused=False` | `resumed` |
| `override_detected` | — | `override_detected` |
| `override_confirmed` | — | `override_confirmed` |
| `override_self_resolved` | — | `override_self_resolved` |
| `override_cleared` | — | `override_cleared` |

**`target_temp` enrichment:** Production emits a decision event (`classification_applied`, `bedtime_setback`, etc.) and the setpoint via a separate `climate.set_temperature` action at the same virtual-clock instant. `_temps_by_timestamp()` builds a map from naive-ISO timestamp to setpoint, and `production_decisions()` fills `target_temp` on each decision from this map.

**Unmapped production-only events** (`UNMAPPED_PRODUCTION_EVENTS`): `warm_day_state_confirmed`, `nat_vent_away_ceiling_exit`, `nat_vent_ceiling_escalation`, `nat_vent_forecast_skip`, `nat_vent_floor_imminent_skip`, `nat_vent_predicted_floor_exit`, `grace_started`, `incident_detected`. These are silently skipped by `_map_event_to_outcome()`.

### Assertion Surface

Assertions are evaluated by reading three sources:

| Source | What it contains |
|---|---|
| `result.event_log` | Engine decisions: event types, payloads, timestamps |
| `result.action_log` | Service calls: domain, service, data, timestamp |
| `result.engine_state` | Final engine flag snapshot: `_natural_vent_active`, `_manual_override_active`, `_grace_active`, `_paused_by_door`, `_fan_active`, `_fan_override_active`, `_occupancy_mode`, `_override_confirm_pending`, `_economizer_active`, `_current_classification` |

### outcomes — Custom Assertion Types

`check_assertion()` handles these `expect` values by reading engine state and/or event_log directly:

| `expect` value | What it checks | Occupant guarantee |
|---|---|---|
| `nat_vent_not_active` | `engine_state["_natural_vent_active"] is False` | HVAC is active (not suppressed by nat-vent) |
| `nat_vent_still_active` | `engine_state["_natural_vent_active"] is True` | Windows still in control; HVAC not running |
| `override_active` | `engine_state["_manual_override_active"] is True` | User's manual setting is still respected |
| `override_cleared` | `engine_state["_manual_override_active"] is False` | CA has resumed automated control |
| `nat_vent_fan_preserved` | `_natural_vent_active is True AND _fan_active is True` | Occupant still has airflow during nat-vent; regression would stop the fan |
| `dual_setback_applied` | `action_log` has a `climate.set_temperature` with both `target_temp_low` and `target_temp_high` | Both heat and cool setpoints applied; regression dropping one would run AC while away |
| `ceiling_guard_fires_cool` / `ceiling_guard_would_fire` | `production_outcome_at(decisions, at) == "ceiling_guard_fired"` | Pre-cooling fired before forecast peak; occupant avoids overheating |
| `ceiling_guard_dormant*` | `production_outcome_at(decisions, at) != "ceiling_guard_fired"` | Guard did not fire; no unnecessary pre-cooling |
| `setpoint_consistent_with_mode` | Weak cross-check of last `set_hvac_mode` vs `set_temperature` in action_log | Setpoint and mode are not contradictory |

**Code reference:** `tools/sim_harness/outcomes.py#L143` (`production_decisions`), `#L346` (`check_assertion`)

---

## tools/simulate.py — CLI Shell

`tools/simulate.py` (~657 lines) is a thin shell around `run_production.py`. It owns:
- CLI argument parsing (`--pending`, `-s NAME`, `--list`, `--cases`, `-v`, `--report`, `--check-integrity`, `--sign`)
- `run_scenario_production(scenario_file, state)` — loads JSON, calls `run_production_scenario()`, formats assertions
- Tier separation: `track: "integration"` → `skipped: True`; `simulator_support: false` → evaluated normally
- Output formatting (`print_result`, `_status_label`)
- MANIFEST integrity (`check_integrity`, `sign_scenario`)
- Markdown report generation (`write_report`)

There is no `--engine` flag. The only engine is production.

**Code reference:** `tools/simulate.py#L71` (`run_scenario_production`)

---

## tests/test_production_harness.py — Tier A Enforcement

`test_golden_passes_production_engine` is parametrized over every `.json` in `tools/simulations/golden/` (excluding `MANIFEST.json`). For each scenario:
- Calls `run_scenario_production(scenario_path, state="golden")`
- Asserts `result["callback_errors"]` is empty (unexpected engine errors = untrustworthy run)
- Asserts `result["passed"] is not False` (`True` = assertions passed; `None` = only integration/deferred assertions, acceptable)

`test_all_goldens_discovered` guards against an empty parametrization silently passing: asserts `len(golden_files) >= 20`.

**Code reference:** `tests/test_production_harness.py#L32` (`test_golden_passes_production_engine`)

---

## State Transitions — Tier A Scenario Lifecycle

| From | Trigger | To | Side effects |
|---|---|---|---|
| `pending/` | Author runs `--pending -v`, reviews output | `golden/` or `pending-fix/` | `--sign <name>` updates MANIFEST.json; `mv` to golden/ |
| `golden/` | `test_production_harness.py` parametrized test | (stays golden) or FAIL | FAIL = regression; code or scenario needs investigation |
| `golden/` | User authorizes modification | modified `golden/` | `--sign <name>` required; `--check-integrity` confirms |
| `pending/` | Behavior confirmed out of scope | `unsupported/` | Never delete — documents deliberate scope decisions |

---

## Error Conditions

| Failure | Handling | Caller receives |
|---|---|---|
| Unexpected exception during callback/task | Caught, printed, appended to `scheduler.callback_errors` | `ProductionRunResult.callback_errors` non-empty; `passed = False` |
| `advance_to()` called with past target | `ValueError` raised immediately | Exception propagates to test |
| Scenario JSON missing `events` key | `scenario.get("events", [])` returns `[]`; run produces empty `event_log` | `passed = None` (no assertions evaluated) |
| `build_headless_engine()` import error (HA stubs not installed) | Raised during `from custom_components... import AutomationEngine` | `ImportError` propagates |
| Unknown event type in scenario | Silently ignored (mirrors `simulate.py`'s `return None` fallback) | No decision recorded; assertion at that time returns `"no_decision"` |
| `track: "integration"` assertion | Skipped with explicit reason string | `skipped: True` in assertion result; never counts toward `passed` |

---

## Code Reference

- [`install_ha_stubs`](../tools/sim_harness/ha_stubs.py#L124) — idempotent HA stub installer
- [`FakeHass`](../tools/sim_harness/fake_hass.py#L118) — minimal HA stand-in
- [`_FakeServices._apply_state_feedback`](../tools/sim_harness/fake_hass.py#L70) — state-feedback loop
- [`FakeScheduler`](../tools/sim_harness/fake_scheduler.py#L57) — virtual clock + priority-queue timer driver
- [`FakeScheduler.installed`](../tools/sim_harness/fake_scheduler.py#L185) — patching context manager
- [`build_headless_engine`](../tools/sim_harness/build_engine.py#L79) — engine assembly
- [`_DEFAULT_CONFIG`](../tools/sim_harness/build_engine.py#L46) — harness default config
- [`run_production_scenario`](../tools/sim_harness/run_production.py#L239) — top-level scenario adapter
- [`_dispatch_event`](../tools/sim_harness/run_production.py#L325) — event-type → engine method dispatch
- [`_handle_temp_update`](../tools/sim_harness/run_production.py#L441) — temp_update + predicted_indoor re-classification
- [`production_decisions`](../tools/sim_harness/outcomes.py#L143) — event_log → legacy outcome vocab
- [`check_assertion`](../tools/sim_harness/outcomes.py#L346) — custom assertion types
- [`UNMAPPED_PRODUCTION_EVENTS`](../tools/sim_harness/outcomes.py#L89) — production-only events with no legacy outcome
- [`run_scenario_production`](../tools/simulate.py#L71) — CLI shell entry point
- [`test_golden_passes_production_engine`](../tests/test_production_harness.py#L32) — Tier A parametrized golden test
