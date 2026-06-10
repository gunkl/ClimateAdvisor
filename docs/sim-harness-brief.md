<!-- Nav: ← [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md) | → [sim-harness-spec.md](sim-harness-spec.md) | ↔ [simulation-feedback-loop.md](simulation-feedback-loop.md) -->

# Production Simulation Harness — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What is the single-engine harness and why does it replace the old simulator? | Issue #236 eliminated the standalone `ClimateSimulator` (~1,500 lines that re-implemented `automation.py`). There is now one engine: `tools/sim_harness/` drives the real production `AutomationEngine` headless. Goldens pass or fail against production code itself. | [Scope](#scope) |
| What are Tier A and Tier B, and what falls into each? | Tier A = fast unit harness; runs every commit; exercises every `automation.py` decision path. Tier B = HeadlessTarry Docker harness (CI-gated); covers coordinator state-listener timing, restart recovery, and thermostat hardware — behavior the engine cannot model headlessly. | [Tier A and Tier B](#tier-a-and-tier-b) |
| What does `tools/simulate.py` do now? | It is a thin CLI/report/MANIFEST shell (~657 lines). All engine work is delegated to `tools/sim_harness/run_production.py::run_production_scenario()`. There is no `--engine` flag and no `ClimateSimulator`. | [Scope](#scope) |
| Where do assertions land — event_log, action_log, or engine state? | All three. `event_log` carries decisions (`ceiling_guard_fired`, `nat_vent_comfort_floor_exit`, etc.). `action_log` carries service calls (`climate.set_temperature`). `engine_state` is a snapshot of live engine flags after all events process. | [sim-harness-spec.md — Assertion Surface](sim-harness-spec.md#assertion-surface) |
| What does `simulator_support: false` mean now? | The real engine CAN evaluate these once the scenario supplies the needed inputs (e.g. `predicted_indoor`). Not skipped in Tier A. Previously marked phantom because the legacy simulator had no ODE. | [Scope](#scope) |
| Where is the full Tier 3 spec covering each harness module? | Covers `fake_hass.py`, `fake_scheduler.py`, `build_engine.py`, `run_production.py`, `outcomes.py`, and `ha_stubs.py` with module-level Anchors. | [sim-harness-spec.md](sim-harness-spec.md) |

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
- Coordinator state-listener timing, HA restart recovery (Tier B — future HeadlessTarry)
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

```python
# Primary entry point for scenario execution
def run_production_scenario(scenario: dict) -> ProductionRunResult:
    """Drive the real AutomationEngine through a scenario dict, return logs."""

# Build a headless engine for direct test use
def build_headless_engine(
    config: dict | None = None,
    *,
    climate_entity: str,
    climate_state: str,
    climate_attributes: dict | None,
    start_time: datetime | None,
    sensor_polarity_inverted: bool,
) -> tuple[AutomationEngine, FakeHass, FakeScheduler, list]:
    """Returns (engine, fake_hass, scheduler, event_log)."""

# Convert event_log to legacy outcome vocab
def production_decisions(result: ProductionRunResult) -> list[ProductionDecision]:
    """Map event_log + action_log enrichment to time-ordered ProductionDecision list."""

# Check a custom assertion type
def check_assertion(
    result: ProductionRunResult,
    assertion: dict,
    decisions: list[ProductionDecision] | None,
) -> str | bool:
    """Returns the expect string on match, False if the assertion does not apply."""
```

| Symbol | Caller(s) | Purpose |
|---|---|---|
| `run_production_scenario()` | `tools/simulate.py::run_scenario_production()`, `tests/test_production_harness.py` | Run a full scenario; return event_log, action_log, engine_state, callback_errors |
| `build_headless_engine()` | `run_production.py`, direct test use | Construct engine + FakeHass + FakeScheduler with merged config |
| `production_decisions()` | `tools/simulate.py`, assertion checker | Convert event_log to legacy-vocab decisions for expect: comparison |
| `check_assertion()` | `tools/simulate.py` | Custom assertion types not in the outcome vocab |
| `install_ha_stubs()` | `ha_stubs.py`, `tests/conftest.py`, harness runtime | Idempotent HA sys.modules stub installer |

---

## Data Structures

```python
@dataclass
class ProductionRunResult:
    event_log: list[tuple[str, dict, datetime | None]]  # (event_type, payload, ts)
    action_log: list[dict]  # {"domain", "service", "data", "ts"}
    engine_state: dict[str, Any]  # snapshot of live engine flags
    callback_errors: list[tuple[datetime, BaseException]]

@dataclass
class ProductionDecision:
    time: str          # naive ISO timestamp (tz stripped for lexicographic compare)
    event_type: str    # raw production event type
    outcome: str       # legacy outcome vocabulary string
    target_temp: float | None  # set when a temperature was applied
```

---

## Invariants

1. `FakeScheduler.advance_to()` never moves the clock backwards — `ValueError` if attempted
2. Every `async_create_task` coroutine is either run or enqueued; nothing is silently dropped
3. `install_ha_stubs()` is idempotent — safe to call from both tests and harness on the same process
4. `track: "integration"` assertions are always skipped in Tier A — they are never evaluated against the headless engine
5. `simulator_support: false` assertions are evaluated normally by the production harness — they are no longer phantoms
6. `FakeServices._apply_state_feedback()` reflects every `climate` and `fan`/`switch` command back into `FakeStates` before the next engine read-back
7. The `--sign` / `--check-integrity` MANIFEST workflow is unchanged from the legacy simulator

---

## Tier A and Tier B

**Tier A — production harness (this package):**
Runs on every commit via `tests/test_production_harness.py`. Drives the real `AutomationEngine` headless. Covers every decision path in `automation.py`: natural vent, grace periods, override confirmation, occupancy setback, ceiling guard ODE, fan cycles. All `track: "logic"` assertions run here.

**Tier B — HeadlessTarry (future, CI-gated):**
Exercises behavior that requires the coordinator's state-listener layer:
- `_async_thermostat_changed` (HA state change timing)
- Coordinator restart recovery (timer state lost on restart)
- Periodic re-classification cycle driven by `_async_update_data`
- Thermostat hardware response latency

Assertions tagged `track: "integration"` are deferred to Tier B. They are skipped in Tier A without causing a failure.

---

## Disclosure Path

← Tier 1 parent: [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md)
→ Tier 3 spec: [sim-harness-spec.md](sim-harness-spec.md)
↔ Siblings: [simulation-feedback-loop.md](simulation-feedback-loop.md)
