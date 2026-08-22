<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [economizer_fsm.py](../custom_components/climate_advisor/economizer_fsm.py) | ↔ [Nat-Vent Lifecycle Spec](nat-vent-lifecycle-spec.md) -->

# Economizer Lifecycle — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What are the 3 economizer session states and how do they map to flags? | `INACTIVE`, `COOL_DOWN`, `MAINTAIN` — derived purely from `_economizer_active`/`_economizer_phase`. `_economizer_active` is fully redundant with `_economizer_phase != "inactive"` in every real write site, so this is one 3-value enum, not a composed multi-axis state. | [State Transitions](#state-transitions) |
| Where is the derivation computed, and is it load-bearing? | `economizer_lifecycle.py::derive_economizer_lifecycle_state()`, exposed read-only via `AutomationEngine.economizer_lifecycle_state`. Load-bearing when `_economizer_fsm_authoritative` is set (supplies the FSM's starting state on every tick); a pure diagnostic view otherwise. | [Scope](#scope) |
| What is the economizer, occupant-facing? | A two-phase window-cooling strategy: with windows already open on a hot day, the fan alone (or fan-assisted AC) can sometimes hold comfort for free instead of running the compressor continuously. Getting the gate wrong either wastes a free-cooling window (AC runs when a fan would suffice) or pulls in outdoor air that isn't actually helping. | [economizer_gate.py module docstring](../custom_components/climate_advisor/economizer_gate.py) |
| Who calls this, and how often? | Exactly one production call site: `coordinator.py`'s 30-min regular cycle (`_should_run_regular_cycle_window_cooling_check()` gate, then `automation_engine.check_window_cooling_opportunity(...)`) — unlike nat-vent/fan's many call sites, this is genuinely single-entry. | [Scope](#scope) |
| Is nat-vent-active a real transition, or a defer? | A defer, not a transition. If `_natural_vent_active` is True, the economizer stands down entirely for that tick but does **not** deactivate — a real, preserved asymmetry from the not-hot-day branch (which does deactivate). Modeled explicitly as `EconomizerTransition.deferred=True` so the FSM-authoritative shell can reproduce production's exact "return False without touching state" behavior. | [State Transitions](#state-transitions) |
| How was this spec's accuracy verified? | Differential replay: `economizer_fsm.transition()`'s FSM-authoritative branch run against the full 90 golden + pending scenario corpus (`tools/sim_harness/economizer_fsm_authoritative_compare.py`) — zero divergent scenarios, no allowlist (unlike nat-vent's Phase 2d, this extraction is a pure 1:1 translation, not a widened check). | [Verification](#verification) |
| Can the economizer FSM actually drive real HVAC/fan hardware yet? | `AutomationEngine._economizer_fsm_authoritative` is fixed at construction (`True` on the FSM engine identity/`_engine_b`, `False` on the legacy one/`_engine_a`), same #729 pattern as the other 6 flags — `switch.climate_advisor_shadow_engine_primary` is the single control axis. | [§ FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) |
| Is there a live shadow-engine comparison for this subsystem? | Yes — `economizer_mirror` (10th shadow-diagnostic axis), comparing `economizer_lifecycle_state` on each engine's own flags. Unlike nat-vent/door-window/override-grace, there is no separate `economizer_fsm` axis (same reasoning as fan/classification/occupancy's own missing sibling axis — see `_SHADOW_DIAG_AXES`'s comment in `coordinator.py`). Because `check_window_cooling_opportunity()` has no shadow-side call, `_economizer_active`/`_economizer_phase` are raw-copied production→shadow every cycle by `_sync_shadow_inputs()` rather than independently recomputed — the differential comparator above is the real regression proof, not this live axis. | [§ FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) |

## Scope

- **Files:** `custom_components/climate_advisor/economizer_lifecycle.py` (pure session-state derivation), `economizer_gate.py` (pure eligibility/phase-selection gate), `economizer_fsm.py` (unified transition table), `automation.py` (`check_window_cooling_opportunity()` shell, `_check_window_cooling_opportunity_fsm()`, `_deactivate_economizer()`, the 7th `_economizer_fsm_authoritative` flag).
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

## Invariants

- `_economizer_active` and `_economizer_phase` are always set together (never independently) by every real write site (`check_window_cooling_opportunity()`'s legacy branch, `_apply_economizer_fsm_state()`, `_deactivate_economizer()`) — confirmed by direct code reading before choosing the single-enum (not composed multi-axis) lifecycle-state shape.
- The economizer never overrides nat-vent; nat-vent taking over is always a same-tick defer, never a forced deactivation.
- The FSM-authoritative branch (`_check_window_cooling_opportunity_fsm()`) must never diverge from the legacy branch while `_economizer_fsm_authoritative=False` — enforced by the full-corpus differential comparator (zero-divergence, no allowlist).

## Verification

- **Unit tests** (pure logic, no HA stubs): `tests/test_economizer_gate.py` (eligibility/phase-selection math), `tests/test_economizer_lifecycle.py` (state derivation), `tests/test_economizer_fsm.py` (transition wiring — short-circuit ordering, changed/unchanged, direction_ok exposure).
- **Decision-equivalence**: `tests/test_economizer_fsm_authoritative_compare.py` — flips `_economizer_fsm_authoritative` True on every engine constructed during a scenario replay and asserts a byte-identical `event_log`/`action_log` against the untouched baseline, across all 90 golden+pending scenarios (zero divergence, zero allowlist), plus a positive control proving the comparator can detect an injected regression in the swapped path.
- **Combined-flip proof**: `tools/sim_harness/combined_fsm_authoritative_compare.py` now flips all 7 `*_fsm_authoritative` flags together (Issue #746 added the 7th) — `tests/test_combined_fsm_authoritative_compare.py` still passes across the full corpus.
- **Flag ownership**: `tests/test_fsm_flag_ownership.py`'s AST-based registry confirms `_apply_economizer_fsm_state()` is the sole `_apply_*_fsm_state()`-shaped writer of `_economizer_active`/`_economizer_phase`.
- **Regression tests unaffected**: the pre-existing `tests/test_economizer.py` (legacy-path behavior) passes unmodified — confirms the legacy branch (`_economizer_fsm_authoritative=False`, the default) is untouched.

## Code Reference

| Function/property | File | Role |
|---|---|---|
| `decide_economizer_transition(inputs)` | `economizer_gate.py` | Pure eligibility + phase-selection leaf. |
| `derive_economizer_lifecycle_state(inputs)` | `economizer_lifecycle.py` | Pure flags → 3-state enum derivation. |
| `transition(current_state, event)` | `economizer_fsm.py` | Unified `(state, event) -> Transition` dispatcher — day_type/nat-vent short-circuits, then the gate. |
| `AutomationEngine.economizer_lifecycle_state` | `automation.py` | Read-only property view of current session state. |
| `AutomationEngine._build_economizer_fsm_inputs(...)` | `automation.py` | Builds `EconomizerFsmInputs` from live engine state + call parameters. |
| `AutomationEngine._apply_economizer_fsm_state(state)` | `automation.py` | Writes `_economizer_active`/`_economizer_phase` from a transition result. |
| `AutomationEngine._check_window_cooling_opportunity_fsm(...)` | `automation.py` | FSM-authoritative shell — same side effects (fan/HVAC/logging) as the legacy branch, driven by `transition()`'s result. |
| `AutomationEngine.check_window_cooling_opportunity(...)` | `automation.py` | Public entry point; dispatches to the FSM shell or the untouched legacy body based on `_economizer_fsm_authoritative`. |
