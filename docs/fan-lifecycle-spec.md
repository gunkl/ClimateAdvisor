<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [fan_fsm.py](../custom_components/climate_advisor/fan_fsm.py) | ↔ [Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md) -->

# Fan/WHF Lifecycle — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Why 5 composed axes instead of one flat enum, unlike nat-vent's 4-state enum? | A WHF session can occupy several axes at once (on, overridden, HVAC-suppressing, rate-limited) simultaneously. Flattening to one enum would combinatorially explode or drop states a status card needs. | [State Model](#state-model) |
| What are the 16 event kinds and where does each one's real call site live? | One per real production entry point in `automation.py` — see the table in [Event Kinds](#event-kinds). | [Event Kinds](#event-kinds) |
| Why do 2 of the 16 kinds never move `to_state` away from `from_state`? | `THERMO_BACKSTOP_TICK`/`THERMOSTAT_CHECK_TICK` only inform a downstream routing decision (`_exit_nat_vent()`/`_deactivate_fan()` selection between several possible event types) this FSM's 5 axes cannot represent without duplicating that routing logic — the same "sibling threshold drift" trap Issues #400/#402/#417/#456/#458 already document for this codebase. | [Deliberately Unreachable Kinds](#deliberately-unreachable-kinds) |
| How does fan/WHF's wiring differ structurally from nat-vent/door-window/override-grace? | `fan_fsm.py` is wired inside `AutomationEngine` via `_resolve_fan_fsm_state()`, not as a separate coordinator computation. (See [Wiring](#wiring).) | [Wiring](#wiring) |
| Did fan/WHF ever have shadow-diagnostic coverage like the other 3 lifecycles? | Yes, a `fan_mirror` axis existed. Phase 6 (Issues #757–#770) deleted it along with the dual-engine shell. (See [Shadow-Diagnostic Coverage](#shadow-diagnostic-coverage).) | [Shadow-Diagnostic Coverage](#shadow-diagnostic-coverage) |
| How does this FSM read nat-vent's own session-ownership state? | As a plain boolean (`natural_vent_active`), not `NatVentLifecycleState` — `_release_whf_and_reclassify()`'s guard only needs "does a nat-vent session currently claim this fan," which decouples this module from a state shape that has already changed twice (Issues #672, #706). | [Nat-Vent Coupling](#nat-vent-coupling) |
| Were `_activate_fan`/`_deactivate_fan` ever "mirrored" in a shadow-coverage registry the way nat-vent's entry points were? | Historically classified `"internal"` in the now-deleted `tests/test_shadow_engine_coverage.py` registry — neither method needed a `_mirror_to_shadow()` replay call site, since the shadow engine (while it existed) ran its own FSM dispatch internally. That registry and the shadow engine it audited are both gone as of Phase 6. | [Shadow-Coverage Classification](#shadow-coverage-classification) |
| Did flipping `_fan_fsm_authoritative` ever change production behavior? | No — while the flag existed, it was `False` for `_engine_a`/production for its whole lifetime, so the legacy body of all 16 entry points ran unconditionally and unchanged; only `_engine_b`/shadow ever applied the FSM's `to_state`. Phase 6 deleted the flag along with the legacy branch and the shadow engine — the FSM is now the sole, unconditional path, so there is no cutover left to prove a no-op for. | [Verification](#verification) |
| Is a single source of truth enforced for the 6 pure decision functions this FSM assembles? | Yes — `tests/test_fan_fsm_single_caller.py` AST-scans the whole integration and requires each of the 6 to be called from at most 2 places: `fan_fsm.py`'s own dispatch, and one named legacy closure in `automation.py`. | [Verification](#verification) |

## Scope

- **Files:** `custom_components/climate_advisor/fan_lifecycle.py` (Phase 1, pure composed-state derivation), `custom_components/climate_advisor/fan_toggle_rate_limit.py` (Phase 2, rapid-cycling backstop, delegated to from `automation.py`'s `_fan_toggle_rate_limited()`), `custom_components/climate_advisor/fan_fsm.py` (Phase 3, the unified `(state, event) -> Transition` table), `custom_components/climate_advisor/automation.py` (Phase 4/5 — `_build_fan_fsm_inputs()`, `fan_lifecycle_state` property, `_apply_fan_fsm_state()`, `_resolve_fan_fsm_state()`, and the 16 real entry points' dispatch wiring, now the sole path as of Phase 6). `coordinator.py` no longer carries any fan/WHF-specific axis — the per-engine cutover flag and the `fan_mirror` shadow-diagnostic axis were both deleted along with the dual-engine shell in Phase 6 (Issues #757–#770); only historical comments referencing them remain.
- **Entry point:** `AutomationEngine.fan_lifecycle_state` (property) / `fan_lifecycle.derive_fan_lifecycle_state()` (pure function) / `fan_fsm.transition()` (the unified transition table) / `AutomationEngine._resolve_fan_fsm_state()` (the single dispatch chokepoint every real entry point calls through).

**Does NOT cover:**
- The 6 imported pure decision functions' own internal logic (`decide_fan_toggle_rate_limit()`, `decide_fan_drift_reconciliation()`, `decide_fan_cycle_on()`/`decide_fan_cycle_off()`, `decide_fan_thermo_backstop()`, `decide_fan_thermostat_check()`) — each already has its own exhaustive unit-test coverage and is out of scope here; this spec covers the wiring/integration layer only, per `fan_fsm.py`'s own module docstring.
- The WHF/AC mutual-exclusion choke-point guard (`_whf_owns_hvac()`) — see `docs/08-COMPUTATION-REFERENCE.md` §"Structural WHF/AC Mutual Exclusion" for that mechanism's own history (Issue #392 Fix 1b); this spec covers only the `hvac_ownership` axis's read-only derivation, now re-expressed in terms of `fan_lifecycle_state` (Phase 4).
- Nat-vent's own session lifecycle (`_natural_vent_active`/`_nat_vent_soft_start`) — owned by `nat-vent-lifecycle-spec.md`. This spec only documents the one cross-lifecycle read (`natural_vent_active` as a plain boolean input) — see [Nat-Vent Coupling](#nat-vent-coupling).
- Pause/grace/override lifecycles — owned by `grace-periods-spec.md`. Fan/WHF grace starts (`_start_grace_period()`, "unprotected" grace triggers) are a real coupling point but are not modeled as a fan_fsm.py axis.

## State Model

`FanLifecycleState` (`fan_lifecycle.py`) is a composed dataclass of 5 independent axes, not a single flat enum:

| Axis | Enum | States | Real flag(s) it derives from |
|---|---|---|---|
| `physical` | `FanPhysicalState` | `OFF`, `ON`, `ON_DRIFT_SUSPECTED` | `_fan_active`, `_fan_drift_tick_count` (nonzero only while a drift AWAITING confirmation is in progress) |
| `override` | `FanOverrideState` | `NONE`, `ACTIVE`, `ACTIVE_REMOTE_TIMER` | `_fan_override_active`, `_fan_remote_timer_hours` |
| `cycling` | `FanCyclingState` | `IDLE`, `ACTIVE`, `SUSPENDED` | `_fan_min_runtime_active`, `_fan_override_active` (an active override suspends cycling) |
| `hvac_ownership` | `WhfHvacOwnership` | `NONE`, `SUPPRESSING` | `fan_mode` (WHOLE_HOUSE/BOTH only) AND `_pre_fan_hvac_mode is not None` |
| `rate_limit` | `FanRateLimitState` | `NOT_DEFERRED`, `DEFERRED_ACTIVATE`, `DEFERRED_DEACTIVATE` | `_fan_rate_limited_until`, `_fan_rate_limited_direction` |

The all-idle `FanLifecycleState.initial()` — `OFF`/`NONE`/`IDLE`/`NONE`/`NOT_DEFERRED` — matches every axis's default field value in a freshly-constructed `AutomationEngine`.

Each axis is derived independently and purely from the current flags — `derive_fan_lifecycle_state()` has no memory of which call path produced those flag values, so its correctness does not depend on the event-kind enumeration below being complete (same invariant `derive_nat_vent_lifecycle_state()` documents for itself).

## Event Kinds

`fan_fsm.py`'s `FanFsmEventKind` has 16 members, one per real production entry point — unlike nat-vent's state-first dispatch (periodically re-evaluated from a small number of call sites reachable from either state), fan/WHF is **handler-triggered**: `transition()` dispatches on `event.kind` first, the same shape `override_grace_fsm.py` uses for itself.

| Event kind | Real `automation.py` entry point | Group |
|---|---|---|
| `ACTIVATE_REQUESTED` | `_activate_fan()` | 2 (decision-bearing) |
| `DEACTIVATE_REQUESTED` | `_deactivate_fan()` | 2 |
| `STARTUP_RECONCILE` | `reconcile_fan_on_startup()` (3 flag-write sites) | 1 (caller-already-decided) |
| `MANUAL_OVERRIDE_DETECTED` | `handle_fan_manual_override()` | 1 |
| `OVERRIDE_CLEARED` | `clear_fan_override()` | 1 |
| `USER_FAN_OFF` | `on_fan_turned_off()` | 1 |
| `TIMER_BOUNDARY_SETTLE` | `on_fan_turned_off()`'s RF-timer-boundary coalesce branch (Issue #530) | 1 |
| `FLAGS_CLEARED_FOR_GRACE` | `_clear_fan_flags_and_start_grace()` | 1 |
| `MIN_RUNTIME_CYCLE_ON` | `_fan_cycle_on()` | 2 |
| `MIN_RUNTIME_CYCLE_OFF` | `_fan_cycle_off()` | 2 |
| `MIN_RUNTIME_CYCLE_STOPPED` | `_stop_fan_min_runtime_cycles()` | 1 |
| `DRIFT_TICK` | `_reconcile_fan_physical_drift()` | 2 |
| `THERMO_BACKSTOP_TICK` | `_thermo_backstop_task()` / `_start_fan_thermo_backstop()` / `_cancel_fan_thermo_backstop()` | 2 (never moves `to_state`) |
| `THERMOSTAT_CHECK_TICK` | `fan_thermostat_check()` | 2 (never moves `to_state`) |
| `WHF_SUPPRESSION_REQUESTED` | `_suppress_hvac_for_whf()` (via the dispatcher) | 1 |
| `WHF_RELEASE_REQUESTED` | `_release_whf_and_reclassify()` (via the dispatcher) — also covers `_deactivate_fan()`'s two release branches, which call the same dispatcher | 1 |

**Group 1 — pure caller-already-decided kinds** (9 of 16): the real production method has already mutated the underlying `AutomationEngine` flags by the time this FSM would be consulted — same convention `_resolve_override_grace_fsm_state()` establishes for override/grace. `event.inputs` is the POST-change snapshot; `to_state` is simply `derive_fan_lifecycle_state(event.inputs)`. No pure-fn call, no shell-directive fields populated.

**Group 2 — decision-bearing kinds** (7 of 16): these correspond to real call sites where a pure decision function itself determines whether/how a flag changes. `event.inputs` is the PRE-change snapshot: `fan_fsm.py` calls the relevant pure decision function, populates the outcome into the transition's shell-directive fields (`rate_limit_outcome`, `drift_outcome`, `cycle_outcome`, `thermostat_outcome`, `thermo_backstop_should_be_armed`), and derives `to_state` by re-deriving composed state against an *effective* inputs snapshot that applies only the flag changes the pure function's own outcome implies — never a flag change the pure function didn't decide. Mirrors `nat_vent_fsm.py`'s `fan_should_be_active` precedent: the FSM surfaces what a pure decision implies, the shell still owns applying the real side effects (hardware commands, grace periods, HVAC restores, event emission).

## Deliberately Unreachable Kinds

2 of the 7 decision-bearing kinds — `THERMO_BACKSTOP_TICK` and `THERMOSTAT_CHECK_TICK` — deliberately never move `to_state` away from `from_state` (`to_state == from_state` always for these two):

- **`THERMO_BACKSTOP_TICK`**: `decide_fan_thermo_backstop()` only answers "should the 5-minute re-arm timer fire" — it reads `fan_running` but writes no fan-lifecycle flag of its own (timer arm/disarm is not one of the 5 composed axes). Only `thermo_backstop_should_be_armed` carries the decision.
- **`THERMOSTAT_CHECK_TICK`**: every non-KEEP outcome of `decide_fan_thermostat_check()` in the real method routes through `_exit_nat_vent()` or `_deactivate_fan()` with additional context this FSM's 5 composed axes cannot represent without duplicating that routing logic here — event-type selection between `nat_vent_outdoor_rise_exit`/`fan_deactivated`/`nat_vent_comfort_floor_exit`, HVAC-restore-vs-pause-for-open-sensor decisions, WHF suppression release. Modeling a partial version of that routing would risk silently drifting from `_exit_nat_vent()`'s own logic — exactly the "sibling threshold drift" failure mode this codebase has hit repeatedly (Issues #400/#402/#417/#456/#458), and precisely why `check_natural_vent_conditions()`'s exit-chain extraction (`nat-vent-lifecycle-spec.md`'s [Known Duplicate-Logic Race](nat-vent-lifecycle-spec.md#known-duplicate-logic-race-issue-608-finding)) is held up as the cautionary precedent.

Both kinds still populate their outcome field (`thermostat_outcome`/`thermo_backstop_should_be_armed`) on the returned `FanTransition` — a future wired caller reads that field to decide what to do next (e.g. raise a follow-up `DEACTIVATE_REQUESTED` or a nat-vent-FSM exit event); the composed state simply doesn't change for these two kinds by design, not by omission.

## Wiring

`fan_fsm.py` is wired **inside** `AutomationEngine` itself through a single dispatch chokepoint: `_resolve_fan_fsm_state(*, kind, origin_state=None, **input_overrides)`, called from all 16 real entry points. As of Phase 6 (Issues #757–#770), the FSM is the sole, permanent authority — the legacy inline branches have been deleted.

- **`_resolve_fan_fsm_state()` calls `fan_fsm.transition()` unconditionally** and applies its `to_state` via `_apply_fan_fsm_state()`, which writes `_fan_active`/`_fan_override_active`/`_fan_min_runtime_active`/`_pre_fan_hvac_mode` (None-clear only) — the inverse of `fan_lifecycle_state`'s own derivation, for exactly the 4 fields that derivation composes. Every other fan/WHF-adjacent field (`_fan_on_since`, `_fan_remote_timer_hours`, `_fan_toggle_command_time`, `_fan_drift_tick_count`, etc.) stays owned by the real side-effecting call site.
- **`_resolve_fan_fsm_state()` is the single emit point for `WHF_HVAC_SUPPRESSED`/`WHF_HVAC_RELEASED`** — a before/after diff of `_whf_owns_hvac()` around the `transition()` call.

There is a single `AutomationEngine` instance in production (the dual-engine production/shadow shell was deleted in Phase 6); its `fan_lifecycle_state` property (`derive_fan_lifecycle_state()` over its own live flags) is always safe to read as the current composed state.

## Nat-Vent Coupling

`FanFsmInputs.natural_vent_active` is a **plain boolean**, not nat-vent's own `NatVentLifecycleState` object (Issue #530). Several real fan-subsystem methods (`_release_whf_and_reclassify()`'s guard, most notably) read `self._natural_vent_active` as a simple flag-ownership check — "does the nat-vent session still own this fan" — not as a state-composition input the way `paused_by_door`/`natural_vent_active` feed `door_window_fsm.py`. Reading a plain bool here, rather than importing `nat_vent_lifecycle.NatVentLifecycleState` and pattern-matching on it, keeps this module decoupled from nat-vent's own state shape — which has already changed twice (Issue #672, Issue #706) without this module needing to know. A future `FanFsmInputs` field could upgrade to the richer type if a decision genuinely needs more than "is a session currently claiming this fan," but no branch in this phase does.

This is the fan/WHF subsystem's only direct cross-lifecycle read. It does not go through `lifecycle_dispatcher.py`'s pub/sub event channel (unlike the door/window ↔ nat-vent coupling `nat-vent-lifecycle-spec.md`'s "First live disagreement" incident describes) — `_build_fan_fsm_inputs()` reads `self._natural_vent_active` directly off the engine, same canonical-attribute convention the other 3 lifecycle builders already follow (Issue #717's revert rationale: a same-instance emit/consume round-trip can never actually go stale relative to the canonical attribute, so routing through a dispatcher-synced mirror buys nothing).

## Shadow-Diagnostic Coverage

While the dual-engine production/shadow shell existed, `coordinator.py`'s `_update_shadow_engine_diagnostic()` tracked a **`fan_mirror`** axis comparing `self.automation_engine.fan_lifecycle_state` against `self.shadow_automation_engine.fan_lifecycle_state`, alongside the equivalent axes for the other 3 lifecycles. There was deliberately no paired `fan_fsm` axis: unlike nat-vent/door-window/override-grace, whose FSMs were never wired into either engine and so needed a free-standing third computation to compare against, fan/WHF's shadow engine already computed FSM-derived state directly (via the now-deleted `_fan_fsm_authoritative` flag) — a second `fan_fsm` axis would have read identically to `fan_mirror` with zero independent signal. Phase 6 (Issues #757–#770) deleted the dual-engine shell, `_update_shadow_engine_diagnostic()`, and the diagnostic sensor's `fan_*` attributes entirely, once the FSM path had run production-authoritative with zero divergence across the full scenario corpus. Nothing in current code exposes a `fan_mirror` comparison; there is only one live engine.

## Shadow-Coverage Classification

While it existed, `tests/test_shadow_engine_coverage.py`'s AST-based `_TRACKED_FIELDS` registry classified `_activate_fan`/`_deactivate_fan` as `"internal"` — neither needed a `_mirror_to_shadow()` replay call site, since the shadow engine ran its own fan/WHF FSM dispatch internally on every one of its own 16 entry-point calls. That test file, the registry, and the shadow engine it audited were all removed in Phase 6; there is no coverage-classification mechanism for fan/WHF methods today because there is no second engine to classify coverage against.

## Verification

Per this project's Accuracy Verification convention: `fan_fsm.py`'s own module docstring documents the 16-event-kind/2-group split and both intentionally-unreachable kinds in full; this spec's tables were cross-checked against that docstring and against `automation.py`'s "Issue #731 Phase 5" comments at each of the 16 real dispatch call sites (all confirmed present by direct grep as of this spec's authoring).

**Wiring-correctness tests** (not decision-logic re-proofs — each of the 6 imported pure functions already has its own exhaustive unit coverage, out of scope here per `fan_fsm.py`'s own docstring):
- `tests/test_fan_lifecycle.py` — `derive_fan_lifecycle_state()`'s per-axis derivation rules, including the drift-tick-count-nonzero-implies-ON safety invariant.
- `tests/test_fan_toggle_rate_limit.py` — the Phase 2 rate-limit backstop `automation.py`'s `_fan_toggle_rate_limited()` delegates to.
- `tests/test_fan_fsm.py` — `fan_fsm.transition()`'s own dispatch-on-event-kind wiring for all 16 kinds, both groups' effective-inputs substitution rules, and the 2 deliberately-unreachable kinds' `to_state == from_state` invariant.
- `tests/test_fan_fsm_single_caller.py` — AST-scan enforcement that each of the 6 pure decision functions is called from at most 2 places in the whole integration (`fan_fsm.py`'s own dispatch, once each, plus at most one named legacy closure in `automation.py`) — the same "sibling threshold drift" guard `test_nat_vent_exit_lockout_coverage.py` provides for nat-vent's exit chain.

**Behavior-preservation proof (historical — the cutover this proved is now permanent):** During the migration, `_fan_fsm_authoritative=False` held for `_engine_a`/production for its whole lifetime (fixed at construction, same pattern the other 3 lifecycles used), so the legacy body of all 16 entry points ran unconditionally and unchanged while only `_engine_b`/shadow ever applied the FSM branch's `to_state`. `tools/sim_harness/fan_fsm_authoritative_compare.py` + `tests/test_fan_fsm_authoritative_compare.py` proved flipping the flag alone was a full behavioral no-op across the shared golden/pending scenario corpus (diffing the entire event_log/action_log, not a derived state label), and `tools/sim_harness/combined_fsm_authoritative_compare.py` + `tests/test_combined_fsm_authoritative_compare.py` extended this to all 4 lifecycles' flags flipped together, confirming no cross-lifecycle interaction effect. Phase 6 (Issues #757–#770) then deleted the flag, the legacy branch, the shadow engine, and all four of those tools/tests — once the FSM was made permanently authoritative there was no more flag to flip and no more no-op left to prove. None of these files exist in the current codebase; do not cite them as running tests.

**Shadow-diagnostic tests (historical — removed in Phase 6):** `tests/test_shadow_engine_sensor.py`/`tests/test_shadow_engine_live.py` used to exercise the diagnostic sensor's `extra_state_attributes` and its debounce mechanics, including the `fan_mirror` axis. Both files, the sensor, and the dual-engine comparison they tested were removed in Phase 6 along with the rest of the shadow-engine infrastructure.

## Code Reference

- [`FanLifecycleState`, `derive_fan_lifecycle_state`](../custom_components/climate_advisor/fan_lifecycle.py) — Phase 1, pure composed-state derivation (Issue #731)
- [`decide_fan_toggle_rate_limit`](../custom_components/climate_advisor/fan_toggle_rate_limit.py) — Phase 2, rapid-cycling backstop
- [`FanFsmEventKind`, `FanFsmInputs`, `FanFsmEvent`, `FanTransition`, `transition`](../custom_components/climate_advisor/fan_fsm.py) — Phase 3, the unified `(state, event) -> Transition` table
- [`AutomationEngine._build_fan_fsm_inputs`, `AutomationEngine.fan_lifecycle_state`, `AutomationEngine._apply_fan_fsm_state`, `AutomationEngine._resolve_fan_fsm_state`](../custom_components/climate_advisor/automation.py) — Phase 4/5, the read/write integration layer; the sole path since Phase 6
- [`tests/test_fan_fsm_single_caller.py`](../tests/test_fan_fsm_single_caller.py) — AST-based single-source-of-truth enforcement for the 6 imported pure decision functions

**Removed in Phase 6 (Issues #757–#770) — no longer present in the codebase, listed here only for historical/archaeology purposes:** `AutomationEngine._fan_fsm_authoritative` (per-engine cutover flag), `ClimateAdvisorCoordinator._update_shadow_engine_diagnostic`'s `fan_mirror` axis, `ClimateAdvisorShadowEngineStatusSensor`, `tools/sim_harness/fan_fsm_authoritative_compare.py` + `tests/test_fan_fsm_authoritative_compare.py`, `tools/sim_harness/combined_fsm_authoritative_compare.py` + `tests/test_combined_fsm_authoritative_compare.py`, and `tests/test_shadow_engine_coverage.py`. All were deleted along with the dual-engine production/shadow shell once the fan/WHF FSM proved permanently reliable.
