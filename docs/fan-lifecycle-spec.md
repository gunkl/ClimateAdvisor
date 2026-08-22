<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [fan_fsm.py](../custom_components/climate_advisor/fan_fsm.py) | ↔ [Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md) -->

# Fan/WHF Lifecycle — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Why 5 composed axes instead of one flat enum, unlike nat-vent's 4-state enum? | A WHF session can genuinely occupy several independent axes at once (physically on, under manual override, owning HVAC suppression, AND rate-limited against its next toggle, simultaneously) — flattening would either explode combinatorially or silently drop states a status card / future decision needs to read independently. | [State Model](#state-model) |
| What are the 16 event kinds and where does each one's real call site live? | One per real production entry point in `automation.py` — see the table in [Event Kinds](#event-kinds). | [Event Kinds](#event-kinds) |
| Why do 2 of the 16 kinds never move `to_state` away from `from_state`? | `THERMO_BACKSTOP_TICK`/`THERMOSTAT_CHECK_TICK` only inform a downstream routing decision (`_exit_nat_vent()`/`_deactivate_fan()` selection between several possible event types) this FSM's 5 axes cannot represent without duplicating that routing logic — the same "sibling threshold drift" trap Issues #400/#402/#417/#456/#458 already document for this codebase. | [Deliberately Unreachable Kinds](#deliberately-unreachable-kinds) |
| How does fan/WHF's wiring differ structurally from nat-vent/door-window/override-grace? | Those 3 FSMs stand apart from both engines as a third, coordinator-tracked computation. `fan_fsm.py` is wired *inside* `AutomationEngine` itself via a single dispatch chokepoint, `_resolve_fan_fsm_state()`, gated per-engine by `_fan_fsm_authoritative` (fixed at construction, same #729 pattern). | [Wiring](#wiring) |
| Is fan/WHF's shadow-diagnostic coverage a `mirror`+`fsm` pair like the other 3? | No — a single `fan_mirror` axis only. `shadow_automation_engine.fan_lifecycle_state` already IS the FSM-derived state (via `_fan_fsm_authoritative=True`), so a paired `fan_fsm` axis comparing against a third computation would be tautologically identical to `fan_mirror`. | [Shadow-Diagnostic Coverage](#shadow-diagnostic-coverage) |
| How does this FSM read nat-vent's own session-ownership state? | As a plain boolean (`natural_vent_active`), not `NatVentLifecycleState` — `_release_whf_and_reclassify()`'s guard only needs "does a nat-vent session currently claim this fan," which decouples this module from a state shape that has already changed twice (Issues #672, #706). | [Nat-Vent Coupling](#nat-vent-coupling) |
| Are `_activate_fan`/`_deactivate_fan` "mirrored" in the shadow-coverage registry the way nat-vent's entry points are? | No — classified `"internal"`, same as before this issue. `_sync_shadow_inputs()`'s raw-copy mechanism is the only coverage path; there is no second `_mirror_to_shadow()` replay call site for fan, since the shadow engine already runs its own FSM dispatch internally. | [Shadow-Coverage Classification](#shadow-coverage-classification) |
| Does flipping `_fan_fsm_authoritative` change production behavior today? | No — `False` for `_engine_a`/production for its whole lifetime; the legacy body of all 16 entry points runs unconditionally and unchanged. Only `_engine_b`/shadow ever applies the FSM's `to_state`. | [Verification](#verification) |
| Is a single source of truth enforced for the 6 pure decision functions this FSM assembles? | Yes — `tests/test_fan_fsm_single_caller.py` AST-scans the whole integration and requires each of the 6 to be called from at most 2 places: `fan_fsm.py`'s own dispatch, and one named legacy closure in `automation.py`. | [Verification](#verification) |

## Scope

- **Files:** `custom_components/climate_advisor/fan_lifecycle.py` (Phase 1, pure composed-state derivation), `custom_components/climate_advisor/fan_toggle_rate_limit.py` (Phase 2, rapid-cycling backstop, delegated to from `automation.py`'s `_fan_toggle_rate_limited()`), `custom_components/climate_advisor/fan_fsm.py` (Phase 3, the unified `(state, event) -> Transition` table), `custom_components/climate_advisor/automation.py` (Phase 4/5 — `_build_fan_fsm_inputs()`, `fan_lifecycle_state` property, `_apply_fan_fsm_state()`, `_resolve_fan_fsm_state()`, and the 16 real entry points' dispatch wiring), `custom_components/climate_advisor/coordinator.py` (the 4th fixed-per-engine-identity flag and the `fan_mirror` shadow-diagnostic axis).
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

Unlike nat-vent/door-window/override-grace — whose pure FSM modules stand apart from both engines as a third, coordinator-tracked computation compared against each engine's own legacy-derived state — `fan_fsm.py` is wired **inside** `AutomationEngine` itself, through a single dispatch chokepoint: `_resolve_fan_fsm_state(*, kind, legacy, origin_state=None, **input_overrides)`, called from all 16 real entry points.

- **`_fan_fsm_authoritative`** (per-engine boolean, fixed at construction, never toggled at runtime — same #729 "engine identity, not a per-subsystem switch" pattern the other 3 lifecycles converged to): `False` for `_engine_a`/production, `True` for `_engine_b`/shadow.
- **When `False`** (production): `_resolve_fan_fsm_state()` calls the caller-supplied `legacy` closure — the pre-extraction inline code, wrapped into the same `FanTransition` shape the FSM branch would have returned, so both branches answer with the same shape regardless of which one decided. The FSM's own `transition()` still runs internally for every caller-instrumented call site (visible in `automation.py`'s "Issue #731 Phase 5" comments preceding each dispatch), but purely as an audit-trail computation — never applied.
- **When `True`** (shadow): `_resolve_fan_fsm_state()` calls `fan_fsm.transition()` for real and applies its `to_state` via `_apply_fan_fsm_state()`, which writes back `_fan_active`/`_fan_override_active`/`_fan_min_runtime_active`/`_pre_fan_hvac_mode` (None-clear only) — the inverse of `fan_lifecycle_state`'s own derivation, for exactly the 4 fields that derivation composes. Every other fan/WHF-adjacent field (`_fan_on_since`, `_fan_remote_timer_hours`, `_fan_toggle_command_time`, `_fan_drift_tick_count`, etc.) stays owned by the real side-effecting call site, same "outside the N-field derivation stays a direct per-caller write" rule `_apply_override_grace_fsm_state()`'s own docstring documents for its own composed-state application.
- **Genuinely mutually exclusive**: exactly one of the FSM branch or `legacy()` ever writes the 4 composed-state fields for a given call — never both — same discipline `_resolve_override_grace_fsm_state()` establishes for itself, for the identical reason (an unconditional `legacy()` call "for the real side effects" would silently make the switch behaviorally inert).
- **`_resolve_fan_fsm_state()` is also the single emit point for `WHF_HVAC_SUPPRESSED`/`WHF_HVAC_RELEASED`** on the FSM branch — a before/after diff of `_whf_owns_hvac()` around the `transition()` call, absorbing the responsibility the now-deleted `_resolve_whf_hvac_suppression()` chokepoint used to own. Every real `legacy` closure for `WHF_SUPPRESSION_REQUESTED`/`WHF_RELEASE_REQUESTED` reproduces the same before/after-diff emit verbatim inline, so this dispatcher does not emit again on the legacy branch (that would double-fire).

Both engines expose the identical read-only `fan_lifecycle_state` property (`derive_fan_lifecycle_state()` over each engine's own live flags) regardless of which code path produced those flags — it is always safe to read either engine's current composed state.

## Nat-Vent Coupling

`FanFsmInputs.natural_vent_active` is a **plain boolean**, not nat-vent's own `NatVentLifecycleState` object (Issue #530). Several real fan-subsystem methods (`_release_whf_and_reclassify()`'s guard, most notably) read `self._natural_vent_active` as a simple flag-ownership check — "does the nat-vent session still own this fan" — not as a state-composition input the way `paused_by_door`/`natural_vent_active` feed `door_window_fsm.py`. Reading a plain bool here, rather than importing `nat_vent_lifecycle.NatVentLifecycleState` and pattern-matching on it, keeps this module decoupled from nat-vent's own state shape — which has already changed twice (Issue #672, Issue #706) without this module needing to know. A future `FanFsmInputs` field could upgrade to the richer type if a decision genuinely needs more than "is a session currently claiming this fan," but no branch in this phase does.

This is the fan/WHF subsystem's only direct cross-lifecycle read. It does not go through `lifecycle_dispatcher.py`'s pub/sub event channel (unlike the door/window ↔ nat-vent coupling `nat-vent-lifecycle-spec.md`'s "First live disagreement" incident describes) — `_build_fan_fsm_inputs()` reads `self._natural_vent_active` directly off the engine, same canonical-attribute convention the other 3 lifecycle builders already follow (Issue #717's revert rationale: a same-instance emit/consume round-trip can never actually go stale relative to the canonical attribute, so routing through a dispatcher-synced mirror buys nothing).

## Shadow-Diagnostic Coverage

`coordinator.py`'s `_update_shadow_engine_diagnostic()` — the wall-clock-debounced production/shadow comparison already tracking `mirror`/`fsm` (nat-vent), `door_window_mirror`/`door_window_fsm`, and `override_grace_mirror`/`override_grace_fsm` — gained a 7th axis, **`fan_mirror`**, comparing `self.automation_engine.fan_lifecycle_state` against `self.shadow_automation_engine.fan_lifecycle_state`, joined into a single `"physical/override/cycling/hvac_ownership/rate_limit"` string (the same joint-string convention `override_grace`'s own `"confirm/grace"` state already uses).

**Deliberately no paired `fan_fsm` axis.** For the other 3 lifecycles, the second axis compares production against a free-standing third FSM computation the coordinator tracks itself (`self._nat_vent_fsm_state`, `self._door_window_fsm_state`, `self._override_grace_fsm_state`) — a genuinely independent third opinion, because those pure FSM modules were never wired into either engine. Fan/WHF has no such free-standing computation to compare against: `_engine_b`'s own `fan_lifecycle_state` **is** the FSM-derived state already, via `_fan_fsm_authoritative=True` (see [Wiring](#wiring) above). A hypothetical `fan_fsm` axis would always read identically to `fan_mirror` — both would be comparing production against the exact same shadow-engine-computed value — carrying zero independent signal, the same "no functional consumer" trap CLAUDE.md's `KNOWN_FIXES` `scope_not_covered` removal (Issue #563) warns about for diagnostic surfaces that look complete but never actually diverge from a sibling field.

`sensor.climate_advisor_shadow_engine_status`'s `extra_state_attributes` exposes `fan_production_state`/`fan_shadow_state`/`fan_mirror_agrees` alongside the existing 3 lifecycles' fields, plus a `debounce.fan_mirror` sub-dict matching the existing per-axis shape (`disagreement_seconds`/`sustained`/`cumulative_seconds_today`). A sustained disagreement (past `SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S`) logs at `WARNING`, same as the other 6 axes.

## Shadow-Coverage Classification

`tests/test_shadow_engine_coverage.py`'s AST-based `_TRACKED_FIELDS` registry — which enforces that every method directly assigning a tracked lifecycle field is classified `"mirrored"` / `"internal"` / `"exempted: <reason>"` — keeps `_activate_fan`/`_deactivate_fan` classified `"internal"`, unchanged by this issue. Neither method gained a new `_mirror_to_shadow()` replay call site, and none is needed: both are already called from *inside* the shadow engine's own `_resolve_fan_fsm_state()` dispatch (reached via `_fan_fsm_authoritative=True` on `_engine_b`), so a coordinator-level replay would be redundant — the shadow engine already runs its own real fan/WHF decision logic on every one of its own 16 entry-point calls, it does not need production's decisions replayed onto it a second time the way nat-vent's `_natural_vent_active`/`_paused_by_door` genuinely do (those flags on the shadow are set ONLY by mirrored replays or the `_sync_shadow_inputs()` raw copy — there is no engine-internal FSM already computing them independently).

`_sync_shadow_inputs()`'s raw-copy mechanism remains the coverage path for whatever cross-lifecycle inputs the shadow engine's own fan FSM computation needs that aren't produced by its own prior fan-entry-point calls (e.g. `_pre_fan_hvac_mode`, already raw-copied since Issue #724 for the door/window cross-read — see `nat-vent-lifecycle-spec.md`'s own write-up of that fix).

## Verification

Per this project's Accuracy Verification convention: `fan_fsm.py`'s own module docstring documents the 16-event-kind/2-group split and both intentionally-unreachable kinds in full; this spec's tables were cross-checked against that docstring and against `automation.py`'s "Issue #731 Phase 5" comments at each of the 16 real dispatch call sites (all confirmed present by direct grep as of this spec's authoring).

**Wiring-correctness tests** (not decision-logic re-proofs — each of the 6 imported pure functions already has its own exhaustive unit coverage, out of scope here per `fan_fsm.py`'s own docstring):
- `tests/test_fan_lifecycle.py` — `derive_fan_lifecycle_state()`'s per-axis derivation rules, including the drift-tick-count-nonzero-implies-ON safety invariant.
- `tests/test_fan_toggle_rate_limit.py` — the Phase 2 rate-limit backstop `automation.py`'s `_fan_toggle_rate_limited()` delegates to.
- `tests/test_fan_fsm.py` — `fan_fsm.transition()`'s own dispatch-on-event-kind wiring for all 16 kinds, both groups' effective-inputs substitution rules, and the 2 deliberately-unreachable kinds' `to_state == from_state` invariant.
- `tests/test_fan_fsm_single_caller.py` — AST-scan enforcement that each of the 6 pure decision functions is called from at most 2 places in the whole integration (`fan_fsm.py`'s own dispatch, once each, plus at most one named legacy closure in `automation.py`) — the same "sibling threshold drift" guard `test_nat_vent_exit_lockout_coverage.py` provides for nat-vent's exit chain.

**Behavior-preservation proof (production never changes):** `_fan_fsm_authoritative=False` for `_engine_a`/production for its whole lifetime (fixed at construction, same as the other 3 lifecycles' pattern) — the legacy body of all 16 entry points runs unconditionally and unchanged; only `_engine_b`/shadow ever applies the FSM branch's `to_state`. `tools/sim_harness/fan_fsm_authoritative_compare.py` + `tests/test_fan_fsm_authoritative_compare.py` prove flipping the flag alone is a full behavioral no-op across the shared golden/pending scenario corpus (diffing the entire event_log/action_log, not a derived state label — same standard `nat_vent_fsm_authoritative_compare.py` set for nat-vent). `tools/sim_harness/combined_fsm_authoritative_compare.py` + `tests/test_combined_fsm_authoritative_compare.py` extend this to all 4 lifecycles' flags flipped together, confirming no cross-lifecycle interaction effect.

**Shadow-diagnostic tests:** `tests/test_shadow_engine_sensor.py`/`tests/test_shadow_engine_live.py` exercise the sensor's `extra_state_attributes` and the `_shadow_diag_update_axis()` debounce mechanics respectively; neither asserts an exhaustive/closed set of axis keys, so the new `fan_mirror` axis is additive and required no golden-attribute-set update.

## Code Reference

- [`FanLifecycleState`, `derive_fan_lifecycle_state`](../custom_components/climate_advisor/fan_lifecycle.py) — Phase 1, pure composed-state derivation (Issue #731)
- [`decide_fan_toggle_rate_limit`](../custom_components/climate_advisor/fan_toggle_rate_limit.py) — Phase 2, rapid-cycling backstop
- [`FanFsmEventKind`, `FanFsmInputs`, `FanFsmEvent`, `FanTransition`, `transition`](../custom_components/climate_advisor/fan_fsm.py) — Phase 3, the unified `(state, event) -> Transition` table
- [`AutomationEngine._build_fan_fsm_inputs`, `AutomationEngine.fan_lifecycle_state`, `AutomationEngine._apply_fan_fsm_state`, `AutomationEngine._resolve_fan_fsm_state`](../custom_components/climate_advisor/automation.py) — Phase 4, the read/write integration layer
- [`AutomationEngine._fan_fsm_authoritative`](../custom_components/climate_advisor/automation.py) — per-engine cutover flag, fixed at construction (`False` for `_engine_a`/production, `True` for `_engine_b`/shadow) — see `coordinator.py`'s `__init__`
- [`ClimateAdvisorCoordinator._update_shadow_engine_diagnostic`](../custom_components/climate_advisor/coordinator.py) — the `fan_mirror` shadow-diagnostic axis
- [`ClimateAdvisorShadowEngineStatusSensor`](../custom_components/climate_advisor/sensor.py) — diagnostic sensor, `fan_production_state`/`fan_shadow_state`/`fan_mirror_agrees` attributes
- [`fan_fsm_authoritative_compare.py`](../tools/sim_harness/fan_fsm_authoritative_compare.py), [`tests/test_fan_fsm_authoritative_compare.py`](../tests/test_fan_fsm_authoritative_compare.py) — full-corpus decision-equivalence proof that flipping the flag is a behavioral no-op
- [`combined_fsm_authoritative_compare.py`](../tools/sim_harness/combined_fsm_authoritative_compare.py), [`tests/test_combined_fsm_authoritative_compare.py`](../tests/test_combined_fsm_authoritative_compare.py) — all 4 lifecycles' flags flipped together
- [`tests/test_fan_fsm_single_caller.py`](../tests/test_fan_fsm_single_caller.py) — AST-based single-source-of-truth enforcement for the 6 imported pure decision functions
- [`tests/test_shadow_engine_coverage.py`](../tests/test_shadow_engine_coverage.py) — `_activate_fan`/`_deactivate_fan` classified `"internal"`, unchanged by this issue
