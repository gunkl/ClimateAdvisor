<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [nat_vent_lifecycle.py](../custom_components/climate_advisor/nat_vent_lifecycle.py) | ↔ [Grace Periods Spec](grace-periods-spec.md) -->

# Nat-Vent Lifecycle — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What are the 4 nat-vent session states and how do they map to flags? | `INACTIVE`, `ACTIVE_FULL_GATE`, `ACTIVE_SOFT_START`, `PAUSED_REACTIVATION_LOCKOUT` — derived purely from `_natural_vent_active`/`_nat_vent_soft_start`/`_paused_by_door`/`_nat_vent_outdoor_exit_time`. | [State Transitions](#state-transitions) |
| Where is the derivation computed, and is it load-bearing? | `nat_vent_lifecycle.py::derive_nat_vent_lifecycle_state()`, exposed read-only via `AutomationEngine.nat_vent_lifecycle_state`. Purely observational — not called from any production decision path. | [Scope](#scope) |
| Does every nat-vent exit hand off through the same choke point? | No — `_exit_nat_vent()` is the choke point for most exits (9 call sites), but the comfort-floor exit inside `check_natural_vent_conditions()`, the away-mode ceiling exit, the ODE ceiling-escalation exit, and two reconcile/bedtime paths mutate the flags directly instead. | [Exit Paths](#exit-paths-not-unified-through-a-single-function) |
| What is the reactivation lockout and how long is it? | 300s default (`NAT_VENT_REACTIVATION_LOCKOUT_S`), only armed by the outdoor-warm-rise exit (the only exit that sets `_nat_vent_outdoor_exit_time`). | [State Transitions](#state-transitions) |
| What happens when nat-vent exits and the sensor is still open — pause or grace? | `_exit_nat_vent()` forks: sensor still open → hands off into the pause lifecycle; sensors closed → hands off into the grace lifecycle. See `grace-periods-spec.md` for what happens next in either lifecycle. | [Handoff to Pause/Grace](#handoff-to-pausegrace) |
| How was this spec's accuracy verified? | Differential replay: `derive_nat_vent_lifecycle_state()` run against the real final engine flags from all 74 golden + 4 pending scenarios (Issue #606), plus 3 hand-reasoned ground-truth scenarios. | [Verification](#verification) |
| Does `check_natural_vent_conditions()`'s exit chain always decide WHY a session ends? | No — confirmed by direct experiment (Issue #608): `nat_vent_temperature_check()` and `fan_thermostat_check()` (both dispatched from the same `temp_update`/thermostat-attribute-change trigger, both already pure-extracted by Issue #441) run FIRST and independently implement equivalent comfort-floor and outdoor-rise-style stops — they win the race and exit the session before `check_natural_vent_conditions()`'s chain ever evaluates, for every golden scenario tested. | [Known Duplicate-Logic Race](#known-duplicate-logic-race-issue-608-finding) |

## Scope

- **Files:** `custom_components/climate_advisor/nat_vent_lifecycle.py` (new, pure derivation), `custom_components/climate_advisor/automation.py` (session flags + all transition sites, `~L700-5400`), `custom_components/climate_advisor/nat_vent_gate.py` (entry gate predicates), `custom_components/climate_advisor/nat_vent_reactivation_lockout.py` (lockout predicate).
- **Entry point:** `AutomationEngine.nat_vent_lifecycle_state` (property) / `derive_nat_vent_lifecycle_state()` (pure function).

**Does NOT cover:**
- The reactivation *gate* itself (should nat-vent (re)activate right now, given current temps) — see `nat_vent_gate.py`'s `decide_nat_vent_gate()`/`decide_nat_vent_soft_start_gate()`.
- The exit *priority chain* (which of the 5+ exit conditions fires first) — still inline in `automation.py` as of this spec; extracting it as a pure, swapped-in function is the next phase of the Block 5 arc (epic #594), not covered here.
- Pause, override, and grace lifecycles' own internal state machines — see `grace-periods-spec.md`. This spec only documents the one real handoff seam between nat-vent and those three.
- Economizer (`_economizer_active`/`_economizer_phase`) — a separate, mutually exclusive mechanism (explicitly gated off whenever `_natural_vent_active` is True), not a nat-vent sub-state.

## State Transitions

**States:**

| State | Flags | Meaning |
|---|---|---|
| `INACTIVE` | `_natural_vent_active=False` | No session running. May coexist with `_paused_by_door=True` from a pause unrelated to nat-vent, or a lockout that has already elapsed. |
| `ACTIVE_FULL_GATE` | `_natural_vent_active=True`, `_nat_vent_soft_start=False` | Full bulk-cooling session — outdoor meaningfully below indoor. |
| `ACTIVE_SOFT_START` | `_natural_vent_active=True`, `_nat_vent_soft_start=True` | Purge/comfort qualifier session (Issue #540) — outdoor at/below indoor parity, today confirmed past peak and declining. |
| `PAUSED_REACTIVATION_LOCKOUT` | `_natural_vent_active=False`, `_paused_by_door=True`, `_nat_vent_outdoor_exit_time` set and within the lockout window | Session ended via the outdoor-rise exit specifically; reactivation is locked out for `NAT_VENT_REACTIVATION_LOCKOUT_S` (default 300s) to prevent flapping. |

**Selected entry transitions** (not exhaustive — see [Exit Paths](#exit-paths-not-unified-through-a-single-function) below for why a complete site-by-site table is deferred):

| From | Trigger | To | Guard | automation.py |
|---|---|---|---|---|
| `INACTIVE` | Door/window opens, full gate passes | `ACTIVE_FULL_GATE` | `decide_nat_vent_gate()` | `~L2841` (`handle_door_window_open`) |
| `INACTIVE` | Door/window opens, only soft-start gate passes | `ACTIVE_SOFT_START` | `decide_nat_vent_soft_start_gate()` | `~L3093-3094` |
| `INACTIVE` / `PAUSED_REACTIVATION_LOCKOUT` | Idle-open re-eval, full gate passes | `ACTIVE_FULL_GATE` | `_nat_vent_may_reactivate()` (includes lockout check) | `~L3053`, `~L3381` |
| `INACTIVE` / `PAUSED_REACTIVATION_LOCKOUT` | Idle-open re-eval, soft-start only | `ACTIVE_SOFT_START` | same, soft-start variant | `~L3412-3413` |
| `ACTIVE_SOFT_START` | Full gate independently clears | `ACTIVE_FULL_GATE` | `_nat_vent_may_reactivate()` | `~L3125` — label-only, fan/HVAC untouched |
| `INACTIVE` | Fan already physically running at startup/reconcile, eligible | `ACTIVE_FULL_GATE` | `reconcile_fan_on_startup()` eligibility check | `~L3955` |

**Exit transitions via the `_exit_nat_vent()` choke point** (`automation.py:5098-5138`, Issue #411/#417/#418): 9 call sites (`~L1198, 2917, 3287, 3311, 3328, 3493, 3547, 3744, 4040`), covering door/window-closed, outdoor-rise, ceiling-threshold, and RF-timer-boundary exits. Always sets `_natural_vent_active=False, _nat_vent_soft_start=False`; only the outdoor-rise-exit caller passes `set_outdoor_exit_time=True`, which is the sole path that can produce `PAUSED_REACTIVATION_LOCKOUT`.

**Exit transitions that bypass `_exit_nat_vent()` entirely** — go directly to `INACTIVE`, never touch `_paused_by_door`/`_nat_vent_outdoor_exit_time`:

| Exit | automation.py | Why it bypasses the choke point |
|---|---|---|
| Comfort-floor exit (`check_natural_vent_conditions()`) | `~L3165` | Hand-rolled inline exit, predates or was never migrated to `_exit_nat_vent()` — confirmed by direct read, corrected from an earlier (inaccurate) assumption that ALL exits route through it. Its own docstring comment says "Do NOT enter pause — the house needs to warm up." |
| Away-mode ceiling exit | `~L3217-3218` | `_exit_nat_vent()`'s own docstring explicitly excludes this: "different concept, no pause/grace state machine." |
| ODE ceiling-escalation exit | `~L2080-2081` | Escalates to AC directly on the classification cycle; a separate call path. |
| Reconcile "fan confirmed off" / RF-boundary settle | `~L1287-1288`, `~L3796-3797`, `~L3894-3895` | Defensive/observational clears during reconcile, not a decision-driven exit. |
| Bedtime handler, no-classification / normal paths | `~L4701-4702`, `~L4728-4729` | Bedtime-specific shutdown, gate ≠ `DEFER_NAT_VENT`. |

## Exit Paths Not Unified Through a Single Function

Unlike the entry gate (`decide_nat_vent_gate()`, unified across 5 sites — Issue #411 Pass 4) and the reactivation lockout (`is_reactivation_locked_out()`, one call site), the exit side of the lifecycle is **not** currently a single pure function — `_exit_nat_vent()` is a shared *choke point* for most exits, but several exit paths (away-ceiling, ODE-escalation, reconcile/bedtime) mutate the flags directly and were confirmed via direct code reading, not assumed.

**Update (Issue #608):** `check_natural_vent_conditions()`'s own 5-check priority-ordered chain (comfort-floor, away-ceiling, proactive-floor, outdoor-rise, ceiling-threshold) is now extracted into a pure, tested `nat_vent_exit.decide_nat_vent_exit()` and wired into production, following Issue #441's proven extract → differential-validate → swap-in methodology. See [Known Duplicate-Logic Race](#known-duplicate-logic-race-issue-608-finding) below for an important caveat discovered during that extraction: this chain is not the only place these exit conditions are evaluated, and for the temp_update-triggered dispatch path, it usually isn't even the first.

## Known Duplicate-Logic Race (Issue #608 finding)

While differentially validating the `check_natural_vent_conditions()` exit-chain extraction (positive-control testing: deliberately corrupt one condition, confirm a golden scenario that should exercise it diverges), an unexpected result surfaced: **corrupting the comfort-floor and outdoor-rise conditions in `decide_nat_vent_exit()` produced zero divergence in every relevant golden scenario.** Investigation (not assumption) traced the cause to `tools/sim_harness/run_production.py`'s `_handle_temp_update()`, which mirrors real production's own `_async_thermostat_changed()` dispatch order:

1. `nat_vent_temperature_check()` (if nat-vent active) — has its **own independent** comfort-floor stop ("nat-vent hard floor exit [daytime/sleep]"), using `resolve_hard_exit_floor()` (the same pure function `decide_nat_vent_exit()` also calls) but reached via a completely separate call path.
2. `fan_thermostat_check()` (if any CA fan active) — has its **own independent** outdoor-rise-style stop (Check 1, "free-cooling-direction stop," per `fan_thermostat_decision.py`'s own docstring: "Check 1's direction-reversal stop deliberately uses NO hysteresis" — the exact same boundary condition as `decide_nat_vent_exit()`'s `OUTDOOR_RISE` check).
3. `check_natural_vent_conditions()` — dispatched last, in both the harness and real production's `_async_thermostat_changed()`.

Both #1 and #2 already exit the session (clearing `_natural_vent_active`) before #3 ever runs, for any indoor-temperature-change-triggered tick. `check_natural_vent_conditions()`'s chain is genuinely reachable — it is the one place these conditions are evaluated on the **periodic 30-minute `_async_update_data` cycle**, independent of any thermostat attribute change (e.g. an outdoor-only weather-poll update with no indoor temperature change) — but for the specific golden-scenario corpus today, which is dominated by `temp_update` events carrying both indoor and outdoor readings together, #1/#2 win the race essentially every time.

**This is the same duplicate-threshold-logic pattern already tracked in project memory** (nat-vent gate logic previously drifted across 2-3 parallel call sites — Issues #400/#402) — now confirmed to also apply to the exit side, across three separate functions (`nat_vent_temperature_check()`, `fan_thermostat_check()`, `check_natural_vent_conditions()`) rather than one. **Not fixed in Issue #608** — that issue's scope was extracting and validating `check_natural_vent_conditions()`'s own chain, which is now genuinely pure/tested/behavior-preserving (proven via full-suite + golden regression, catching one real latent bug — an `UnboundLocalError` when `indoor` was only computed inside the active-session branch). Consolidating all three call sites onto one shared decision — the way the entry gate was consolidated in #411 — is flagged here as valuable follow-up work, not undertaken in this pass.

## Handoff to Pause/Grace

`_exit_nat_vent()` forks based on `_any_monitored_sensor_open()`:
- **Sensor still open** → `_paused_by_door=True`, `_pre_pause_mode` captured — hands off into the **pause** lifecycle, owned by `grace-periods-spec.md`'s `PAUSED` state.
- **Sensors closed** → `_start_grace_period("automation", trigger="nat_vent_exit_resume")` — hands off into the **grace** lifecycle, owned by `grace-periods-spec.md`'s `GRACE` state.

The reverse edge (grace/pause → nat-vent reactivation) is documented from `grace-periods-spec.md`'s own side; see its `GRACE` row: *"New door/window open (outdoor cool enough for nat-vent) → NAT_VENT... falls through grace guard to nat-vent path."*

## Invariants

1. Away-mode ceiling exit never routes through `_exit_nat_vent()` — confirmed by that function's own docstring and by direct code reading at `automation.py:~3217-3218`.
2. The soft-start → full-gate upgrade (`~L3125`) never restarts the fan or HVAC band — only the `_nat_vent_soft_start` label flips; the session (fan running, HVAC suppressed) is untouched.
3. `_nat_vent_outdoor_exit_time` is set by exactly one exit reason (outdoor-warm-rise, via `_exit_nat_vent(set_outdoor_exit_time=True)`) — no other exit path arms the reactivation lockout.
4. `derive_nat_vent_lifecycle_state()` is purely a function of its 4 explicit inputs — it has no dependency on which of the (currently uncataloged-in-full) real call sites produced those flag values, so its correctness does not depend on the exit-path enumeration above being complete.

## Verification

Per this project's Accuracy Verification convention: this spec's transition table was cross-checked against `automation.py` via direct grep + read of every `_natural_vent_active =`/`_nat_vent_soft_start =`/`_exit_nat_vent(` occurrence (line numbers above are exact as of Issue #606; they will drift with future edits — treat them as a navigation aid, not a contract; if a line doesn't match, search for the function name).

The state **derivation itself** (as opposed to the prose describing individual transition sites) has stronger, automated verification: `tests/test_nat_vent_lifecycle_state.py` runs `derive_nat_vent_lifecycle_state()` against the real final engine flags from all 74 golden + 4 pending scenarios after a full production replay, asserting internal-consistency invariants hold for every one, plus 3 independently hand-reasoned ground-truth scenarios (`mild_all_day_nat_vent_only`, `nat-vent-comfort-floor-exit-restores-heat`, `nat-vent-outdoor-rises-above-indoor-exit`) with expected states worked out by reading each scenario's own events/assertions/verdict, not by inspecting this module's code. All 90 tests pass. No golden/pending scenario currently exercises `ACTIVE_SOFT_START` end-to-end — noted as a coverage gap, not silently absent.

**The exit chain (`decide_nat_vent_exit()`, Issue #608)** is verified two ways, deliberately not just one: (1) 22 direct unit tests covering all 5 exit reasons, their priority order, and boundary conditions — including a revert-test (temporarily invert one condition, confirm the matching unit tests fail, then restore) proving the function is genuinely load-bearing at the unit level. (2) The swap-in itself (replacing the inline chain with a call to this function inside `check_natural_vent_conditions()`) is proven behavior-preserving by the **full** test suite (3976 tests) + golden (74/74) + pending (4/4) all passing unchanged — and this process caught one real latent bug (an `UnboundLocalError` on `indoor` when the exit-chain block was reached with `_natural_vent_active=False`), which the revert-test-driven fix resolved. **A golden-scenario-level positive control for the swap specifically was attempted and found unreliable** — see [Known Duplicate-Logic Race](#known-duplicate-logic-race-issue-608-finding) — because `nat_vent_temperature_check()`/`fan_thermostat_check()` intercept the same conditions first for every scenario tried. The unit-level revert-test is the load-bearing proof for this extraction instead.

## Code Reference

- [`derive_nat_vent_lifecycle_state`](../custom_components/climate_advisor/nat_vent_lifecycle.py) — pure derivation
- [`AutomationEngine.nat_vent_lifecycle_state`](../custom_components/climate_advisor/automation.py) — read-only property
- [`decide_nat_vent_exit`](../custom_components/climate_advisor/nat_vent_exit.py) — pure exit-chain decision (Issue #608)
- [`_exit_nat_vent`](../custom_components/climate_advisor/automation.py#L5098) — the primary (not sole) exit choke point
- [`decide_nat_vent_gate`, `decide_nat_vent_soft_start_gate`](../custom_components/climate_advisor/nat_vent_gate.py) — entry gates
- [`is_reactivation_locked_out`](../custom_components/climate_advisor/nat_vent_reactivation_lockout.py) — lockout predicate
