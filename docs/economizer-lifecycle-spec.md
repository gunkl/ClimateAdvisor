<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [economizer_fsm.py](../custom_components/climate_advisor/economizer_fsm.py) | ↔ [Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md) -->

# Economizer Lifecycle — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What are the 3 economizer session states and how do they map to flags? | `INACTIVE`, `COOL_DOWN`, `MAINTAIN` — derived purely from `_economizer_active`/`_economizer_phase`. `_economizer_active` is fully redundant with `_economizer_phase != "inactive"` in every real write site, so this is one 3-value enum, not a composed multi-axis state. | [State Transitions](#state-transitions) |
| Where is the derivation computed, and is it load-bearing? | `economizer_lifecycle.py::derive_economizer_lifecycle_state()`, exposed read-only via `AutomationEngine.economizer_lifecycle_state`. Always load-bearing — it supplies the FSM's starting state on every tick. (During the Phase 5 migration this was conditional on a now-removed `_economizer_fsm_authoritative` flag; that flag and the legacy branch it gated were deleted in Phase 6, so this is unconditional today.) | [Scope](#scope) |
| What is the economizer, occupant-facing? | A two-phase window-cooling strategy: with windows already open on a hot day, the fan alone (or fan-assisted AC) can sometimes hold comfort for free instead of running the compressor continuously. Getting the gate wrong either wastes a free-cooling window (AC runs when a fan would suffice) or pulls in outdoor air that isn't actually helping. | [economizer_gate.py module docstring](../custom_components/climate_advisor/economizer_gate.py) |
| Who calls this, and how often? | Exactly one production call site: `coordinator.py`'s 30-min regular cycle (`_should_run_regular_cycle_window_cooling_check()` gate, then `automation_engine.check_window_cooling_opportunity(...)`) — unlike nat-vent/fan's many call sites, this is genuinely single-entry. | [Scope](#scope) |
| Is nat-vent-active a real transition, or a defer? | A defer, not a transition. If `_natural_vent_active` is True, the economizer stands down entirely for that tick but does **not** deactivate — a real, preserved asymmetry from the not-hot-day branch (which does deactivate). Modeled explicitly as `EconomizerTransition.deferred=True` so the FSM-authoritative shell can reproduce production's exact "return False without touching state" behavior. | [State Transitions](#state-transitions) |
| How was this spec's accuracy verified? | Differential replay via `economizer_fsm_authoritative_compare.py` proved zero divergence against the full 90-scenario corpus during Phase 5. The comparator was deleted in Phase 6 once the legacy branch was removed. (See [Verification](#verification).) | [Verification](#verification) |
| When did the economizer FSM become sole authority? | Phase 6 (Issues #757–#770) permanently deleted the legacy two-phase branch, the `_economizer_fsm_authoritative` flag itself, and the shadow-engine infrastructure, once the flag had run permanently `True` in production for weeks with zero corpus divergence (Phase 5, Issue #746). `economizer_fsm.transition()` is now the sole, unconditional decision path — there is no flag left to toggle and no second code path left to diverge from. | [§ FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) |
| What happened to the live shadow-engine comparison for economizer? | It existed only during the migration and was permanently deleted in Phase 6. The `economizer_mirror` diagnostic axis (and the entire shadow-engine infrastructure) was removed once all 4 FSMs had proved reliable in production use — there is no live shadow comparison running today. | [§ FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) |
| Can the economizer claim it activated the fan when the fan-ON command was actually rate-limited or overridden mid-await? | Before Issue #936, yes — `_apply_economizer_fsm_state(result.to_state)` was called before the `await self._activate_fan(...)` resolved, so a deferred/overridden command still left `_economizer_active=True`. Fixed by `_apply_economizer_fsm_state_after_activation()`, the direct mirror of nat-vent's `_apply_nat_vent_fsm_state_after_activation()` (Issue #935). | [Rate-Limit / Override Symmetry (Issue #936)](#rate-limit--override-symmetry-issue-936-mirrors-931935) |
| Can the economizer strand itself "active" when the fan-OFF command on deactivation was rate-limited? | Before Issue #936, yes — `_deactivate_economizer()` cleared `_economizer_active`/`_economizer_phase` unconditionally before the fan-stop command's result was known. Fixed to gate the flag-clear on `FanCommandResult`, the direct mirror of nat-vent's `_end_nat_vent_session()` (Issue #931). | [Rate-Limit / Override Symmetry (Issue #936)](#rate-limit--override-symmetry-issue-936-mirrors-931935) |

## Scope

- **Files:** `custom_components/climate_advisor/economizer_lifecycle.py` (pure session-state derivation), `economizer_gate.py` (pure eligibility/phase-selection gate), `economizer_fsm.py` (unified transition table), `automation.py` (`check_window_cooling_opportunity()`, `_check_window_cooling_opportunity_fsm()`, `_deactivate_economizer()`). The `_economizer_fsm_authoritative` flag that gated the Phase 5 migration was deleted in Phase 6 along with the legacy branch it selected — `check_window_cooling_opportunity()` now always delegates to the FSM path unconditionally.
- **Entry point:** `AutomationEngine.economizer_lifecycle_state` (property) / `derive_economizer_lifecycle_state()` (pure function). Decision entry: `AutomationEngine.check_window_cooling_opportunity()`.

**Does NOT cover:**
- Nat-vent's own lifecycle (`nat_vent_lifecycle.py`/`nat_vent_fsm.py`) — the economizer explicitly defers to an active nat-vent session (see the Anchors table above); the two are mutually exclusive by construction, never a shared state machine.
- Fan/WHF's own hardware-level session tracking (`fan_lifecycle.py`) — the economizer calls `_activate_fan()`/`_deactivate_fan()` the same way nat-vent does, but does not model fan hardware state itself.
- The time-of-day window gate's exact hour boundaries (`ECONOMIZER_MORNING_START_HOUR`/`ECONOMIZER_MORNING_END_HOUR`/`ECONOMIZER_EVENING_START_HOUR`/`ECONOMIZER_EVENING_END_HOUR` in `const.py`) — unchanged by this extraction, just consumed as a caller-resolved `in_window` boolean, same convention as nat-vent's `in_sleep_window`.

## State Transitions

**States:**

| State | Flags | Meaning |
|---|---|---|
| `INACTIVE` | `_economizer_active=False`, `_economizer_phase="inactive"` | No economizer session running. |
| `COOL_DOWN` | `_economizer_active=True`, `_economizer_phase="cool-down"` | Indoor is above `comfort_cool`; the fan assists the comfort band's own AC cooling (does not set HVAC mode/setpoint itself — Issue #264). |
| `MAINTAIN` | `_economizer_active=True`, `_economizer_phase="maintain"` | Indoor is at or below `comfort_cool` (or `aggressive_savings` is set, which shortcuts straight here regardless of indoor temp) — fan-only ventilation; the comfort band stays armed and self-arbitrates. |

**Transition table** (`economizer_fsm.transition(current_state, event)`, evaluated on every tick from the single real call site):

| From | Condition | To | Notes |
|---|---|---|---|
| any | `day_type != "hot"` (including no classification yet) | `INACTIVE` | Deactivates via `_deactivate_economizer()` if a session was active — resumes AC if the classification's `hvac_mode == "cool"`. |
| any | `_natural_vent_active == True` | **unchanged** (`deferred=True`) | Defer, not a transition — see the asymmetry callout in the Anchors table. |
| any | eligible (`windows_physically_open and outdoor <= comfort_cool + delta and in_window and direction_ok`) and `aggressive_savings` | `MAINTAIN` | Savings mode shortcuts past the indoor-temp phase check entirely. |
| any | eligible, not `aggressive_savings`, `indoor > comfort_cool` | `COOL_DOWN` | |
| any | eligible, not `aggressive_savings`, `indoor <= comfort_cool` (or indoor unknown) | `MAINTAIN` | |
| any | not eligible | `INACTIVE` | Deactivates if a session was active. |

`direction_ok` (`temperature.free_cooling_direction_ok()`, reused unchanged from nat-vent's own Issue #327/#429 consolidation) fails open (`True`) when indoor is unknown, and is exposed on `EconomizerTransition.direction_ok` so the FSM-authoritative shell can reproduce production's own direction-rejected DEBUG log without recomputing the check.

Fan activation/deactivation and INFO logging only fire when the resolved phase actually **changes** (`EconomizerTransition.changed`) — matches legacy's own `if self._economizer_phase != "...":` guards exactly, avoiding a redundant `_activate_fan()` call (and its own rate-limit/logging side effects) every 30-min tick a session merely continues in the same phase.

**As of Issue #936, `_apply_economizer_fsm_state(result.to_state)` is no longer called directly from `_check_window_cooling_opportunity_fsm()` for the MAINTAIN/COOL_DOWN activation branches.** Each branch first `await`s `self._activate_fan(...)`, captures the returned `FanCommandResult`, and applies the FSM state through `_apply_economizer_fsm_state_after_activation(result.to_state, activation_result)` instead — see [Rate-Limit / Override Symmetry (Issue #936)](#rate-limit--override-symmetry-issue-936-mirrors-931935) below. The `INACTIVE`-via-deactivation and defer-on-nat-vent-active rows in the transition table above are unaffected — those paths don't await a fan command between deciding the state and applying it.

## Rate-Limit / Override Symmetry (Issue #936, mirrors #931/#935)

Nat-vent's lifecycle went through the identical defect shape twice, on its two opposite sides (`docs/nat-vent-lifecycle-spec.md` §17 in `08-COMPUTATION-REFERENCE.md`, "Natural Ventilation"):

- **Exit side (`_end_nat_vent_session()`, Issue #931):** clearing the session flags before confirming the fan-stop command actually executed left a physically-running fan with nothing left to retry stopping it, if the Issue #641 5-minute anti-cycling floor deferred the command.
- **Entry side (`_apply_nat_vent_fsm_state_after_activation()`, Issue #935/#706 Bug F):** applying the pre-await FSM decision after an `await self._activate_fan(...)` that got rate-limited or overridden left the status claiming a session was active when the fan was never actually told to turn on.

Issue #936 found and fixed the same two gaps on the economizer's own activate/deactivate pair, independently discovered while investigating the economizer specifically (not copy-pasted from the nat-vent fix without re-verifying the economizer's own call shape first):

**Activation (mirrors #935 — `_apply_economizer_fsm_state_after_activation()`):**

```python
def _apply_economizer_fsm_state_after_activation(
    self, to_state: EconomizerLifecycleState, activation_result: FanCommandResult
) -> None:
    if activation_result in (
        FanCommandResult.OVERRIDDEN,
        FanCommandResult.RATE_LIMITED_NEW,
        FanCommandResult.RATE_LIMITED_DUP,
    ):
        self._apply_economizer_fsm_state(EconomizerLifecycleState.INACTIVE)
    else:
        self._apply_economizer_fsm_state(to_state)
```

Both `_check_window_cooling_opportunity_fsm()` branches (`MAINTAIN` and `COOL_DOWN`) now call `await self._activate_fan(...)`, capture the result, and route the FSM-state write through this method instead of applying `result.to_state` before the await. Same fallback direction as nat-vent's entry-side mirror: a deferred or overridden fan-ON command means the fan was never actually told to turn on, so the correct state is `INACTIVE`, not the stale pre-await decision — this lets the FSM's own eligibility re-check retry activation on the next tick instead of the status page silently claiming ventilation is running.

**Deactivation (mirrors #931 — the gated flag-clear in `_deactivate_economizer()`):**

```python
async def _deactivate_economizer(self, outdoor_temp: float) -> None:
    unit = self.config.get("temp_unit", "fahrenheit")
    c = self._current_classification
    result = await self._deactivate_fan(reason="economizer off — fan no longer needed")
    if result in (FanCommandResult.RATE_LIMITED_NEW, FanCommandResult.RATE_LIMITED_DUP):
        pass  # command never reached the fan — leave the session active for retry
    else:
        self._economizer_active = False
        self._economizer_phase = "inactive"
    ...  # HVAC-resume block runs unconditionally either way
```

Same fallback direction as nat-vent's exit-side mirror: a rate-limited fan-stop command means the fan is still physically running, so the session flags must stay active — `_check_window_cooling_opportunity_fsm()` derives its `current_state` input from these same flags on its next tick, so clearing them early would leave nothing to retry the stop. The HVAC-resume block (re-arming `cool` mode from the current classification, when applicable) runs unconditionally regardless of the fan result — the same asymmetry `_exit_nat_vent()` already has on its own sensors-closed branch (HVAC always resumes; only the fan-session bookkeeping is gated).

**Log-level split, same convention as #931/#935's own follow-up fix (Issue #935 follow-up):** `OVERRIDDEN` is a genuine anomaly (WARNING); `RATE_LIMITED_NEW` (a fresh deferral) is routine anti-cycling behavior worth noting once (INFO); `RATE_LIMITED_DUP` (a repeat hit within an already-reported deferral window) is per-tick noise (DEBUG) — this is the same three-way split `_apply_nat_vent_fsm_state_after_activation()` and `_end_nat_vent_session()`'s callers use, not a new logging policy invented for the economizer.

## Invariants

- `_economizer_active` and `_economizer_phase` are always set together (never independently) by every real write site (`_apply_economizer_fsm_state()`, `_deactivate_economizer()`) — confirmed by direct code reading before choosing the single-enum (not composed multi-axis) lifecycle-state shape.
- The economizer never overrides nat-vent; nat-vent taking over is always a same-tick defer, never a forced deactivation.
- `_check_window_cooling_opportunity_fsm()` is now the sole implementation of `check_window_cooling_opportunity()` — there is no legacy branch left for it to diverge from. (During the Phase 5 migration, a full-corpus differential comparator enforced zero-divergence between the FSM branch and the legacy branch before the legacy branch was permanently deleted in Phase 6 — see [Verification](#verification).)
- **(Issue #936)** `_economizer_active`/`_economizer_phase` never claim a state the paired fan command didn't actually reach: activation only applies `result.to_state` when `_activate_fan()`'s result is not `OVERRIDDEN`/`RATE_LIMITED_NEW`/`RATE_LIMITED_DUP` (else `INACTIVE`); deactivation only clears the flags when `_deactivate_fan()`'s result is not `RATE_LIMITED_NEW`/`RATE_LIMITED_DUP` (else the flags are left active for retry). See [Rate-Limit / Override Symmetry](#rate-limit--override-symmetry-issue-936-mirrors-931935).

## Verification

- **Unit tests** (pure logic, no HA stubs): `tests/test_economizer_gate.py` (eligibility/phase-selection math), `tests/test_economizer_lifecycle.py` (state derivation), `tests/test_economizer_fsm.py` (transition wiring — short-circuit ordering, changed/unchanged, direction_ok exposure).
- **Flag ownership**: `tests/test_fsm_flag_ownership.py`'s AST-based registry confirms `_apply_economizer_fsm_state()` is the sole `_apply_*_fsm_state()`-shaped writer of `_economizer_active`/`_economizer_phase`.
- **End-to-end behavior**: `tests/test_economizer.py` exercises the two-phase economizer through the public `check_window_cooling_opportunity()` entry point and passes against today's FSM-only implementation.
- **Rate-limit/override symmetry (Issue #936)**: `_apply_economizer_fsm_state_after_activation()`'s three-way branch (OVERRIDDEN/RATE_LIMITED_NEW/RATE_LIMITED_DUP → INACTIVE, else the real `to_state`) plus its two-tick retry is covered by `tests/test_economizer_activation_rate_limit_guard.py`. `_deactivate_economizer()`'s gated flag-clear, the no-op-tick regression pin, and the COOL_DOWN→MAINTAIN-with-fan-already-active guard are covered by `tests/test_economizer.py::TestEconomizerDeactivationRateLimitGuard`/`TestEconomizerUnchangedTickNoFlagWrite`/`TestEconomizerMaintainTransitionFanAlreadyActive`. End-to-end regression coverage: `tools/simulations/pending/issue_936_economizer_deactivation_rate_limited.json` (verified via revert-test to actually fail against pre-fix code). Mirrors the equivalent nat-vent coverage (`tests/test_nat_vent_activation_rate_limit_guard.py`, `tests/test_end_nat_vent_session.py`).
- **Historical (Phase 5, no longer present)**: a decision-equivalence comparator (`economizer_fsm_authoritative_compare.py`) flipped `_economizer_fsm_authoritative` True on every engine constructed during a scenario replay and asserted a byte-identical `event_log`/`action_log` against the untouched legacy baseline across all 90 golden+pending scenarios (zero divergence, zero allowlist), plus a positive control proving it could detect an injected regression. A combined-flip variant (`combined_fsm_authoritative_compare.py`) did the same across all 7 `*_fsm_authoritative` flags at once (Issue #746). Both comparator tools and their tests (`tests/test_economizer_fsm_authoritative_compare.py`, `tests/test_combined_fsm_authoritative_compare.py`) were deleted in Phase 6 once the legacy branches they compared against were removed — confirmed absent from `tools/sim_harness/` and `tests/` (only stale `.pyc` bytecode remains).

## Code Reference

| Function/property | File | Role |
|---|---|---|
| `decide_economizer_transition(inputs)` | `economizer_gate.py` | Pure eligibility + phase-selection leaf. |
| `derive_economizer_lifecycle_state(inputs)` | `economizer_lifecycle.py` | Pure flags → 3-state enum derivation. |
| `transition(current_state, event)` | `economizer_fsm.py` | Unified `(state, event) -> Transition` dispatcher — day_type/nat-vent short-circuits, then the gate. |
| `AutomationEngine.economizer_lifecycle_state` | `automation.py` | Read-only property view of current session state. |
| `AutomationEngine._build_economizer_fsm_inputs(...)` | `automation.py` | Builds `EconomizerFsmInputs` from live engine state + call parameters. |
| `AutomationEngine._apply_economizer_fsm_state(state)` | `automation.py` | Writes `_economizer_active`/`_economizer_phase` from a transition result. |
| `AutomationEngine._check_window_cooling_opportunity_fsm(...)` | `automation.py` | FSM-authoritative shell — same side effects (fan/HVAC/logging) as the legacy branch, driven by `transition()`'s result. Since Issue #936, applies its FSM-state write via `_apply_economizer_fsm_state_after_activation()` rather than directly, for both the MAINTAIN and COOL_DOWN branches. |
| `AutomationEngine.check_window_cooling_opportunity(...)` | `automation.py` | Public entry point; always delegates to `_check_window_cooling_opportunity_fsm()` — the legacy two-phase body it used to dispatch to alongside was permanently deleted in Phase 6 (Issue #757). |
| `AutomationEngine._apply_economizer_fsm_state_after_activation(to_state, activation_result)` | `automation.py` | Issue #936 — activation-side gate: applies `to_state` only when the paired `_activate_fan()` result wasn't `OVERRIDDEN`/`RATE_LIMITED_NEW`/`RATE_LIMITED_DUP` (else `INACTIVE`). Direct mirror of `AutomationEngine._apply_nat_vent_fsm_state_after_activation()` (Issue #935). |
| `AutomationEngine._deactivate_economizer(outdoor_temp)` | `automation.py` | Issue #936 — deactivation-side gate: clears `_economizer_active`/`_economizer_phase` only when the paired `_deactivate_fan()` result wasn't `RATE_LIMITED_NEW`/`RATE_LIMITED_DUP`; HVAC-resume runs unconditionally. Direct mirror of `AutomationEngine._end_nat_vent_session()` (Issue #931). |
