<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [classification_fsm.py](../custom_components/climate_advisor/classification_fsm.py) | ↔ [Fan/WHF Lifecycle Spec](fan-lifecycle-spec.md) -->

# Classification Decision — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Why is `classification_fsm.py` stateless, unlike the other 4 FSMs? | `apply_classification()` has no genuine multi-tick session state — `_current_classification` is DATA (a fresh `DayClassification` handed in each cycle), not a state machine that persists/evolves across calls. Forcing a state dataclass with no real transitions to model would be dead weight. | [State Model](#state-model) |
| Why only ONE event kind (`CYCLE_EVALUATED`) instead of one per production call site? | All 6 real call sites (`api.py:578`, the grace-expiry re-entry, coordinator's startup coalesce / 30-min cycle / two others) invoke the exact same `apply_classification()` method with no call-site-specific branching inside it. | [Event Kinds](#event-kinds) |
| What does the ODE ceiling guard extraction actually cover? | The ~190-line eligibility/dormancy/breach-scan/lead-time computation inside `apply_classification()` (Issue #136, dormancy fixed Issue #247, archetype-aware threshold Issue #392 Fix 1) — extracted into `ode_ceiling_guard.py`'s `decide_ode_ceiling_guard()`. | [Scope](#scope) |
| When did the classification FSM become sole authority? | Phase 6 (Issues #757–#770) deleted the legacy inline block and the shadow-engine infrastructure. The FSM is now the only decision path for classification apply. | [Verification](#verification) |
| What happened to classification's shadow-diagnostic coverage? | Deprecated and deleted in Phase 6. The `classification_mirror` axis and the entire shadow-engine infrastructure were removed once all 4 FSMs proved reliable in production use. | [Shadow-Diagnostic Coverage](#shadow-diagnostic-coverage-historical) |
| What proves the extraction is behavior-identical? | During the migration, a differential comparator ran the full 90-scenario golden+pending corpus and found zero divergence (Issue #742). That comparator was retired along with the rest of the shadow-engine infrastructure in Phase 6 (Issue #757) — the FSM is now unconditionally the sole implementation, so there is no second path left to compare against. | [Verification](#verification) |

## Scope

- **Files:** `custom_components/climate_advisor/ode_ceiling_guard.py` (pure ODE ceiling guard decision), `custom_components/climate_advisor/classification_fsm.py` (the wiring layer composing `desired_state.decide_scheduled_band_gate()` + `ode_ceiling_guard.decide_ode_ceiling_guard()`), `custom_components/climate_advisor/automation.py` (`_resolve_classification_fsm_state()`, `_apply_ode_ceiling_guard_decision()`, and the unconditional call site inside `apply_classification()`). `coordinator.py`'s per-engine flag and `classification_mirror` shadow-diagnostic axis existed only during the migration and were deleted in Phase 6 (Issue #757).
- **Entry point:** `AutomationEngine.apply_classification()` / `classification_fsm.transition()` (the composed decision function) / `AutomationEngine._resolve_classification_fsm_state()` (the single dispatch point).

**Does NOT cover:**
- `desired_state.decide_scheduled_band_gate()`'s own internal logic (occupancy/override/paused/nat-vent defer priority) — pre-existing, unchanged by this phase, already covered by `test_desired_state.py`.
- The 3 `DEFER_*` branches' real side-effecting bodies (`handle_occupancy_vacation()`, `handle_occupancy_away()`, forcing HVAC off, `_apply_nat_vent_hvac_state()`) — these stay in the `automation.py` shell on both the legacy and FSM-authoritative paths; only the DECISION of which branch to take is what `decide_scheduled_band_gate()` (unchanged) already owned before this phase.
- Nat-vent's own session lifecycle — owned by `nat-vent-lifecycle-spec.md`. This spec only documents the one cross-lifecycle read (`natural_vent_active` as a plain boolean input to both the gate and the ceiling guard).
- Fan/WHF's own lifecycle — owned by `fan-lifecycle-spec.md`. This spec only documents the `whf_owns_hvac`/`fan_mode` reads needed to replicate `apply_classification()`'s DEFER_NAT_VENT short-circuit (Issue #392 Fix 1b).

## State Model

**Deliberately stateless** — there is no `ClassificationLifecycleState` type, unlike the 4 precedent FSMs. `apply_classification()` computes its entire decision fresh from the `classification` argument, the current gate-relevant flags, and the current thermal model snapshot, then returns; nothing it decides this cycle constrains or is remembered by the next cycle. See `classification_fsm.py`'s own module docstring for the full five-whys behind this decision.

`classification_fsm.transition()` therefore takes no `current_state` parameter — its signature is `transition(event: ClassificationFsmEvent) -> ClassificationDecision`, not the `(current_state, event) -> Transition` shape the other 4 FSMs use.

## Event Kinds

`classification_fsm.py`'s `ClassificationFsmEventKind` has exactly 1 member:

| Event kind | Real production call sites | Notes |
|---|---|---|
| `CYCLE_EVALUATED` | `api.py:578`, `automation.py`'s grace-expiry re-entry (`~L6653`), `coordinator.py`'s startup coalesce (`~L3124`), 30-min cycle (`~L3616`), and two more (`~L4606`, `~L6240`) | All 6 sites call the same `apply_classification()` method with no call-site-specific branching inside it — the method itself never asks "who called me," only "what are the current inputs." |

## Decision Composition

`transition()` composes exactly 2 pure pieces, in this order:

1. `desired_state.decide_scheduled_band_gate()` (existing, reused unchanged, Issue #498) — returns `ScheduledBandGate`: `DEFER_OCCUPANCY`, `DEFER_OVERRIDE`, `DEFER_PAUSED`, `DEFER_NAT_VENT`, or `PROCEED`.
2. `ode_ceiling_guard.decide_ode_ceiling_guard()` (Issue #742) — only called when the gate and short-circuit conditions below allow.

### Ceiling-Guard Eligibility

`CeilingGuardEligibility` mirrors `apply_classification()`'s own early-return chain exactly — a pure mirror, not a reinterpretation:

| Gate result | Additional condition | Eligibility | Ceiling guard called? |
|---|---|---|---|
| `DEFER_OCCUPANCY` | — | `NOT_EVALUATED_OCCUPANCY_DEFER` | No |
| `DEFER_PAUSED` | — | `NOT_EVALUATED_PAUSED_DEFER` | No |
| `DEFER_NAT_VENT` | `aggressive_savings=True` | `NOT_EVALUATED_SAVINGS_NAT_VENT` | No |
| `DEFER_NAT_VENT` | `fan_mode` in (WHOLE_HOUSE, BOTH) | `NOT_EVALUATED_WHF_ARCHETYPE` | No |
| `DEFER_NAT_VENT` | `fan_mode` is HVAC or DISABLED, `aggressive_savings=False` | `EVALUATED` | Yes |
| `PROCEED` | — | `EVALUATED` | Yes |

**`DEFER_OVERRIDE` is structurally unreachable** at this call site: the real method's `self._manual_override_active` early-return always fires *before* `decide_scheduled_band_gate()` is ever called, so `DEFER_OVERRIDE` can never be returned here. If it somehow were, it would fall through to the ceiling guard exactly like `PROCEED` — `classification_fsm.py` mirrors this exactly (only 3 explicit short-circuit branches, not 4).

### ODE Ceiling Guard (`ode_ceiling_guard.py`)

`decide_ode_ceiling_guard()` reimplements `apply_classification()`'s ~190-line inline block (automation.py, pre-Issue-#742 `~L2664-2851`) as a pure function. `OdeCeilingGuardOutcome` has 8 members, checked in this priority order:

| Outcome | Condition | Shell action |
|---|---|---|
| `NOT_APPLICABLE` | `predicted_indoor` empty/None, or `classification.hvac_mode != "off"` | None — matches production's outer `if predicted_indoor and hvac_mode == "off":` guard |
| `MODEL_INELIGIBLE` | `k_passive` missing/non-negative, or confidence is `"none"` without bridge, or `comfort_cool` missing | DEBUG log only |
| `MISSING_TEMPS` | `outdoor` or `indoor` unavailable | DEBUG log only |
| `NO_CEILING_THRESHOLD` | Archetype is WHOLE_HOUSE/BOTH (Issue #402) | DEBUG log only — never escalates for this archetype |
| `DORMANT` | Issue #247's 3-condition dormancy test: `outdoor <= indoor` AND `natural_vent_active` AND `indoor <= ceiling_threshold` | DEBUG log only |
| `NO_BREACH_PREDICTED` | Predicted curve never crosses `comfort_cool (+ bridge tolerance)` | DEBUG log only |
| `STANDING_BY` | Breach predicted, but `hours_to_breach > lead_min / 60` | INFO breach log, then DEBUG standing-by log |
| `ESCALATE` | Breach predicted within lead time | INFO breach log, INFO escalation log, deactivates fan if nat-vent was active, sets HVAC to `cool` + `comfort_cool`, emits `nat_vent_ceiling_escalation` (if fan deactivated) and `ceiling_guard_fired` |

`ceiling_threshold` is a caller-resolved input (`AutomationEngine._ceiling_threshold()`), not re-derived by this module — see `ode_ceiling_guard.py`'s own module docstring five-whys for why.

## Wiring

`apply_classification()`'s ceiling-guard section (automation.py) calls the FSM unconditionally. As of Phase 6 (Issues #757–#770), the legacy inline block has been deleted — the FSM is the sole authority.

`_resolve_classification_fsm_state()` builds one `ClassificationFsmInputs` snapshot and calls `classification_fsm.transition()`; `_apply_ode_ceiling_guard_decision()` then reproduces the exact same logging/event/HVAC-write behavior, driven by the returned `OdeCeilingGuardDecision`.

## Shadow-Diagnostic Coverage (historical)

During the migration, a single `classification_mirror` axis (no paired `classification_fsm` axis — `classification_fsm.py` is genuinely stateless, so there was no separate untethered FSM state object to compare production against) tracked live agreement via `_update_shadow_engine_diagnostic()`, exposed on a dedicated shadow-engine status sensor. This entire mechanism — the axis, the comparison, and the sensor — was permanently deleted in Phase 6 graduation (Issue #757) once the classification FSM proved reliable in production; nothing about it remains in the current codebase.

## Verification

- `tests/test_ode_ceiling_guard.py` — exhaustive unit coverage of `decide_ode_ceiling_guard()`'s 8 outcomes and their boundary conditions.
- `tests/test_classification_fsm.py` — wiring correctness between the 2 composed pure pieces.
- During the migration, a full 90-scenario golden+pending corpus differential comparator (Issue #742's primary gate) and a combined-flags comparator (all `*_fsm_authoritative` flags flipped together) both proved zero divergence. Both comparators were retired in Phase 6 (Issue #757) along with the flags they compared — there is no second path left to compare against.

## Code Reference

- [`decide_ode_ceiling_guard`](../custom_components/climate_advisor/ode_ceiling_guard.py) — pure ODE ceiling guard decision
- [`classification_fsm.transition`](../custom_components/climate_advisor/classification_fsm.py) — the composed decision function
- [`AutomationEngine._resolve_classification_fsm_state`](../custom_components/climate_advisor/automation.py) — the single dispatch point
- [`AutomationEngine._apply_ode_ceiling_guard_decision`](../custom_components/climate_advisor/automation.py) — the FSM-authoritative side-effecting shell
- [`AutomationEngine.apply_classification`](../custom_components/climate_advisor/automation.py) — the real entry point, flag-gated call site
