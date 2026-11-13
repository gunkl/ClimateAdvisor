<!-- Nav: ← [sim-harness-brief.md](sim-harness-brief.md) | → [tools/sim_harness/](../tools/sim_harness/) | ↔ [simulation-feedback-loop.md](simulation-feedback-loop.md) -->

# Production Simulation Harness — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| How does `FakeHass` prevent stale thermostat read-backs? | `_FakeServices._apply_state_feedback()` reflects every `climate`, `fan`, and `switch` command into `_FakeStates` immediately after recording it in `action_log`, before the engine's next read; the harness thermostat reports `hvac_action` by default from its own loop (Issue #1537; opt out with `thermostat_reports_hvac_action: false`), and CA's mode changes and new readings update it the way a real thermostat would (idle past the setpoint, Issues #1310/#1316). | [FakeHass — State-Feedback Loop](#fakehass--service-bus-interception) |
| How does the virtual clock fire grace-period timers and fan-cycle chains? | `FakeScheduler.advance_to(target)` pops due callbacks from a min-heap, fires each, drains `async_create_task` coroutines, then loops — so self-recursive grace→convergence chains complete without manual intervention. | [FakeScheduler — Virtual Clock](#fakescheduler--virtual-clock) |
| What symbols does `FakeScheduler.installed()` patch, and in which module? | `async_call_later`, `callback` (identity), `dt_util.now`, `dt_util.utcnow`, `dt_util.as_local` (HA-faithful: naive input = clock's local zone, result always aware; Issue #1117) — patched on `custom_components.climate_advisor.automation` (and the same set plus timer helpers and `parse_datetime` on `coordinator`), not the originating HA module. | [FakeScheduler — Patching Contract](#fakescheduler--patching-contract) |
| What does `run_production_scenario()` do with `thermal_model` and `hourly_forecast`? | If present as top-level keys in the scenario dict, they are seeded into their owner before event dispatch begins (Issue #1443/#1440): in coordinator mode the coordinator (`coordinator._thermal_model` plus `pin_scenario_thermal_model`, `coordinator._hourly_forecast_temps`), which passes them in at each engine decision; in engine-only mode a `StaticEngineInputs` passed to `build_headless_engine(inputs=…)`. No engine private is written. The real-HA adapter seeds the coordinator the same way. | [run_production — ODE Inputs](#run_production--ode-inputs) |
| How does `predicted_indoor` in a `temp_update` event exercise the ODE ceiling guard? | After injecting temperatures and calling `check_natural_vent_conditions()`, `run_production` checks for `event["predicted_indoor"]`; if present and a classification exists, it calls `apply_classification(classification, predicted_indoor=predicted)` again — modelling the coordinator's periodic re-classification cycle. | [run_production — predicted_indoor Re-Classification](#run_production--predicted_indoor-re-classification) |
| Which event types have no clean production entry point? | `fan_cycle_on`, `fan_cycle_off`, `grace_start`, `grace_end` — all driven internally by scheduler timers in production. The adapter skips them; `advance_to()` drives the timers that cover the same transitions. | [run_production — Unmapped Event Types](#run_production--unmapped-event-types) |
| How are legacy `expect:` strings matched when production emits different event types? | `outcomes.production_decisions()` maps production event types to the legacy vocabulary (e.g. `bedtime_setback` → `setback_applied`, `sensor_opened result=natural_ventilation` → `natural_ventilation`). Unmapped production-only events are collected in `UNMAPPED_PRODUCTION_EVENTS` and silently skipped. | [outcomes — Event-to-Outcome Mapping](#outcomes--event-to-outcome-mapping) |
| What custom assertion types exist and what guarantee do they test? | `nat_vent_not_active`, `nat_vent_still_active`, `override_active`, `override_cleared`, `nat_vent_fan_preserved`, `dual_setback_applied`, `ceiling_guard_fires_cool`, `ceiling_guard_dormant*`, `setpoint_consistent_with_mode`, and (Issue #1194 e) `commanded_setpoint_at` / `setpoint_writes`. Each tests the occupant-visible guarantee, not a label. | [outcomes — Custom Assertion Types](#outcomes--custom-assertion-types) |
| How does `ha_stubs.py` avoid metaclass conflicts between MagicMock and HA base classes? | It replaces MagicMock entries with real minimal base classes for `DataUpdateCoordinator`, `CoordinatorEntity`, `SensorEntity`, `RepairsFlow`, `ConfirmRepairFlow`, and enum types. These are the only bases that cause metaclass conflicts when subclassed by production code. | [ha_stubs — Idempotent Stub Installer](#ha_stubs--idempotent-stub-installer) |
| Why do scenarios run with `primary_engine: v3`, and how does a scenario exercise v4? | `merged_scenario_config` pins v3 (goldens were signed against it; a scenario home has no v4 fit); a scenario states `config.primary_engine: "v4"` plus a `v4_model` spec and uses `plan_cycle: true` on a `temp_update`. | [Primary Prediction Engine in Scenarios](#primary-prediction-engine-in-scenarios-issue-1230-r6b) |
| What is the Session seam, and what do generated days, promises and surfaces add? | One `Session` protocol (`session.py`) is implemented by `FakeHassSession` (fast default) and `RealHASession` (real Home Assistant); `promise_probe.py` judges any run with the production promise registry, `generator.py` draws closed-loop days against a toy house, `surfaces.py` is test-only Promise 8. Three ratchet ledgers record known divergences. | [Session Seam, Generated Days and Promises](#session-seam-generated-days-and-promises-issues-1127-1128) |
| What is the bedroom node, and how are the #1194 remote-sensor scenarios run? | `world.BedroomNode` is an optional second thermal node (coupling, lag, register plume on the sensor, `last_reported`), off by default and byte-identical to before when absent. Seventeen Tier A scenarios in `pending/remote_offset_1194e_*.json` run the real coordinator; the `remote_sensor_report` event, the `thermostat_echo_delay_s` config key and the `commanded_setpoint_at` / `setpoint_writes` assertions are the hooks; Tier B is `tests_ha/test_remote_offset_sim_1194e.py`. | [Remote Bedroom Node and the #1194 Scenarios](#remote-bedroom-node-and-the-1194-scenarios-issue-1194-e) |

---

## Scope

This section covers the original harness modules in `tools/sim_harness/` and the CLI shell `tools/simulate.py` (the Session seam, generator and promise modules have their own section above):

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

## Primary Prediction Engine in Scenarios (Issue #1230 R6b)

The primary prediction engine drives the ceiling guard, the nat-vent plan, the TOU lead and the setback rates ([Prediction Engines Spec](prediction-engines-spec.md#control-feed-r6b)). Scenario consequences:

| Item | Rule |
|---|---|
| **Primary pin** | `scenario_model.merged_scenario_config` seeds `primary_engine: "v3"` under the scenario's own `config` (`SCENARIO_PRIMARY_ENGINE`). Every golden was signed against v3's behaviour (`thermal_model` pinned) and a scenario home has no fitted v4 model; under the production default (v4) the decisions would take the learning-not-finished defaults and `warm_day_ceiling_breach_ac_defense` (and its `__hvac_fan` twin) would read `resumed` instead of `ceiling_guard_fires_cool`. Test-infrastructure justification: it returns the goldens to the engine they were signed against and changes no assertion or outcome mapping; it cannot hide a real failure because the v4-primary behaviour has its own scenarios (`pending/r6b_*.json`). Direct builders (`build_headless_coordinator` in unit tests) keep the production default. |
| `config.primary_engine` | A scenario may state `"v4"`; with it `v4_model` is required for a usable fit. |
| `v4_model` | Compact spec (`{"confidence", "primary" (default `_ca`), "house": {...}}`) or `{"model": <whole stored model>}`; `scenario_model.pin_scenario_v4_model` installs it as the zone's fitted model and stops the coordinator refitting it (a run across the nightly 03:15 fit would otherwise replace it with the scenario home's empty-history fit). Real-HA tier: `RealHASession._apply_injected_inputs` does the same. |
| `plan_cycle: true` on `temp_update` | Models the coordinator's periodic plan cycle: `_refresh_control_prediction()` (the primary predicts), `_compute_and_cache_nat_vent_plan()`, then `apply_classification(predicted_indoor=control_predicted_indoor(), nat_vent_cutoff=...)`. An event that only carries `predicted_indoor` (every golden) behaves as before: with the v3 pin the control curve IS the event's curve. |
| Snapshot | `engine_state._nat_vent_cutoff` (ISO string or `None`) and `_nat_vent_cutoff_already_reached` were added to the final snapshot (additive; no existing assertion reads them). |
| Existing goldens under v4 | Without the pin (and, since R6, with the guard sending nothing when the thermostat already cools at or below its ceiling, the signed `warm_day_ceiling_breach_ac_defense` asserts the 12:30 thermostat state: no mode write, setpoint 74, guard dormant; owner-approved edit), 1 of the 95 goldens (`warm_day_ceiling_breach_ac_defense`) and 1 of the 98 earlier pending scenarios (its `__hvac_fan` twin) change: `ceiling_guard_fires_cool` becomes `resumed` (v4 has no fit in the scenario, so the guard reads no learned rates). The other 94 goldens and 97 pending scenarios do not depend on the primary. |

## Session Seam, Generated Days and Promises (Issues #1127, #1128)

Epic #1125. Plans: `plans/test-automation/` (`02-step2-design.md` real-HA tier, `03-step3-design.md` promises, `04-retirement-waves.md` proposal only). Promise definitions live in [household-promises-spec.md](household-promises-spec.md), not here.

### Files

| File | Role |
|---|---|
| `tools/sim_harness/session.py` | The ONE runner seam. `Session` protocol (`feed`, `advance_to`, read-only `thermostat` view, `finish` returning a `RunResult`), `drive_scenario`, `make_session(scenario, engine="fakehass" or "realha", probe=None)`. No HA imports, installs no stubs. |
| `tools/sim_harness/scenario_model.py` | Pure, stub-free helpers lifted from `run_production.py`: `ProductionRunResult` (alias `RunResult`, with an optional `promise_report`), `parse_event_time`, config merge / start-time rule, hand-built `DayClassification`, final engine-state snapshot. |
| `tools/sim_harness/run_production.py` | `FakeHassSession`, the FakeHass adapter (behavior-preserving extraction of the old `run_production_scenario` body). With a probe attached, time advances in slices of at most 5 min so the probe also sees quiet stretches; `tests/test_goldens_promises.py` proves the logs are identical. |
| `tests_ha/realha_session.py` | `RealHASession`, the real-Home-Assistant adapter: real core, real config entry and coordinator, dev thermostat sim, frozen clock, service-call recorder. `feed`/`advance_to`/`finish` are coroutines. Returns the same `RunResult`, so `outcomes.py` judges both worlds. Imports no stubs. With no weather entity the refresh never reaches production's start-up coalescing gate, so the adapter completes that gate after each real refresh: start-up/restart windows end at +300 s (Issue #1309; docs/08 §11). Real-HA tier only (`python scripts/test.py ha`; WSL on Windows). |
| `tools/sim_harness/promise_probe.py` | `PromiseProbe` plus `FakeHassPromiseSource`: collects readings, commands and CA events, records them in the production `PromiseJournal`, calls production `run_promise_checks(..., scope="test")`. Owns no promise logic. Installs stubs (tests/tools only). |
| `tools/sim_harness/generator.py`, `world.py`, `replay.py`, `trial_gate.py` | Seeded random days against a toy house; replay and shrink; the Step 3 trial. |
| `tools/sim_harness/surfaces.py` | Promise 8 `surfaces_agree` (test-only, not in the production `PROMISES` registry). |
| `tools/sim_harness/divergence.py` | FakeHass-vs-real-HA comparison, `judge_scenario`, and the real-HA ledger. |

### Two engines

`engine="fakehass"` (default) is the production `AutomationEngine`/coordinator under FakeHass + FakeScheduler: fast, and the engine for `tools/simulate.py` and Tier A. `engine="realha"` runs the same scenario against real Home Assistant (`tests_ha/`). The two are compared, not merged: a golden that fails on real HA must be listed in the ledger. Tier B is this real-HA tier; HeadlessTarry is a proposal that is not in the repository.

### Generated days

A plan drawn from `random.Random(day_seed)` (config, weather, occupancy, doors, occupant pokes) is walked in 5-minute ticks against a `Session`, with a `ToyHouse` (`world.py`) answering with indoor temperature and `hvac_action`. The realised events are written as an ordinary scenario file, so replay needs no generator. Identity: `day_seed = master_seed * DAYS_PER_MASTER + index` (`DAYS_PER_MASTER = 1_000_000`). Profiles: `coordinator` (default), `engine`, `mixed`. Findings are saved under `tools/simulations/generated/failures/` (gitignored). CI smoke: seeds 101 and 102, 50 days each (`CI_SMOKE_*` in `generator.py`). Nightly (`.github/workflows/nightly.yml`): 12 fresh master seeds x 250 days by default.

### CLI

| Command | Does |
|---|---|
| `python tools/simulate.py --generate N --seeds S [S ...] [--profile P]` | N generated days per master seed, judged by the promises |
| `python tools/simulate.py --generate-day DAY_SEED [--profile P]` | generate and judge ONE day by its seed (replay a seed) |
| `python tools/simulate.py --replay-generated FILE [--shrink]` | replay a saved finding; `--shrink` also writes a minimal copy |
| `python tools/simulate.py --surfaces [--generate N --seeds S ...]` | Promise 8 over every golden (and generated days) |
| `python -m tools.sim_harness.trial_gate --seeds 1 2 3 4 5 --days 200 [--profile P] [--promise settling_flip] [--out F]` | the Step 3 trial; run from the root of the tree under test |
| `python tools/sim_harness/divergence.py dump-fake [scenario ...] [--out DIR]`, then `compare [--fake DIR] [--real DIR] [--out DIR]` | FakeHass-side dump; diff against the dumps `python scripts/test.py ha -m goldens` writes |
| `python -m tools.sim_harness.replay [FILE] [--shrink] [--generate-day N] [--generate N] [--seeds ...]` | the module `simulate.py` delegates to |

### Three ratchet ledgers

| Ledger | Records | Rule |
|---|---|---|
| `tools/sim_harness/realha_ledger.json` | goldens that fail on real HA, with class, failing assertion indexes, evidence | an unlisted failure fails; a listed scenario that now passes or fails differently fails (stale entry) |
| `tools/simulations/surface_divergences.json` | known P8 divergences (per golden, or the fixed generated sample: seeds 101/102, 10 days) | same two-way ratchet |
| `tools/sim_harness/promise_waivers.json` (tolerated hard hits on locked goldens) and `tools/sim_harness/promise_baseline.json` (warning counts per golden) | tolerated promise hits | waivers: two-way as above; baseline: may go down, never up |

Goldens are LOCKED; a divergence is recorded in a ledger, never by editing a scenario.

**Occupant thermostat changes land off the minute (Issue #1527).** The generator gives each drawn occupant thermostat change (`thermostat_state_changed`) a random whole second, 5 to 55, within its minute, drawn after every other draw so the rest of each seed is unchanged. Event times are snapped to whole seconds, as `_iso()` reports them; no other event has a sub-second offset. Climate Advisor writes on the minute (wake-up, bedtime, each 30-minute cycle) and by design reads a mode change within 3 s of its own write as that write reporting back (08 occupant-change guard table, owner decision D6). A whole-minute occupant change could land in that same second and be judged a Climate Advisor failure (run 43037566357, day 17003000036, a false P1). Tests: `tests/test_generator_occupant_seconds_1527.py`.

**Real-HA ledger history.** The ledger started on 2026-10-30 (#1125) with 8 entries: 5 class T (trigger divergence: the scenario models fewer inputs than real HA delivers) and 3 class P (real production bugs the first real-HA replay exposed: `wakeup_preserves_whf_manual_override`, `restart_whf_ac_mutex_backstop_race`, `ceiling_guard_fires_when_outdoor_stays_below_indoor`). Each P entry was removed by its fix (`ceiling_guard_fires_when_outdoor_stays_below_indoor` by #1144; the other two by #1130); the class-T `window_close_stops_whole_house_fan` was retired by #1144 and `warm_day_planned_window_no_pause` by #1211, which also added `2026-03-28-overnight`. One class-G entry was added on 2026-11-07 by #1420: `warming_trend_pre_cool_applied` (the scenario's classification has no forecast highs, so production's own 02:30 trigger skips the night at its fire-time re-check; the scenario's direct handler call had hidden that). It was removed in 0.9.8 by fixing the scenario's premise (owner decision): its 14:00 classification now carries the forecast (today 80/62 °F, tomorrow 95/77 °F), a 00:00 reclassification onto the new day (95/77 °F) models the coordinator's first cycle after midnight, and it runs `use_coordinator`, so the real trigger's #1378 re-check sees the +15 °F trend and pre-cools to 69 °F on both tiers; a new `setpoint_writes` row pins the real 69 °F service call (the `pre_cool_applied` event row alone still passed with the write deleted, since the event is announced whatever the write's outcome). Its control `warming_trend_pre_cool_revised_away_1378` revises the waking day to 82/62 °F overnight and asserts no 02:30 write. Four class-T entries remained (2026-11-03). `733_restart_reconcile_clobbers_fresh_nat_vent` was removed in 0.9.23 by #1500: its "silent seed write" explanation was incomplete. On real HA the door/window listener starts nat-vent at 23:15, and the 23:19 startup reconcile on that command-only whole-house fan read the idle thermostat blower as "fan off" and recorded the fresh session stopped, so the coalesce started it a second time. With the reconcile reading Climate Advisor's own record it keeps the session, as the golden expects (the golden is unchanged). Three remain: `cool_all_day_heat_all_day` and `2026-03-28-overnight` (engine-only goldens that omit the daily wake/bedtime schedule real HA runs), `issue-392-natvent-ceiling-oscillation` (an atomic two-reading update has no real-HA equivalent). All three are marked "awaiting human review"; none is a known occupant-impacting defect except the half-updated-pair exit in issue-392, which is an open design question.

**Excused time (Issue #1260).** Time a promise did not count under a named exception (today only P7's thermostat deadband, `excused_by=thermostat_deadband`) is not a hit and is in none of the ledgers above. It is reported beside hits: `PromiseReport.excused` per run and `excused_by_promise` (runs and minutes) in the nightly batch summary and the `replay` line, so a rise is visible. Minutes are counted from the excused intervals past a per-series `counted_until` (Issue #1262), so a stretch longer than the 3 h journal window is reported in full.

**Occupancy events go through the coordinator's occupancy-change entry (Issue #1409, test-infrastructure change).** `run_production`'s `occupancy_away/home/vacation/change` events used to call the engine handlers directly. Production now runs every occupancy change through `coordinator._run_occupancy_handler()` (the toggle and its delayed away timer), which lets an open time-of-use bank take the change's write; the direct call modelled a path production no longer takes, so a #1409 regression could not be seen. With a coordinator the event now calls that entry (`_occupancy_handler` in `run_production.py`); engine-only sessions keep the direct call. Could it hide a failure? No: the engine handler is the same call, the wrapper only sets the flag production sets, and all 120 goldens are unchanged.

**Schedule events go through the production timer (Issue #1420, test-infrastructure change, dispatch only).** A coordinator-backed session (Tier A `use_coordinator`, and every real-HA run) runs the coordinator's own timers: the daily `wake_time` / `sleep_time` timers and the overnight pre-cool trigger. A `bedtime`, `wakeup` or `pre_cool` event in the same minute used to call the engine handler as well, so the test saw a second write production never makes: two pre-cool writes on real HA, two wake-up writes in Tier A. Now the event only moves the clock (real-HA dispatch mode `time-travel`) when that timer is in the event's minute: `scenario_model.daily_schedule_fires_at()` reads the time the coordinator scheduled (its config and defaults, so a scenario without `sleep_time` matches 22:30), and `scenario_model.record_pre_cool_fires()` (a reference-counted class-level wrapper that calls the real trigger unchanged) notes each pre-cool fire for `pre_cool_timer_fired_at()`. Any other minute, and every engine-only Tier A session (no timers), keeps the direct call. `outcomes.py` is untouched. Could it hide a failure? No: the write still comes from production, by the real coordinator callback, which now also runs its fire-time checks (#1378's re-check of the night's eligibility) that the direct call skipped; a deleted timer now fails the scenario instead of being masked by the event. That exposed `warming_trend_pre_cool_applied` (ledger class G: its classification carried no forecast highs, so the real 02:30 trigger skipped; fixed in 0.9.8 by giving the scenario the forecast, see the ledger history above). Test: `tests/test_schedule_event_dispatch_1420.py`.

**P8 armed reference during a time-of-use bank (Issue #1404, test-infrastructure change).** `surfaces.read_armed()` used to be `engine._resolved_comfort_band(c)` alone, the schedule band. That meaning was WRONG during a bank: since #1393/#1395 the bank owns the setpoint, the cycle does not write the schedule band at all and the thermostat is told the bank target (cool 77 °F while the Away band is 60–80 °F), so "armed" named a band the engine does not arm then, and `tou_away_bank_writes_once_1395` reported the chart (which drew the bank) as the divergent surface. `read_armed()` now reads the production display resolver `coordinator.current_hold().band`, which IS `_resolved_comfort_band(c)` whenever no bank owns the thermostat (outside the window, a deferred bank, no schedule) and the bank band while one does; it is never re-derived in the harness (a coordinator-less session keeps the engine band). The chart reader reads `coordinator._chart_display_band()`, the function `get_chart_data()` draws from (falling back to `_build_target_band_for()` on a pre-#1404 tree). Nothing was removed from the check and no ratchet row was added. *Could this let a real future failure pass silently?* Only if `current_hold()` itself claimed a bank the thermostat is not told, and every surface then agreed with that wrong claim. Three things stop that: `current_hold()` claims a bank only when the bank write's own deferral checks pass (`tou_bank_holds_setpoint()`, unit-tested per deferral), the `thermostat_hold` reader and the goldens' `setpoint_writes` rows compare against the real service calls, and the #1404 goldens assert both the writes and the quoted band; outside a bank the reference is byte-for-byte the old one, so every non-bank sample is judged exactly as before. `capture_next_action()` (additive) also records `hold` (Status, thresholds and their context, the briefing HVAC Mode row, the chart band now, the plan list's Now text) so a scenario can pin what each surface says.

**P8 `chart_strips` surface (Issue #1436, test-infrastructure change).** Semantic justification: P8 compared every surface that quotes a band with the armed band, and nothing compared the chart's activity strips with anything. That coverage was WRONG for the strips: since R3b the occupant is told the plan (card, list, briefing all read the timeline), but the faded strips came from `predicted_activity`, a second forward computation, so a chart telling the occupant the opposite of the plan ("Nat-vent" all night under "close the windows by 7:14 PM", #1436) passed every check. The new surface's reference is the plan itself (the timeline's window periods via the briefing's own `window_periods` + `is_direction_vetoed`, its runs, its setback and resume steps), read with production functions; the readers are the real `timeline_payload()` and the real `get_chart_data()` (the only shim is the flat hourly forecast `read_chart_band` already uses when a run has no weather forecast, restored after). Could it let a real failure pass silently? Only for what it does not sample: it judges the server payloads, not the canvas pixels (the Playwright spec `tests/ui/strips-plan.spec.js` and `check_chart_chips.py` cover that the page draws exactly those spans), and it samples on plan changes and at most every 2 virtual hours, so a divergence that lives for less than 2 h between two unchanged plans could be missed; the grid itself is every 30 min over the next 24 h. It cannot hide a band divergence: the other surfaces are unchanged and the ledger is untouched. **Follow-up in the same PR (independent review of d57488f).** The first version was one-directional and grid-only, and the review reproduced six tampered strips it passed: Win plan or Nat-vent running 10 minutes past the close (#1439's shape; a 30-minute grid sampled at :15/:45 cannot see under 15 minutes), a Nat-vent, Fan or Heating/Cooling strip dropped while the plan holds the session or run, and Nat-vent drawn over a window period with no session; Away's end and Override's end were unchecked. That coverage was wrong for what P8 promises (every surface tells the plan's story), so the reference now answers both directions per strip (`strip_check._Reference`: Nat-vent from the plan's sessions AND its window periods, runs from their placed minutes, Away to midnight or the horizon, Override to the resume), samples 30 s either side of every span end, step time and run/burst bound as well as the grid, and compares the Win plan endpoints exactly. Could it now let a real failure pass? Overruns under 30 s, and anything the payload carries but the canvas draws differently (covered by `tests/ui/strips-plan.spec.js`); the reference still reads the plan through production's `window_periods`, so a pairing bug there would move both sides together (the plan itself is pinned by goldens and `test_operation_timeline*`). Each reproduced case is a unit test (`tests/test_chart_strips_1436.py::test_strip_check_catches_each_tampered_span` and the Nat-vent, Away and Override tests).

**Planning step before reading (Issue #1436 mirror audit, test-infrastructure change).** Production's surface reads no longer build the plan (a mid-cycle read latched it from half-updated inputs); only planning steps do, and Tier A runs no 30-minute cycle. `scenario_model.run_planning_step` runs production's own `_timeline_shadow_run("cycle")` before `capture_next_action` and before each P8 sample reads, as the cycle does before it computes the surfaces. Why the old harness was wrong: it got its plan from the very read-builds-the-plan behaviour that was the bug. It cannot hide a read that builds (unit tests pin that no read path reaches a build), and the plan commands nothing, so the run is unchanged.

The scenario capture (`capture_next_action`) gains `chart_strips` (`strips`, `activity_windows_hours`, `activity_fan_hours`, `problems`, `agree`), additive, shared with the real-HA adapter.

**Code reference:** `tools/sim_harness/session.py`, `tools/sim_harness/generator.py` (`run_batch`, `run_day`, `replay_scenario`), `tools/sim_harness/surfaces.py` (`cli_surfaces`).

---

## Remote Bedroom Node and the #1194 Scenarios (Issue #1194 e)

**Occupant outcome.** The hallway thermostat only sees the hallway. On a cold night the person asleep in the bedroom can be under their sleep temperature while the furnace never starts; Climate Advisor's remote-sensor outer loop (docs/08 "Remote-sensor outer loop on the thermostat setpoint") nudges the setpoint so it does. This section is how the harness proves that, in simulation, before the owner trusts it (cool mode ships simulation-tested only).

### The plant: a second thermal node (`tools/sim_harness/world.py`)

Physical plant only, no Climate Advisor logic. Optional and OFF by default: `HouseParams.bedroom` is `None` (omitted from `to_dict()` exactly like `off_offset_f`, #1234), the day generator never draws one, and `tests/test_remote_sim_1194e.py` pins the sha256 of seeds 0-59 x 3 profiles (`7d35f57c...`, taken on 0.8.44 before the node existed), so every generated plan, serialised house and replay file is byte-identical.

| Piece | Model | Default (owner's home, POC v2) |
|---|---|---|
| Coupling | `dB/dt = k_hall (hallway - B) + k_env (outdoor - B)`; steady gap below the hallway `k_env/(k_hall+k_env) * (hallway - outdoor)`; lag `1/(k_hall+k_env)` | `k_hall` 1.7/h, `k_env` 0.28/h (about 3 F below the hallway on a 45 F night, 30 min lag) |
| Plume | While the hallway equipment runs, and for `fan_overrun_min` after it stops, the register puts a plume on the SENSOR (never the room): dead time, then first-order rise to `plume_f` (heat +, cool -), decay once the air stops | `plume_f` 7 F, dead 4 min, rise tau 8 min, decay tau 11 min, overrun 2 min (fit to 12 measured cycles, max error 0.82 F, under the sensor quantum) |
| Sensor | Reports on change in `sensor_step_c` steps; `reported_f` / `last_reported_min` / `age_min`; `silent=True` freezes the last report | 0.1 C (0.18 F) |
| Thermostat | Unchanged `ToyHouse.decide_action` on the HALLWAY reading with the asymmetric deadband (`swing_f` start, `off_offset_f` stop, #1234) | starts at setpoint - 1.5, stops at setpoint - 0.5 |

`ToyHouse(params, indoor_f, bedroom_f=None)` owns the node when `params.bedroom` is set and advances it after the hallway in `step()`.

### Harness hooks added with the node

| Hook | Where | Purpose |
|---|---|---|
| `remote_sensor_report` event (`entity`, `value_f`, optional `unit`) | `run_production._dispatch_event` | The remote sensor REPORTS: `states.async_set` (the real `_on_sleep_indoor_temp_changed` listener runs) stamped with `last_reported`/`last_updated` = the virtual clock. `external_entity_state_change` cannot stand in (it never stamps them, and an unstamped state counts as fresh forever). Silence is the absence of the event. |
| `thermostat_echo_delay_s` config key | `FakeHass.set_echo_delay`, `run_production` | A cloud thermostat reports a `set_temperature` that many seconds later and without Climate Advisor's Context (#1232). The call is logged at once; only the state and its listener event are delayed. Absent = the instant context-carrying echo every other scenario sees. |
| `thermostat_reports_hvac_action` config key (default `true`) | `FakeHass.services.seed_hvac_action`, `run_production` | Issue #1537: the thermostat reports `hvac_action` from its own loop from the first moment. `false` models a thermostat with no such attribute (the #1416/#1417 no-`hvac_action` goldens). |
| `thermostat_action: "scripted"` config key | `FakeHass.set_action_deadband(entity, None)`, `run_production` | Issue #1310: keep the scenario's injected `hvac_action` through CA's mode changes (a world whose equipment never starts, e.g. the cap scenarios). Absent = the thermostat derives its own action after CA's mode changes (see State-Feedback Loop). |
| `commanded_setpoint_at` assertion (`value`) | `outcomes.check_assertion` | The last real `climate.set_temperature` at or before `at` equals `value`. |
| `setpoint_writes` assertion (`values`, optional `since`) | `outcomes.check_assertion` | The exact ordered list of values written in `(since, at]`; `[]` asserts NO write. |
| `hvac_mode_writes` assertion (`values`, optional `since`; Issue #1230 R6) | `outcomes.check_assertion` | The mode analogue: the exact ordered list of modes the engine sent with `climate.set_hvac_mode` in `(since, at]`; `[]` asserts NO mode write. |
| `tools/sim_harness/remote_night.py` | new module | A closed-loop night through the real coordinator (`run_night(NightSpec)`): the plant answers the thermostat, the bedroom reports, the thermostat cycles; returns writes, furnace runs, starts/hour, shortest off time, room minimum and degree-minutes short. `python -m tools.sim_harness.remote_night 0.59 1.0 1.5` prints the night-comparison rows. |

**Rule-8 justification for `hvac_mode_writes` (Issue #1230 R6).** Purely ADDITIVE (a new `expect` string no existing scenario used before the R6 goldens). The previous semantics were not wrong, they could not say this: `hvac_mode_never_commanded` forbids one mode and `setpoint_writes` ignores mode calls, so a guard re-sending `cool` every two hours (cool -> cool, visible only as a cloud write and an Activity row) passed every assertion. It cannot let a real failure pass silently: it is an exact comparison against the `set_hvac_mode` calls actually issued (a missing, extra or wrong mode write fails; empty `values` asserts none) and it is not evaluated unless a scenario names it. Unit test: `tests/test_control_feed_r6_x2_x5.py`.

**Rule-8 justification (`outcomes.py`).** The two assertion types are purely ADDITIVE: new `expect` strings no existing scenario uses, so no existing result can change. The previous semantics were not wrong; they could not say this at all (`expect_temp` reads the comfort TARGET a decision recorded, `field_equals` reads one end-of-run snapshot, `hvac_mode_never_commanded` scans modes only). They cannot let a real failure pass silently: both are exact comparisons against the service calls the engine really issued, a missing, extra or wrong write fails, and an empty `values` asserts no write.

### Tier A scenarios (`tools/simulations/pending/remote_offset_1194e_*.json`)

All 17 run the real `ClimateAdvisorCoordinator` + `AutomationEngine` (`use_coordinator: true`, so the sleep-sensor listener and the 1-minute timer are real), so every assertion is `track: "integration"` and is EVALUATED (never skipped); `simulator_support` is left at its default (true) because the production harness is the only engine. `tests/test_production_harness.py::test_remote_offset_1194e_pending_scenario_passes_production_engine` runs them on every commit. They stay in `pending/` until the owner approves the scenario cards (`/home/dev/ca-group-briefs/1194e-scenario-cards.md`); goldens are locked.

| Scenario | Proves | Fails when (scratch-copy mutation) |
|---|---|---|
| `heat_night_cold_bedroom_raise_hold_step_down` | raise one step, furnace starts, HOLD (no second step), step down at target + swing | offset deleted; hold deleted; step-down deleted |
| `cool_night_warm_bedroom_lowers_setpoint_and_steps_back` | the mirror image | same three |
| `heat_cap_plus_4_when_thermostat_never_starts`, `cool_cap_minus_4_...` | the +4 / -4 F cap | offset deleted; cap removed |
| `heat_cap_opposite_band_edge_ceiling`, `cool_cap_opposite_band_edge_floor` | the opposite band edge beats the +4 cap | offset deleted; edge cap ignored |
| `stale_remote_over_120_min_falls_back_to_plain_setpoint` | plain setpoint by the 1-minute timer 120 min after the last report; restart from zero on the next report | offset deleted; freshness ignored; context not forgotten |
| `window_open_inside_debounce_freezes_the_offset` | FREEZE: no write at all | offset deleted; FREEZE treated as CLOSED |
| `window_open_past_debounce_pauses_then_exits_cleanly` | pause, then plain target + ONE step, never the stranded cap | offset deleted; whole gate deleted |
| `manual_override_no_step_occupant_value_kept` | no CA write over the occupant's value | offset deleted; override terms removed; whole gate deleted |
| `away_closes_the_gate_then_returns_plain_target_plus_one_step` | away setback has no offset | offset deleted; away term removed; whole gate deleted |
| `whole_house_fan_ownership_closes_the_gate` | WHF-owned house: no write; clean restore | offset deleted; whole gate deleted |
| `natural_ventilation_closes_the_gate_cool` | nat-vent owns the house (cool) | offset deleted only (see limits) |
| `sleep_window_entry_starts_at_zero_and_exit_leaves_no_offset` | entry from zero; wake band plain | offset deleted; context not forgotten |
| `ca_own_late_setpoint_echo_is_not_an_override` | #1232: a 90 s late, context-free echo is not the occupant | offset deleted; by-value echo recognition deleted |
| `closed_loop_night_cold_bedroom_swing_1_5`, `..._swing_0_59` | a whole night recorded from the two-node house; the exact setpoint sequence | offset deleted; step-down deleted; (0.59) context not forgotten |

**Limits, stated plainly.** (1) The whole-house-fan term alone and the nat-vent term alone cannot be isolated in Tier A: with a monitored window open the door/window pause closes the same gate, and a whole-house fan with no door/window sensor is unsupported (#1200; `test_no_scenario_runs_an_unsupported_sensorless_whole_house_fan` forbids it). Removing both terms leaves all 17 scenarios green; those terms are proven in `tests/test_remote_offset_gates_1194b.py`. (2) During natural ventilation the thermostat is Off and the loop never changes the mode, so that scenario cannot show the nat-vent gate term (it shows the offset and that nat-vent owns the house). (3) The two closed-loop night scenarios pin the loop's CURRENT sequence on a synthetic night; they are regression pins, not design requirements. (4) The sleep-window entry/exit scenario asserts the 0.8.44 behaviour (offset zero at entry, plain wake band); increment (c) refines the same boundary and must keep it true.

### Tier B (real Home Assistant)

HeadlessTarry (a proposed Docker harness) is not in this repository; Tier B is the real-HA tier, which exercises what Tier A's virtual clock and instant thermostat cannot (`python scripts/test.py ha`, dev thermostat simulator, CI). `tests_ha/test_remote_offset_sim_1194e.py` (both fan modes): the 1-minute timer steps the loop with no new bedroom report; a silent sensor is dropped by the timer alone after 120 minutes; the loop's own steps echoing back 45 s and 110 s late (no cause for an override) while an occupant's real change in flight is still caught; a restart mid-night (nothing written inside the startup-coalescing window, never past the cap, no false override). Mutation check (CA_COMPONENT_ROOT overlay): offset deleted fails 4 tests x 2 fans, freshness ignored fails the silent-sensor test, by-value echo deleted fails the late-echo test, the floor timer deleted fails 4. Deleting the startup-coalescing silence does NOT fail the restart test: the loop has no target after a restart, so it is inert by construction (docs/08 'Startup coalescing').

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

**`@callback` is the identity (Issue #1278).** `homeassistant.core.callback` is replaced by `_callback`, which marks the function and returns it unchanged, as real HA does. A MagicMock decorator replaced each decorated method with one shared mock. That had two effects:
- `ClimateAdvisorCoordinator._async_whf_safety_tick`, `_async_thermal_sample_tick` and `_on_post_grace_fan_check` never ran in simulation.
- Every call was recorded forever, about 25 MB per generated day, which got the nightly runner killed.

`tests/test_ha_stub_callback_1278.py` guards the identity, checks that no production class attribute is a mock, and checks that generated days do not pile up recorded mock calls. `--generate` prints `peak_rss=…MB` after each master seed.

**`voluptuous` handling:** If the real `voluptuous` package is importable it is used; otherwise a MagicMock is injected. This prevents schema-validation code from failing at import time in environments without `voluptuous`.

**Code reference:** `tools/sim_harness/ha_stubs.py` (`install_ha_stubs`)

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
| `climate.set_temperature` | `attributes["temperature"]`, `["target_temp_low"]`, `["target_temp_high"]` updated as present; `state = data["hvac_mode"]` **only for the default (obedient) entity** — see the `sensi_like` quirk below |
| `climate.set_fan_mode` | `attributes["fan_mode"] = data["fan_mode"]` |
| `fan.turn_on` / `switch.turn_on` | `state = "on"` |
| `fan.turn_off` / `switch.turn_off` | `state = "off"` |

**The thermostat's own `hvac_action` (Issue #1310).** CA's `set_hvac_mode` / `set_temperature` used to leave `hvac_action` as the scenario last injected it, so after CA switched fan_only -> cool the thermostat kept reporting `"fan"` with the house below the setpoint, and production's mode fallback counted the idle time as AC runtime (scenario 912: 103.5 min at 70 F under a 72 F cool setpoint). Now `_FakeServices._derive_hvac_action()` answers like a real thermostat, using `world.thermostat_running` (the rule `ToyHouse` and the real-HA `ca_dev_thermostat_sim` use: start at setpoint -/+ deadband, stop the deadband past the far side; deadband `THERMOSTAT_ACTION_DEADBAND_F` 1.5 F, the real-HA sim default; 1.5/1.8 C for a `temp_unit: celsius` scenario):

| CA command | Thermostat reports |
|---|---|
| mode change into heat/cool | `heating`/`cooling` only if the reading is the deadband past the setpoint, else `idle` (a mode change starts fresh) |
| setpoint change, same mode, action last derived by the thermostat | the same loop with hysteresis (a running session continues until the far side) |
| mode change into off / fan_only while `heating`/`cooling` | `off` / `fan` (the session stops) |
| setpoint change, same mode, action injected by the scenario | unchanged -- scripted equipment (e.g. a thermostat that never starts) stays scripted until CA's next mode change |
| anything else (heat_cool, auto, off/fan_only while idle) | unchanged (heat_cool/auto are not modelled: the action stays what it was, `idle` from the seed) |
| new indoor reading (`temp_update`), action last derived by the thermostat (Issue #1316) | the same loop with hysteresis: an idle AC/furnace starts once the house drifts the deadband past the setpoint and stops past the far side; a scripted, attribute-less or disabled thermostat is untouched |
| scenario-injected mode change without an `hvac_action` (the occupant at the dial), action last derived by the thermostat (Issue #1318) | off -> `off`, fan_only -> `fan`, heat/cool -> the same loop (hysteresis in the same mode, fresh start on a mode change); an injected `hvac_action` always wins, scripted/attribute-less/disabled thermostats are untouched |

**The harness thermostat reports `hvac_action` by default (Issue #1537, owner decision 2026-11-13: "Sims report hvac_action").** A real thermostat reports what its equipment is doing, and the household-promise checks no longer infer it from mode and room temperature (`invariant_watchdog.equipment()` returns unknown without it), so the harness thermostat reports it from the first moment: `FakeHass.services.seed_hvac_action()` seeds it at session start from the thermostat's OWN loop (off -> `off`, fan_only -> `fan`, heat/cool -> `world.thermostat_running` over the reading and the setpoint, `idle` until both exist; heat_cool/auto, which the model does not simulate, -> `idle`), and marks it as the thermostat's own, so every later reading and command keeps deriving it per the table above. It is never derived from Climate Advisor's decisions (that would make a golden echo production back to itself). A scenario-injected `hvac_action` still wins and is always scripted (`mark_action_scripted`), even when it equals the value the thermostat derived (before, an injected `idle` equal to a derived `idle` read as the thermostat's own and was re-derived on the next reading, which made the #1194 e "never starts" worlds start once every thermostat was seeded). Limit: the scripted room reading is not coupled to the equipment, so with the 1.5 F symmetric deadband a furnace that started stays `heating` until the script takes the room 1.5 F past the setpoint. `"thermostat_reports_hvac_action": false` models a thermostat with no such attribute (nothing seeded, nothing derived; production records no heating/cooling run at all, owner decision 2026-11-13 removed the #93 mode fallback). Opted out, because their fix IS the no-`hvac_action` path: `tou_home_cool_bank_ends_no_hvac_action_1416`, `tou_home_heat_bank_ends_no_hvac_action_1416` (Issue #1416: the bank-write 'ran' record) and `hot_night_same_mode_pre_cool_wakeup_keeps_cool_1417` (Issue #1417: the pre-cool 'ran' record); with a reported action the settling guard is satisfied by the action and these would pass on the old code. Goldens re-derived when this landed (decisions changed only where production reads the reported action): `cool_all_day_heat_all_day` (bedtime keeps heat, setback 64 F, instead of switching to cool 72 F while the furnace was still running), `override_adopted_on_matching_decision` (the 13:00 heat->cool margin switch waits for the 13:01 classification: the furnace stopped at 13:00) and `issue_637_paused_during_grace_open_fallthrough` (no heat armed at 13:05 one reading after the AC stopped on a 90 F day); other event-log changes were payload only (`incident_detected.thermostat_action`, `comfort_switch_held.opposite_ran_min_ago`). `"thermostat_action": "scripted"` (or `FakeHass.set_action_deadband(entity, None)`) keeps the scenario's injected action through CA's mode changes too. The generator (`generator.own_thermostat`) and `remote_night.py` turn it off: their `ToyHouse` is the thermostat and reports through `thermostat_state_changed`. Tests: `tests/test_fake_thermostat_action_1310.py`, `tests/test_sim_reports_hvac_action_1537.py`.

**Next Action card text (Issue #1276, opt-in).** A scenario that sets `"capture_next_action": true` gets `engine_state._next_action = {"text", "advises_opening_windows"}`: `FakeHassSession._capture_next_action()` (and the real-HA adapter's `RealHASession.finish()`, through the shared `scenario_model.capture_next_action()`, so the golden is judged on both tiers) calls the real `coordinator._compute_next_action()` once at the end of the run, inside the patched clock, with the same arguments `_async_update_data` passes (outdoor = the last `temp_update` `outdoor_f`, since Tier A runs no weather entity). Assert it with `field_equals`, e.g. `engine_state._next_action.advises_opening_windows`. Absent unless the flag is set, so no other golden's snapshot changes; `outcomes.py` is untouched. Limit: one end-of-run reading, not the card at each cycle. **R3b extension (Issue #1230):** the same capture now also holds `source` (`timeline`/`legacy`), `window_step`, `next_automation` (`action`, `time`), `timeline` (`you_steps`, `ca_kinds`, `ca_steps`, `window_confidence`, `held_ca_steps`, `sensor_swap`, `has_resume_step`, and (redesign 2026-11-06) `you_step_states` and `resume_step` = `{at, text}`), `window_step_state` (`{status, hold, text}` of the card step: `held` with `held.direction_veto.*` when the live direction veto applies), `briefing_windows_row` and `agrees`; existing keys are unchanged and `outcomes.py` is untouched (all read through `field_equals`). The occupancy event driver now also sets the coordinator's `_occupancy_mode` and runs the timeline, as production's occupancy toggle does (semantic justification in `run_production._sync_coordinator_occupancy`). Used by `issue_1276_evening_floor_heat_on_no_open_windows_advice` (+ `__whf`), which fails when the #1276 direction rule is reverted (mutation-checked 2026-11-06). **R6 cards extension (Issue #1230, X7):** the capture also holds `tou_window` = `{start, end, target, mode}` (`HH:MM`, from `coordinator._tou_precondition_window_tuple()`; `null` without a resolved schedule), additive, so a scenario pins the exact bank start the surfaces quote (`engine_state._next_action.tou_window.start`) against the first thermostat write (`setpoint_writes`). Pair it with `timeline.ca_steps` (e.g. `"14:24 precool_start"`) and `next_automation`; the 5-minute check (`coordinator._async_tou_bank_tick`) is a Tier A event the virtual clock fires (interval phase = the session start, so the first write lands on the first tick at or after the start, e.g. 14:24:59 for a 14:24 window). Used by the `r6b_tou_bank_lead_*` and `r6b_primary_without_a_fit_uses_defaults_tou_lead*` cards.

**Finalized day records (Issue #1310).** With a coordinator, `FakeHassSession` passively copies every `DailyRecord` production finalizes (`_async_end_of_day` -> `learning.record_day`, called through unchanged) into `engine_state._finalized_days.<date>` (absent when no day was finalized), so `field_equals` can assert a day's runtime split, e.g. `engine_state._finalized_days.2026-06-10.thermostat_fan_only_runtime_minutes` (scenario 912). The learning store itself cannot be read back for this: it trims records older than 90 days by the real wall-clock date.

**Opt-in `sensi_like` thermostat quirk (Issue #1048).** Real Sensi (and HA-core ecobee) thermostats ignore the `hvac_mode` kwarg of `climate.set_temperature`, and Sensi rejects `set_temperature` while the thermostat is Off. A scenario enables this with `"thermostat_quirk": "sensi_like"` in its `config` (unit tests call `fake_hass.set_entity_quirk(entity_id, QUIRK_SENSI_LIKE)`). For a quirked entity: `set_temperature` never changes the state string from `hvac_mode`; `set_temperature` while state is `"off"` is rejected — a non-blocking call swallows the error (as real HA does: logged, not raised) and leaves state unchanged, a `blocking=True` call raises `FakeHomeAssistantError`; `set_hvac_mode` works normally. Rejected calls still appear in `action_log`. Default entities are unchanged (they honour the kwarg). The engine-state snapshot exposes the thermostat's real mode as `engine_state._thermostat_hvac_mode` for `field_equals` assertions (`last_hvac_mode` reads `action_log` and so cannot see a silently ignored mode).

Without this, production code that reads the thermostat back (e.g. `handle_occupancy_away` checking the actual HVAC mode before selecting setback_heat vs setback_cool) would see the stale initial state and diverge for the wrong reason — reproducing neither the real HA behavior nor the legacy `SimState` mutation pattern.

**Code reference:** `tools/sim_harness/fake_hass.py` (`_FakeServices`), `#L118` (`FakeHass`)

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
| `dt_util.as_local` | `_ha_as_local` — HA-faithful: a naive input is treated as the virtual clock's local zone (`replace(tzinfo=...)`), an aware input is converted (`astimezone`) into it; the result is **always aware** (Issue #1117) |

The virtual clock `start` must be timezone-aware (`FakeScheduler.__init__` raises `ValueError` otherwise); its zone is the harness's "local" zone (UTC by default).

**Issue #1117 — why `as_local` is no longer a pass-through (rule #8 justification).** The old identity stand-in returned a naive input naive. Real HA's `dt_util.as_local` never does that, so the harness created a contract production never sees: the regular coordinator refresh crashed in tests only (`TypeError: can't compare offset-naive and offset-aware datetimes` in `_build_predicted_indoor_future`'s cosine fallback, fed by `_synthesize_hourly_day()`'s naive `as_local` call), before the comfort-minute accumulation, the window-cooling check and the regular-cycle `check_natural_vent_conditions()` re-check. Tests that wanted those paths had to dodge them (e.g. an unavailable weather entity). Could the change let a real failure pass silently? No: for aware input (every pre-existing harness call) the result is identical, and the full suite showed zero deltas; for naive input it now matches HA instead of crashing. The remaining blind spot is unchanged: the harness zone is UTC, so local-day/DST bugs stay invisible. The ~41 process-wide naive `dt_util.now` overrides in individual test modules are a separate, known gap and are not touched here. `tests/test_harness_dt_faithful.py` pins the contract and proves the regular cycle reaches the nat-vent re-check.

**Why `automation` namespace, not `homeassistant.util.dt`?** `automation.py` does `from homeassistant.util import dt as dt_util`, binding the name in its own namespace. Patching the originating module would not affect the already-resolved name in `automation`. The `patch(...)` targets must be on the module that imported the symbol.

**Code reference:** `tools/sim_harness/fake_scheduler.py` (`FakeScheduler`), `#L185` (`installed`)

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
| `nat_vent_hysteresis_f` | `1.0` | mirrors production const (previously 2.0, corrected) |
| `nat_vent_reactivation_lockout_s` | `300` | mirrors production const |

**Why `nat_vent_hysteresis_f = 1.0`?** The production constant `NAT_VENT_HYSTERESIS_F` is 1.0°F. The old harness default was 2.0°F — a hardcoded departure that silently suppressed nat-vent activation for marginal temperature gaps, making scenarios behave unlike any real home. The harness must mirror the exact production constants.

**Code reference:** `tools/sim_harness/build_engine.py` (`build_headless_engine`), `#L46` (`_DEFAULT_CONFIG`)

---

## run_production — Scenario Adapter

**File:** `tools/sim_harness/run_production.py`

`run_production_scenario(scenario)` is the top-level adapter. It:

1. Merges `scenario["config"]` over `_DEFAULT_CONFIG`
2. Determines `start_dt` from the first event time (minus 1 second)
3. Calls `build_headless_engine()` with merged config and initial thermostat mode
4. Creates a `_SensorTracker` and wires it to `engine._sensor_check_callback` (bare-engine mode only). The tracker is wired whatever `door_window_sensors` says, so the engine's WHF safety snapshot always holds exactly one sensor reading (closed until a `sensor_open`). A bare-engine scenario therefore cannot model a zero-sensor house. That is fine because a whole-house fan with zero sensors is unsupported (Issue #1200): every `whole_house_fan` scenario outside `unsupported/` must configure at least one sensor, enforced by `tests/test_production_harness.py::test_no_scenario_runs_an_unsupported_sensorless_whole_house_fan`. In coordinator mode the configured list is real; seed each sensor with a `sensor_close` at the first event so it does not read as unknown.
5. Seeds `thermal_model` and `hourly_forecast` from top-level scenario keys if present, into their owner (the coordinator; a `StaticEngineInputs` for a bare engine; Issue #1443)
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
| `occupancy_away` | `engine.set_occupancy_mode("away")` + `engine.handle_occupancy_away()`. With a coordinator: through `coordinator._run_occupancy_handler("away")` (Issue #1409) |
| `occupancy_home` | `engine.set_occupancy_mode("home")` + `engine.handle_occupancy_home()`. With a coordinator: through `coordinator._run_occupancy_handler("home")` (Issue #1409) |
| `occupancy_vacation` | `engine.set_occupancy_mode("vacation")` + `engine.handle_occupancy_vacation()`. With a coordinator: through `coordinator._run_occupancy_handler("vacation")` (Issue #1409) |
| `occupancy_change` | Dispatches to away/home/vacation by `event["mode"]`; with a coordinator, through `coordinator._run_occupancy_handler(mode)` (Issue #1409) |
| `occupancy_change_with_override` | Sets `engine._manual_override_active = True`, then dispatches as `occupancy_change`, including the coordinator's `_run_occupancy_handler(mode)` entry (production's `_occupancy_bank_write` flag and `occupancy_change()` context) when a coordinator is present; before Issue #1435 it called the engine handler directly. The real-HA adapter does the same |
| `bedtime` | `engine.handle_bedtime()`. With a coordinator: nothing when the coordinator's own daily `sleep_time` timer (default 22:30) is in the event's minute, because the clock already fired it (Issue #1420) |
| `wakeup` | `engine.handle_morning_wakeup()`. With a coordinator: nothing when its own daily `wake_time` timer (default 06:30) is in the event's minute (Issue #1420) |
| `pre_cool` | Indoor inject + `engine.handle_pre_cool(indoor_temp, nat_vent_just_closed)`. With a coordinator: inject only, when the coordinator's own pre-cool trigger (`_async_pre_cool_trigger`) fired in the event's minute (Issue #1420) |
| `economizer_check` | `engine.check_window_cooling_opportunity(...)` |
| `thermostat_state_changed` | Injects thermostat state + `engine.handle_manual_override(...)` if conditions met |
| (coordinator start, `skip_startup_coalesce` false) | `build_headless_coordinator()` leaves `_startup_coalesce_active = True` and arms the same 300 s timer as `simulate_restart` (`arm_startup_coalesce_end()`), so the initial window ends as in production 300 s after the coordinator starts, one second before the first event (Issue #1297). With `skip_startup_coalesce` true the window is closed by flag and no coalesce runs |
| `simulate_restart` (coordinator mode) | `engine.restore_state(engine.get_serializable_state())`, fresh-instance defaults (the door-pause fields, #645, and the un-persisted observe-only WHF notice flags `_whf_observe_notified` / `_whf_observe_rule2_notified`, #1513: a real restart's new engine notifies again, which reopens the P9/P2 notice span the journal reset dropped), `coordinator._startup_coalesce_active = True`, and a 300 s virtual timer that runs the real `coordinator._do_startup_coalesce()` behind production's gate, so the window ends as in production (Issue #1291; docs/08 §11) |
| `startup_coalesce` (coordinator mode) | `coordinator._do_startup_coalesce()` directly, at the scenario's time; once it has run, the start-up/restart timer's gate is false and it does not run again. If that timer already ran it (the event at or after +300 s), the event does nothing: production coalesces once per window (Issue #1297) |
| `remote_sensor_report` (Issue #1194 e, coordinator mode) | `states.async_set(entity, value_f)` stamped with `last_reported` / `last_updated` = the virtual clock: the real sleep-sensor listener and the outer loop's 120-minute freshness rule see a real report |

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

These are seeded into their owner before event dispatch (Issue #1443): the coordinator in coordinator mode, a `StaticEngineInputs` in engine-only mode. Test-infrastructure justification (#1440): production has one forecast and one model, the coordinator's; the engine's former copies were mirrors the coordinator overwrote on every refresh, so seeding only the engine (as the real-HA adapter did) let the coordinator's refresh replace the scenario input. Seeding the owner cannot hide a real failure: it feeds the input where production reads it, and `tests/test_engine_inputs.py` fails if the engine stops reading it live. The legacy simulator had no ODE and ignored these keys; they only affect the production harness path.

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

**Code reference:** `tools/sim_harness/run_production.py` (`run_production_scenario`), `#L325` (`_dispatch_event`)

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
| `occupancy_comfort_restore_held` (Issue #1245) | — | none: registered in `UNMAPPED_PRODUCTION_EVENTS` (the absence of a write, same class as `occupancy_restore_deferred`; cannot shadow the outcome in force) |
| `morning_wakeup` | — | `comfort_restored` |
| `morning_wakeup_skipped` | — | `morning_wakeup_skipped` |
| `grace_expired` | `re_paused=True` | `paused` |
| `grace_expired` | `re_paused=False` | `resumed` |
| `override_detected` | — | `override_detected` |
| `override_confirmed` | — | `override_confirmed` |
| `override_self_resolved` | — | `override_self_resolved` |
| `override_cleared` | — | `override_cleared` |

**`target_temp` enrichment:** Production emits a decision event (`classification_applied`, `bedtime_setback`, etc.) and the setpoint via a separate `climate.set_temperature` action at the same virtual-clock instant. `_temps_by_timestamp()` builds a map from naive-ISO timestamp to setpoint, and `production_decisions()` fills `target_temp` on each decision from this map.

**Unmapped production-only events** (`UNMAPPED_PRODUCTION_EVENTS`): `warm_day_state_confirmed`, `nat_vent_away_ceiling_exit`, `nat_vent_ceiling_escalation`, `nat_vent_forecast_skip`, `nat_vent_floor_imminent_skip`, `grace_started`, `incident_detected`, and the Issue #1049/#1050 owner-alert rows `hvac_mode_not_applied`, `hvac_mode_applied_recovered`, `hvac_command_recovered`, `hvac_output_stall`, `hvac_output_stall_recovered`, `occupancy_restore_deferred`. These are silently skipped by `_map_event_to_outcome()` (each has a one-line justification at its registration in `outcomes.py`: left mapped it would hit the `unknown:<type>` catch-all and shadow the prior real outcome under last-decision-wins; whether the house ended in band or the mode was applied is asserted through live engine state or `event_emitted`, not these rows). The fake thermostat never sets `hvac_action` on a thermostat that reports none (Issue #1310 derives it only for one that does; since Issue #1537 every golden reports one unless it sets `thermostat_reports_hvac_action: false`), so `hvac_output_stall` cannot fire in a golden that opts out. **Superseded in part (Issue #1128):** a `thermostat_state_changed` event may carry an optional `hvac_action` (Issue #790), and the generator's toy house (`world.py`) reports one on every change of equipment state, so generated days do exercise it; the unmapped-event handling is unchanged. (`nat_vent_predicted_floor_exit` — the PROACTIVE_FLOOR event — was in this list until Issue #959, which deleted the mechanism that emitted it; do not re-add it unless the event type returns.)

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
| `comfort_band_armed` | At least one `comfort_band_applied` event at or before `at` **whose `outcome` is `sent`** (or that has no `outcome` field -- legacy pre-#1049 events). Changed in Issue #1049 | A band that was blocked (WHF owns the thermostat), dry-run, failed or superseded is not an armed band. Rule-8 justification: the old form counted any `comfort_band_applied`, which was emitted even when nothing reached the thermostat -- exactly how a week of "band applied" with a cold house went unnoticed; the new form is strictly stricter |
| `event_emitted` | Payload: `{"event": "<type>", "payload_subset": {...}, "at": ...}`. An event of that type exists in `result.event_log` at or before `at` and its payload contains every key/value of `payload_subset` (omitted = any payload). New in Issue #1049 | Asserts observational events that are deliberately unmapped to decisions (`grace_cancelled`, `occupancy_restore_deferred`, `hvac_mode_not_applied`, ...). Purely additive: it cannot pass without the event having been emitted, and no existing scenario uses it |

**Code reference:** `tools/sim_harness/outcomes.py` (`production_decisions`), `#L346` (`check_assertion`)

---

## tools/simulate.py — CLI Shell

`tools/simulate.py` (726 lines) is a thin shell around `run_production.py`. It owns:
- CLI argument parsing (`--pending`, `-s NAME`, `--list`, `--cases`, `-v`, `--report`, `--check-integrity`, `--sign`)
- `run_scenario_production(scenario_file, state)` — loads JSON, calls `run_production_scenario()`, formats assertions
- Tier separation: `track: "integration"` → `skipped: True`; `simulator_support: false` → evaluated normally
- Output formatting (`print_result`, `_status_label`)
- MANIFEST integrity (`check_integrity`, `sign_scenario`)
- Markdown report generation (`write_report`)

There is no `--engine` flag. The only engine is production.

**Code reference:** `tools/simulate.py` (`run_scenario_production`)

---

## tests/test_production_harness.py — Tier A Enforcement

`test_golden_passes_production_engine` is parametrized over every `.json` in `tools/simulations/golden/` (excluding `MANIFEST.json`). For each scenario:
- Calls `run_scenario_production(scenario_path, state="golden")`
- Asserts `result["callback_errors"]` is empty (unexpected engine errors = untrustworthy run)
- Asserts `result["passed"] is not False` (`True` = assertions passed; `None` = only integration/deferred assertions, acceptable)

`test_all_goldens_discovered` guards against an empty parametrization silently passing: asserts `len(golden_files) >= 20`.

**Code reference:** `tests/test_production_harness.py` (`test_golden_passes_production_engine`)

---

## State Transitions — Tier A Scenario Lifecycle

| From | Trigger | To | Side effects |
|---|---|---|---|
| `pending/` | Author runs `--pending -v`, reviews output | `golden/` or `pending-fix/` | `--sign <name>` updates MANIFEST.json; `mv` to golden/ |
| `golden/` | `test_production_harness.py` parametrized test | (stays golden) or FAIL | FAIL = regression; code or scenario needs investigation |
| `golden/` | User authorizes modification | modified `golden/` | `--sign <name>` required; `--check-integrity` confirms |
| `pending/` | Behavior confirmed out of scope | `unsupported/` | Never delete — documents deliberate scope decisions |
| `golden/` | Owner approves retiring it (scope decision, e.g. an unsupported configuration) | `unsupported/` | MANIFEST entry stays (MANIFEST changes only via `--sign`); `--check-integrity` prints it as `REMOVED ... (now in unsupported/); not validated` on every run, so it never drops out silently |

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

- [`install_ha_stubs`](../tools/sim_harness/ha_stubs.py#L632) — idempotent HA stub installer
- [`FakeHass`](../tools/sim_harness/fake_hass.py#L685) — minimal HA stand-in
- [`_FakeServices._apply_state_feedback`](../tools/sim_harness/fake_hass.py#L384) — state-feedback loop
- [`FakeScheduler`](../tools/sim_harness/fake_scheduler.py#L69) — virtual clock + priority-queue timer driver
- [`FakeScheduler.installed`](../tools/sim_harness/fake_scheduler.py#L301) — patching context manager
- [`build_headless_engine`](../tools/sim_harness/build_engine.py#L79) — engine assembly
- [`_DEFAULT_CONFIG`](../tools/sim_harness/build_engine.py#L46) — harness default config
- [`run_production_scenario`](../tools/sim_harness/run_production.py#L615) — top-level scenario adapter
- [`_dispatch_event`](../tools/sim_harness/run_production.py#L662) — event-type → engine method dispatch
- [`_handle_temp_update`](../tools/sim_harness/run_production.py#L1187) — temp_update + predicted_indoor re-classification
- [`production_decisions`](../tools/sim_harness/outcomes.py#L306) — event_log → legacy outcome vocab
- [`check_assertion`](../tools/sim_harness/outcomes.py#L346) — custom assertion types
- [`UNMAPPED_PRODUCTION_EVENTS`](../tools/sim_harness/outcomes.py#L123) — production-only events with no legacy outcome
- [`run_scenario_production`](../tools/simulate.py#L71) — CLI shell entry point
- [`test_golden_passes_production_engine`](../tests/test_production_harness.py#L32) — Tier A parametrized golden test
