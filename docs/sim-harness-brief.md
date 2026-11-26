<!-- Nav: ← [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md) | → [sim-harness-spec.md](sim-harness-spec.md) | ↔ [simulation-feedback-loop.md](simulation-feedback-loop.md) -->

# Production Simulation Harness — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What is the single-engine harness and why does it replace the old simulator? | Issue #236 eliminated the standalone `ClimateSimulator` (~1,500 lines that re-implemented `automation.py`). There is now one engine: `tools/sim_harness/` drives the real production `AutomationEngine` headless. Goldens pass or fail against production code itself. | [Scope](#scope) |
| What are Tier A and Tier B, and what falls into each? | Tier A = fast unit harness; runs every commit; exercises every `automation.py` decision path. Tier B = the real-Home-Assistant tier (`tests_ha/`, `python scripts/test.py ha`, CI job `ha-real`); runs the real coordinator inside a real HA core and covers state-listener timing, restart recovery and persistence, timers, unit handling and load/unload cleanup. (HeadlessTarry, a Docker harness for real thermostat hardware, is a proposal and is not in this repository.) | [Tier A and Tier B](#tier-a-and-tier-b) |
| What does `tools/simulate.py` do now? | It is a thin CLI/report/MANIFEST shell (726 lines). All engine work is delegated to `tools/sim_harness/run_production.py::run_production_scenario()`. There is no `--engine` flag and no `ClimateSimulator`. | [Scope](#scope) |
| Where do assertions land — event_log, action_log, or engine state? | All three. `event_log` carries decisions (`ceiling_guard_fired`, `nat_vent_comfort_floor_exit`, etc.). `action_log` carries service calls (`climate.set_temperature`). `engine_state` is a snapshot of live engine flags after all events process. | [sim-harness-spec.md — Assertion Surface](sim-harness-spec.md#assertion-surface) |
| What does `simulator_support: false` mean now? | The real engine CAN evaluate these once the scenario supplies the needed inputs (e.g. `predicted_indoor`). Not skipped in Tier A. Previously marked phantom because the legacy simulator had no ODE. | [Scope](#scope) |
| Where is the full Tier 3 spec covering each harness module? | Covers `fake_hass.py`, `fake_scheduler.py`, `build_engine.py`, `run_production.py`, `outcomes.py`, and `ha_stubs.py` with module-level Anchors. | [sim-harness-spec.md](sim-harness-spec.md) |
| What is the Session seam? | `session.py` defines one runner protocol; `FakeHassSession` (fast default) and `RealHASession` (real HA, `tests_ha/`) implement it; generated days, promises and surface checks run on top. | [sim-harness-spec.md](sim-harness-spec.md#session-seam-generated-days-and-promises-issues-1127-1128) |
| What does the perf probe record? | `tools/sim_harness/perf_probe.py` (run through `simulate.py --perf-report [PATH]`) records thread CPU per executor callable, per loop callback and per scheduler task, wall time per worker job, and optional line counts (`--perf-lines`). GC is excluded. Report only: it never changes results or exit status. | [Perf probe](#perf-probe-report-only-issue-1728) |

---

## Scope

**Owns:**
- Headless execution of the production `AutomationEngine` via `FakeHass` + `FakeScheduler`
- Scenario JSON → `ProductionRunResult` pipeline (`run_production.py`)
- Event-log → legacy-outcome vocabulary bridge (`outcomes.py`)
- Custom assertion types beyond the outcome vocabulary (`check_assertion` in `outcomes.py`)
- Idempotent HA stub installer shared by tests and harness runtime (`ha_stubs.py`)
- CLI, MANIFEST signing, and report generation shell (`tools/simulate.py`)
- Tier A enforcement: `tests/test_production_harness.py` parametrizes every golden through production

**Explicitly does NOT own:**
- `automation.py` production logic (owned by `automation.py`)
- Coordinator state-listener timing, HA restart recovery (Tier B — the real-HA tier, `tests_ha/`)
- Scenario authoring (scenario JSON files live in `tools/simulations/`)
- The simulation feedback loop (owned by `tools/simulation_loop.py`; see [simulation-feedback-loop.md](simulation-feedback-loop.md))

---

## Responsibilities

- Intercept every `hass.services.async_call()` the engine makes and append a structured record to `action_log` (FakeHass)
- Apply a state-feedback loop so engine read-backs see the commanded state, exactly as real HA would
- Drive timers with a priority-queue virtual clock; fire callbacks in chronological order, drain `async_create_task` coroutines after each callback (FakeScheduler)
- Patch `automation.py`'s `async_call_later`, `callback`, and `dt_util.*` symbols so the engine's scheduling calls land in the virtual clock, not the OS
- Construct the `AutomationEngine` with merged config defaults + scenario overrides, wire coordinator-facing callbacks (FakeHass, event_log)
- Dispatch each scenario event to the correct production engine method in chronological order; advance the virtual clock before each event
- Support ODE ceiling guard testing: a `temp_update` event may carry `predicted_indoor` to re-invoke `apply_classification()`, modelling the coordinator's 30-min re-classification cycle
- Support thermal model injection: top-level `thermal_model` and `hourly_forecast` in scenario JSON are injected directly onto the engine instance
- Convert `event_log` tuples into the legacy outcome vocabulary for `expect:` assertion comparison
- Implement custom assertion types (`nat_vent_not_active`, `nat_vent_fan_preserved`, `dual_setback_applied`, `ceiling_guard_fires_cool`, `ceiling_guard_dormant*`) that read real engine state and event_log
- Skip `track: "integration"` assertions in Tier A (deferred to Tier B); evaluate `simulator_support: false` assertions normally

---

## Interfaces

The public entry points are `run_production_scenario()` (run a full scenario), `build_headless_engine()` (construct engine + stubs), `production_decisions()` (convert event_log to legacy outcomes), `check_assertion()` (custom assertion types), and `install_ha_stubs()` (idempotent HA stub installer). Full interface signatures and caller reference → [sim-harness-spec.md — Code Reference](sim-harness-spec.md#code-reference).

---

## Data Structures

`ProductionRunResult` carries `event_log` (decisions with timestamps), `action_log` (service calls), `engine_state` (final flag snapshot), and `callback_errors` (exceptions during execution). `ProductionDecision` maps a production event type to the legacy outcome vocabulary with optional target temperature. Full field definitions → [sim-harness-spec.md — Assertion Surface](sim-harness-spec.md#assertion-surface).

---

## Invariants

Key guarantees: the virtual clock never moves backwards; coroutines are never dropped; `install_ha_stubs()` is idempotent; `track: "integration"` assertions are skipped in Tier A; state feedback reflects commands before engine read-backs; `simulator_support: false` is now evaluated (not skipped). Full invariant list → [sim-harness-spec.md — Invariants](sim-harness-spec.md#invariants).

---

## Tier A and Tier B

**Tier A — production harness (this package):**
Runs on every commit via `tests/test_production_harness.py`. Drives the real `AutomationEngine` headless. Covers every decision path in `automation.py`: natural vent, grace periods, override confirmation, occupancy setback, ceiling guard ODE, fan cycles. All `track: "logic"` assertions run here.

**Tier B — real Home Assistant (`tests_ha/`, CI-gated):**
Runs the real coordinator and config entry inside a real HA core (pytest-homeassistant-custom-component) with a small simulated home: the dev thermostat simulator (`ca_dev_thermostat_sim`), a real weather entity, contact sensors, a whole-house fan and a frozen clock. Built by `tests_ha/house/` (`make_house`); goldens replay through `tests_ha/realha_session.py` (`RealHASession`) and are judged by the same `outcomes.py`. Exercises:
- `_async_thermostat_changed` and the other state listeners (HA state change timing)
- Coordinator restart recovery and persistence (`house.restart()`; HA boot ordering is not modeled)
- Timers, the periodic `_async_update_data` cycle, unload and reload cleanup (HA's leak checker). A scenario's `bedtime`, `wakeup` or `pre_cool` event in the minute the coordinator's own timer fires only moves the clock; the timer makes the write, as in production (Issue #1420; the same holds in Tier A with `use_coordinator`)
- Occupancy events (`occupancy_*`, including `occupancy_change_with_override`) run the handler through the coordinator's `_run_occupancy_handler`, production's only occupancy-change entry, so a change inside an open time-of-use bank window writes the new bank once (Issues #1409, #1435; the same holds in Tier A with `use_coordinator`)
- Unit system, service validation, snapshots of the briefing and cards

Goldens that fail on real HA are recorded in `tools/sim_harness/realha_ledger.json` (a two-way ratchet; see the spec). CI runs it as the `ha-real` job in `validate.yml` (8-minute cap) and the nightly `ha-lifecycle` job; `ha-compare-weekly.yml` diffs every golden between FakeHass and real HA.

**HeadlessTarry (proposal, not built):** a Docker harness that would drive real thermostat hardware latency. Nothing in this repository implements it; the plan in `plans/test-automation/04-retirement-waves.md` treats it as frozen.

Assertions tagged `track: "integration"` are skipped in Tier A without causing a failure; they need the coordinator's listener layer, which Tier B provides.

---

## Perf probe (report only, Issue #1728)

`tools/sim_harness/perf_probe.py` measures how much CPU the code under simulation takes, so a slow job shows up before it reaches a home. Run it with `python tools/simulate.py --generate N --seeds S --perf-report [PATH]` (default `perf_report.json`). The nightly `generated-days` job does this and uploads the JSON as the `perf-report` artifact.

**What it records** (rows sorted by max CPU):
- `executor`: thread CPU and wall per callable handed to `FakeHass.async_add_executor_job`. The harness runs these inline, so thread CPU is the job's own cost. Flagged `OVER_BUDGET` above 500 ms.
- `loop`: thread CPU per event-loop callback step, hooked at `asyncio.events.Handle._run` (an asyncio internal, restored on exit). Flagged above 10 ms, highlighted above 20 ms.
- `task`: the same per `FakeScheduler.enqueue_task` coroutine.
- `worker`: wall time per `process_offload.async_run_job` call (inline in the sim).
- `lines`: optional (`--perf-lines`), line counts for a few hot functions through `sys.monitoring`. It roughly doubles CPU time.

**Overlap of the loop and task rows.** The harness runs a whole scheduler task inside one loop step, so a task's time is also inside a loop row. Do not add the two kinds together. Read `task` for the useful granularity and `loop` for the asyncio view.

**GC handling.** The GC is disabled for the run and restored on exit. Every pause is timed through `gc.callbacks` and subtracted from any span it falls inside, so a collection is never blamed on a job (pauses of 200-400 ms were seen in the prototype). The probe collects between top-level spans, at most once per interval, and books that time as GC too. Deferring GC raises resident memory: `peak_rss` read about 370-450 MB with the probe against about 150 MB without (it levels off), so judge memory from a run without `--perf-report`. A full collection can pause for up to about 1.4 s; that time is excluded from every row.

**Report-only status.** Budgets only flag rows. No threshold fails the run, and a reporting error is printed and ignored. A fault in the probe's own per-span bookkeeping is counted in `probe_errors` and never raised into the run. A ratchet against a baseline is a later step, once the numbers are stable. Memory is bounded: count, total and max are exact, percentiles come from a 2048-sample reservoir per name. Unit tests: `tests/test_perf_probe_1728.py`.

## Disclosure Path

← Tier 1 parent: [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md)
→ Tier 3 spec: [sim-harness-spec.md](sim-harness-spec.md)
↔ Siblings: [simulation-feedback-loop.md](simulation-feedback-loop.md)
