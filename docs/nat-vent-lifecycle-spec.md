<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [nat_vent_lifecycle.py](../custom_components/climate_advisor/nat_vent_lifecycle.py) | ↔ [Grace Periods Spec](grace-periods-spec.md) -->

# Nat-Vent Lifecycle — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What are the 4 nat-vent session states and how do they map to flags? | `INACTIVE`, `ACTIVE_FULL_GATE`, `ACTIVE_SOFT_START`, `PAUSED_REACTIVATION_LOCKOUT` — derived purely from `_natural_vent_active`/`_nat_vent_soft_start`/`_paused_by_door`/`_nat_vent_outdoor_exit_time`. | [State Transitions](#state-transitions) |
| Where is the derivation computed, and is it load-bearing? | `nat_vent_lifecycle.py::derive_nat_vent_lifecycle_state()`, exposed read-only via `AutomationEngine.nat_vent_lifecycle_state`. Purely observational — not called from any production decision path. | [Scope](#scope) |
| Does every nat-vent exit hand off through the same choke point? | No — `_exit_nat_vent()` is the choke point for most exits, but several paths (comfort-floor, away-ceiling, ODE-escalation, reconcile/bedtime) bypass it. | [Exit Paths](#exit-paths-not-unified-through-a-single-function) |
| What is the reactivation lockout and how long is it? | 300s default (`NAT_VENT_REACTIVATION_LOCKOUT_S`). Originally armed only by the outdoor-rise exit; Issues #696/#755/#739 each closed a gap where a sibling exit shared the same disproven "self-complementary at a fixed reading" reasoning and failed to arm it. | [State Transitions](#state-transitions) |
| What happens when nat-vent exits and the sensor is still open — pause or grace? | `_exit_nat_vent()` forks: sensor still open → hands off into the pause lifecycle; sensors closed → hands off into the grace lifecycle. See `grace-periods-spec.md` for what happens next in either lifecycle. | [Handoff to Pause/Grace](#handoff-to-pausegrace) |
| How was this spec's accuracy verified? | Differential replay: `derive_nat_vent_lifecycle_state()` run against the real final engine flags from all 74 golden + 4 pending scenarios (Issue #606), plus 3 hand-reasoned ground-truth scenarios. | [Verification](#verification) |
| Is every `_exit_nat_vent()` call site's lockout-arming decision enforced, not just documented? | Yes — `tests/test_nat_vent_exit_lockout_coverage.py` AST-scans every call site and requires each to be explicitly classified `"arms lockout"` / `"exempted: <reason>"`, with a positive control that checks the claim against the actual `set_outdoor_exit_time=True` argument (Issue #641). | [Incident: WHF fast-cycling](#incident-whf-fast-cycling-proactive-floor-exit-vs-instant-reactivation-issue-641) |
| Is there a hard floor on how fast CA can physically toggle the fan, independent of any exit/reactivation logic? | Yes — `AutomationEngine._fan_toggle_rate_limited()` enforces `FAN_MIN_TOGGLE_INTERVAL_S` (300s) as a defense-in-depth backstop. (See [Follow-up: rate-limit reporting](#follow-up-rate-limit-reporting-was-misleading-and-mis-framed-as-an-incident-issue-649) for details). | [Follow-up: rate-limit reporting](#follow-up-rate-limit-reporting-was-misleading-and-mis-framed-as-an-incident-issue-649) |
| Does `check_natural_vent_conditions()`'s exit chain always decide WHY a session ends? | No — confirmed by direct experiment (Issue #608): `nat_vent_temperature_check()` and `fan_thermostat_check()` (both dispatched from the same `temp_update`/thermostat-attribute-change trigger, both already pure-extracted by Issue #441) run FIRST and independently implement equivalent comfort-floor and outdoor-rise-style stops — they win the race and exit the session before `check_natural_vent_conditions()`'s chain ever evaluates, for every golden scenario tested. **Update (Issue #737):** `fan_thermostat_check()`'s stop is no longer a hand-duplicated inline check (Issue #435 unified it via `decide_fan_thermostat_check()`). `nat_vent_temperature_check()`'s legacy branch was the last genuine hand-rolled duplicate — it was deleted in Phase 6 (Issues #757–#770); the FSM path it duplicated is now the sole branch and the one every real install runs today. See the Known Duplicate-Logic Race section's update note. | [Known Duplicate-Logic Race](#known-duplicate-logic-race-issue-608-finding) |
| Did an offline shadow engine prove the nat-vent FSM safe to construct? | Yes — `tools/sim_harness/shadow_engine_pair.py` (Issue #611) proved that an offline `dry_run=True` instance never contaminated production across 60 scenarios. This validation informed the Phase 6 decision to make the FSM permanent authority. | [Offline Whole-Engine Shadow-Pair Validation — Historical Context](#offline-whole-engine-shadow-pair-validation-issue-611-subtask-o) |
| What happened to the live shadow engine and diagnostic sensor? | Both retired in Phase 6 (Issues #757–#770) once the nat-vent FSM, along with all other FSM subsystems, proved reliable through weeks of production use. The shadow-engine infrastructure (`coordinator.shadow_automation_engine`, `switch.climate_advisor_shadow_engine_primary`, diagnostic comparisons) was fully deleted; `lifecycle_dispatcher.py` provides the audit trail instead. | [Offline Whole-Engine Shadow-Pair Validation — Historical Context](#offline-whole-engine-shadow-pair-validation-issue-611-subtask-o) |
| When did the nat-vent FSM become sole, permanent authority? | Issue #729 (Phase 6 prep) fixed `_natvent_fsm_authoritative` at construction to `True` on all engines. Phase 6 (Issues #757–#770) deleted the legacy inline branches and the shadow toggle entirely. The FSM is now the only decision path for nat-vent entry/exit/soft-start-escalation. | [Phase R: FSM Becomes Sole Authority](#phase-r-prep-soft-start-escalation-modeled--opt-in-cutover-switch-issue-633-epic-594) |

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
| `INACTIVE` / `PAUSED_REACTIVATION_LOCKOUT` | Idle-open re-eval, full gate passes | `ACTIVE_FULL_GATE` | `_nat_vent_may_reactivate()`, now preceded by an explicit lockout check (Issue #696 — this call site never consulted the lockout before, despite this row's prior claim) | `~L4147` (FSM), `~L4241` (legacy) |
| `INACTIVE` / `PAUSED_REACTIVATION_LOCKOUT` | Idle-open re-eval, soft-start only | `ACTIVE_SOFT_START` | same, soft-start variant — also now gated by the same lockout check (Issue #696) | `~L4147` (FSM), `~L4273` (legacy) |
| `ACTIVE_SOFT_START` | Full gate independently clears | `ACTIVE_FULL_GATE` | `_nat_vent_may_reactivate()` | `~L3125` — label-only, fan/HVAC untouched |
| `INACTIVE` | Fan already physically running at startup/reconcile, eligible | `ACTIVE_FULL_GATE` | `reconcile_fan_on_startup()` eligibility check | `~L3955` |

**Exit transitions via the `_exit_nat_vent()` choke point** (`automation.py:5312-5367`, Issue #411/#417/#418): 10 call sites (door/window-closed, proactive-floor, outdoor-rise, ceiling-threshold, nat-vent-temperature-check's session-force-close and hard-floor, fan_thermostat_check's outdoor-rise and direction-reversal-stop and cooled-to-floor, reconcile-on-startup, and RF-timer-boundary-settle in `on_fan_turned_off`). Always sets `_natural_vent_active=False, _nat_vent_soft_start=False`. Originally only the outdoor-rise-exit caller passed `set_outdoor_exit_time=True` (the sole path able to produce `PAUSED_REACTIVATION_LOCKOUT`); Issue #641 found and fixed two more call sites (proactive-floor, ceiling-threshold in `check_natural_vent_conditions()`) plus a third (`fan_thermostat_check()`'s tick-level direction-reversal stop) that handed into the same paused-reactivation race without arming it — see [Incident: WHF fast-cycling](#incident-whf-fast-cycling-proactive-floor-exit-vs-instant-reactivation-issue-641) below. Every call site's arming decision is now enforced by `tests/test_nat_vent_exit_lockout_coverage.py`'s AST coverage registry, not just documented — a new call site added later without an explicit classification fails that test immediately.

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

**Update (Issue #737 — 2 of 3 now fixed, direct code re-read, not assumed).** Re-verifying this claim against current code found it partially stale:
- `fan_thermostat_check()` was fully unified via Issue #435's architecture-reset (`fan_thermostat_decision.decide_fan_thermostat_check()`) — no inline re-check of any kind remains, FSM-authoritative or not. No longer part of this duplication.
- `check_natural_vent_conditions()` calls `decide_nat_vent_exit()` directly and unconditionally for its exit chain — it was never a hand-duplicated *branch* in the first place. No longer part of this duplication.
- `nat_vent_temperature_check()`'s **legacy** branch was the last hand-rolled duplicate: it hand-rolled a 2-check subset (manual-override-conflict, then comfort-floor) of `decide_nat_vent_exit()`'s 5-reason chain instead of calling it. **This legacy branch was deleted in Phase 6** (Issues #757–#770) — the FSM path is now the only branch, and it is what every real install runs today.

This one remaining instance was enforced by a durable static check rather than only described in prose — see `tests/test_duplicate_gate_detection.py` in [Code Reference](#code-reference) below, which AST-scans for structurally-duplicated gate conditions across the decision-leaf modules and requires every finding to be classified in a registry. Since the legacy branch it was tracking is now deleted, this instance is resolved; the registry test remains as a general-purpose guard against future duplication.

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

## Offline Whole-Engine Shadow-Pair Validation — Historical Context (Issue #611, subtask O)

Epic #594's sequencing item **O** validated the nat-vent FSM safety before deploying it to production. `tools/sim_harness/shadow_engine_pair.py` (Issue #611) replayed all 60 offline-eligible golden + pending scenarios through both production and shadow stacks, proving that constructing a `dry_run=True` shadow engine alongside production changed nothing about production's own behavior — a pre-deployment gate. The validation passed; the shadow-pair infrastructure itself was **deprecated after Phase 6** (Issues #757–#770) when all 4 FSMs proved reliable in live use, making the toggle-based comparison unnecessary. This section is retained as historical reference — the mechanism no longer exists in the codebase.

`differential.py` underwent a real canonicalization fix during this validation (context UUID comparison), which persists and is reused by other harness comparators today.

## Live Shadow Engine — Historical Context (Issue #613, subtask Q)

**Deprecated and deleted in Phase 6 (Issues #757–#770).** This section is retained as historical reference only — the live shadow-engine infrastructure no longer exists in the codebase.

`coordinator.shadow_automation_engine` operated as a real cross-instance diagnostic from 2026-08-08 (v0.6.13) through Phase 6, computing decisions in parallel with production and comparing results via `sensor.climate_advisor_shadow_engine_status`. It served as a confidence gate before making the nat-vent FSM the sole authority (and later the door/window, override/grace, economizer, and classification FSMs). Once all 4 lifecycle FSMs proved reliable in production use over several weeks, the shadow engine and its diagnostic sensor were fully removed; `lifecycle_dispatcher.py` provides the audit trail of real transitions instead, eliminating the need for a parallel comparison engine.

### Incident: WHF fast-cycling, proactive-floor exit vs. instant reactivation (Issue #641)

Reported 2026-08-15: the whole-house fan cycled on/off roughly once a minute for
several minutes (06:35-06:39) before the user disabled automation entirely. Root
cause, confirmed by direct code reading (cross-checked by an independent second
read): the log's "Nat-vent proactive exit — floor in 0.98 hr" is the `PROACTIVE_FLOOR`
exit reason (`nat_vent_exit.py`), which calls `_exit_nat_vent()` **without**
`set_outdoor_exit_time=True`. With a monitored sensor still open, `_exit_nat_vent()`
hands off into `_paused_by_door=True` with no lockout timestamp armed — so the very
next tick's paused-reactivation block (the same `_nat_vent_may_reactivate()` instant
check the entry gate always uses) finds no lockout and immediately reactivates,
producing another `PROACTIVE_FLOOR` exit next tick, repeating indefinitely. This is
deterministic, not occasional: `PROACTIVE_FLOOR` is a *predictive* check
(`time_to_floor_hr < 1.0`, driven by `k_passive`) independent of the *instant* gate
condition (`outdoor < indoor - hysteresis`) it hands off into — indoor/outdoor barely
move tick-to-tick, so if the instant gate was true just before the predictive exit
fired, it is almost always still true immediately after.

The same audit (not assumption — every `_exit_nat_vent()` call site read by hand)
found two more exit reasons with the identical structural gap: `CEILING_THRESHOLD`
(`check_natural_vent_conditions()`) and `fan_thermostat_check()`'s `STOP_DEACTIVATE`
branch — the latter's own existing code comment already called it "the exact same
boundary condition" as its sibling `STOP_VIA_NAT_VENT_EXIT`, which was already armed.
All three now pass `set_outdoor_exit_time=True`, reusing the existing 300s lockout
mechanism rather than inventing a new one.

**Root-cause fix alone was judged insufficient** — the occupant impact (rapid relay
cycling, forced to disable automation) demanded a backstop that holds even if a
*different*, future gap in this exit/entry logic reproduces the same physical
symptom. `AutomationEngine._fan_toggle_rate_limited()` is a hard floor inside
`_activate_fan()`/`_deactivate_fan()`: any reversal within `FAN_MIN_TOGGLE_INTERVAL_S`
(300s, `const.py`) of CA's own last real toggle command is suppressed, logged at
WARNING, and raised as a proactive `fan_rapid_cycling` incident
(`docs/incident-classes.md`) — visible through the existing incident pipeline with no
new plumbing. Deliberately compares against a **separate** `_fan_toggle_command_time`
field, not the pre-existing `_fan_command_time` echo-tracking field also used for
provenance attribution (Issue #482) — found during this fix's own testing that
`_reconcile_fan_physical_drift()`'s corrective "sync the stuck control entity"
off-command legitimately stamps `_fan_command_time` immediately before an
intentional same-tick recycle-on (that method's own docstring already documents this
sequence), which the shared field would have wrongly rate-limited.

**Prevention, not a one-time patch**: `tests/test_nat_vent_exit_lockout_coverage.py`
AST-scans every `_exit_nat_vent()` call site and requires each to be explicitly
classified `"arms lockout"` / `"exempted: <reason>"`, with a positive control checking
the classification against the actual `set_outdoor_exit_time=True` argument in code —
the same AST-registry enforcement shape used elsewhere in this codebase (e.g.
`tests/test_duplicate_gate_detection.py`). A new exit reason added later without a
classification fails immediately. Separately,
`CONF_FAN_MIN_RUNTIME_PER_HOUR`'s cycling decisions (`desired_state.py`'s
`decide_fan_cycle_on`/`decide_fan_cycle_off`) now floor their computed on/off phase
durations at the same 300s minimum — a configured value outside roughly [5, 55]
minutes would otherwise schedule its own off/on command inside the rate-limit window,
stranding the fan far longer than the configured cycle intended.

### Incident: idle-open re-entry never consulted the lockout at all (Issue #696)

Reported/confirmed 2026-08-23: after the wake-up routine's 06:30 comfort-floor exit
(indoor at exactly the 68°F floor), the WHF reactivated ~5 minutes later at indoor
69°F — still below the 70–72°F daytime cycling band — ran for ~5 minutes pulling in
59°F outside air, then was corrected by the next periodic cycling check. Filed as
Issue #696 on 2026-08-20 (three days before this specific occurrence) from a code
audit during the #694 FSM-wiring verification; this incident is the live confirmation.

Root cause, distinct from #641's: not a missing `set_outdoor_exit_time=True` on one
more exit reason (though `COMFORT_FLOOR` also had that gap and was fixed alongside
this), but that the **idle-open re-entry block** inside
`check_natural_vent_conditions()` (both its FSM and legacy branches) never called
`is_reactivation_locked_out()` at all, regardless of whether any exit had armed the
timer. `nat_vent_reactivation_lockout.py`'s own docstring had scoped this
deliberately, reasoning the block was "structurally unreachable... guarded by `not
self._paused_by_door`, already False at that moment." That reasoning predates (or
overlooked) **Issue #523**, which deliberately widened the block's real guard to
`_actively_paused = paused_by_door and not paused_with_hvac_already_off` specifically
so a pause where HVAC was already off would *not* block it — exactly the flag
combination a `COMFORT_FLOOR` exit with the sensor still open produces. The two
decisions contradicted each other; this incident is where that surfaced.

Fix: armed `set_outdoor_exit_time=True` on the `COMFORT_FLOOR` exit
(`nat_vent_temperature_check()`), and wired both idle-open branches to actually
consult the lockout — the FSM branch by no longer overriding `paused_by_door=False`
when building FSM inputs (letting the FSM's existing `PAUSED_REACTIVATION_LOCKOUT`
handling apply, the same as the sibling paused-by-door call site already does), the
legacy branch with an explicit `is_reactivation_locked_out()` guard mirroring that
same sibling site. The other 3 reactivation-gate call sites named in the lockout
module's docstring were individually re-verified (not assumed) and their exemptions
do hold, each for a different, still-valid reason — see the corrected module
docstring in `nat_vent_reactivation_lockout.py`.

**Known residual limitation, out of scope for #696**: the periodic evaluation that
re-triggers `check_natural_vent_conditions()` appears to land on a ~5-minute
clock-aligned cadence — the same order of magnitude as the 300s default lockout. In
the reported incident, elapsed time between exit and reactivation was ≈300.02s,
right at the lockout's edge; the fix closes the "never checked at all" gap
unconditionally, but does not guarantee every occurrence lands outside the lockout
window by a comfortable margin. Tracked as separate follow-up work, not fixed here.

### Incident: STOP_COOLED_TO_FLOOR shared #696's disproven reasoning (Issue #755)

Same disproven reasoning as #696 — `fan_thermostat_check()`'s `STOP_COOLED_TO_FLOOR` exit failed to arm the reactivation lockout. Fixed by armed `set_outdoor_exit_time=True` on this exit, identical in shape to #696's `COMFORT_FLOOR` fix. Unit test: `TestFanThermostatCheck::test_stop_cooled_to_floor_arms_reactivation_lockout`.

### Incident: check_natural_vent_conditions()'s own COMFORT_FLOOR branch shared the same disproven reasoning (Issue #739)

Same disproven reasoning as #696/#755 — `check_natural_vent_conditions()`'s `COMFORT_FLOOR` branch never armed the reactivation lockout because it bypassed `_exit_nat_vent()` entirely. Fixed by setting `_nat_vent_outdoor_exit_time` directly in this branch. Unit test: `TestNatVentComfortFloorExit::test_comfort_floor_exit_arms_reactivation_lockout`. This closes the last known comfort-floor-exit call site with the disproven reasoning.

### Follow-up: rate-limit reporting was misleading and mis-framed as an incident (Issue #649)

Live logs confirmed the #641 floor itself works correctly — a real production window
showed a clean, unbroken 5-min-on/5-min-off cadence. But reviewing the Activity Report
for one of those blocked toggles surfaced three reporting defects, all pre-existing
architecture exposed (not caused) by #641's rate limiter:

1. **A blocked toggle was reported as if it physically happened.** Every nat-vent exit
   branch (`nat_vent_predicted_floor_exit`, `nat_vent_comfort_floor_exit`,
   `fan_deactivated`, etc.) built its Activity Report event **before** calling
   `_exit_nat_vent()` → `_deactivate_fan()`/`_activate_fan()`, with no way to know
   whether the toggle actually executed or was silently swallowed by
   `_fan_toggle_rate_limited()`. This pattern predates #641 — before the rate limiter
   existed, "decided" and "physically executed" were always the same instant, so no
   caller needed to distinguish them.
2. **The same blocked moment produced multiple rows.** Two independent mechanisms: (a)
   two decision paths (e.g. `check_natural_vent_conditions()`'s `PROACTIVE_FLOOR` and
   `fan_thermostat_check()`'s comfort-floor check) can notice the same condition in the
   same tick, and (b) `fan_thermostat_check()` re-runs on every subsequent
   temperature-change tick while the fan is still physically on but blocked — each
   retry independently re-decided and re-triggered the limiter.
3. **`incident_detected`/`fan_rapid_cycling` mischaracterized correct behavior** — the
   floor blocking a too-soon toggle is the protection working, not an anomaly.

**Fix, centralized rather than repeated per call site**: `_exit_nat_vent()` gained
`event_type`/`event_payload` kwargs and now performs the toggle first, then emits the
caller's event itself with `fan_mode_change` reflecting the real
`FanCommandResult` — unchanged when executed, a `"deferred (5-min floor, applies
HH:MM:SS)"` description when newly rate-limited, and not emitted at all for a
duplicate block within an already-reported deferral window.
`_fan_toggle_rate_limited()` now tracks `_fan_rate_limited_direction` alongside
`_fan_rate_limited_until` so it can recognize "this is the same pending window as
last time" — the single change that fixes both duplicate-report mechanisms above,
since both funnel through this one function. `_activate_fan()`/`_deactivate_fan()`
log an INFO "5-minute floor expired — applying deferred exit/activation" line when a
previously-blocked command finally lands, ahead of the pre-existing (unchanged,
WARNING) "Activated/Deactivated fan" line — deferred completion is normal operation,
not a fault, so it doesn't get an elevated severity. `incident_detected`/
`fan_rapid_cycling` was removed entirely (`docs/incident-classes.md`); the first block
in a deferral window now logs at INFO instead of WARNING. `_whf_rate_limit_suffix()`
was reworded from the vague `"(rate-limited Xs ago)"` to `"(<on/off> pending — 5-min
floor, applies at HH:MM:SS)"`.

No change to the #641 floor/lockout mechanism itself, no change to any event *type*
(only payload content — the shadow-FSM re-evaluation triggers keyed on these event
types in `coordinator.py` are unaffected), and no frontend changes (the renderers in
`ai_skills_context.py` were already payload-driven for the nat-vent-specific event
types; `_render_fan_activated`/`_render_fan_deactivated` gained a small
`fan_mode_change`-override fallback since, unlike their nat-vent-specific siblings,
they previously hardcoded the state-transition text unconditionally).

### Finding: three independent 300-second constants, not one shared value (Issue #787)

While investigating an overnight fan-cycling incident, a Five Whys pass traced two
reactivations to two *different* functions than the reactivation lockout described above:
a generic per-toggle rate limiter (`FAN_MIN_TOGGLE_INTERVAL_S`, this section's `#641`
floor) and a separate "5-minute follow-up" revisit scheduler (`REVISIT_DELAY_SECONDS`,
`_schedule_revisit()`). Both happen to be hardcoded to 300 seconds — the same value as
`NAT_VENT_REACTIVATION_LOCKOUT_S` above — but as three **independently defined**
constants in `const.py` with no shared reference between them. Their apparent
coordination in that incident (each firing right as another's window closed) was
coincidental value alignment, not deliberate integration: nothing in the revisit
scheduler or the toggle rate limiter reads or depends on the lockout constant. A future
tuning change to any one of the three (e.g. lengthening the lockout to reduce flapping
further) would not propagate to the other two, and could silently reintroduce this class
of bug. Cross-referencing comments were added at all three `const.py` definitions;
formal consolidation is a tracked follow-up, not done in Issue #787's fix (which
addressed a separate, unrelated root cause — see `docs/grace-periods-spec.md`'s
"Fan-Entity Availability Blips Misread as Manual Action" section).

## Code Reference

- [`derive_nat_vent_lifecycle_state`](../custom_components/climate_advisor/nat_vent_lifecycle.py) — pure derivation
- [`AutomationEngine.nat_vent_lifecycle_state`](../custom_components/climate_advisor/automation.py) — read-only property
- [`decide_nat_vent_exit`](../custom_components/climate_advisor/nat_vent_exit.py) — pure exit-chain decision (Issue #608)
- [`_exit_nat_vent`](../custom_components/climate_advisor/automation.py#L5098) — the primary (not sole) exit choke point
- [`decide_nat_vent_gate`, `decide_nat_vent_soft_start_gate`](../custom_components/climate_advisor/nat_vent_gate.py) — entry gates
- [`is_reactivation_locked_out`](../custom_components/climate_advisor/nat_vent_reactivation_lockout.py) — lockout predicate
- [`AutomationEngine._fan_toggle_rate_limited`](../custom_components/climate_advisor/automation.py) — hard fan-toggle rate-limit backstop, independent of exit/reactivation logic (Issue #641)
- [`tests/test_nat_vent_exit_lockout_coverage.py`](../tests/test_nat_vent_exit_lockout_coverage.py) — AST-based coverage registry for every `_exit_nat_vent()` call site's lockout-arming decision (Issue #641)
- [`tests/test_duplicate_gate_detection.py`](../tests/test_duplicate_gate_detection.py) — AST-based structural-duplication ("DRY") checker: flags 2+ functions independently reimplementing the same gate/threshold condition (alpha-renamed operand matching, not textual diff), across `AutomationEngine` and the pure decision-leaf modules. Registry-enforced (Issue #737). Its former primary tracked instance — `nat_vent_temperature_check()`'s legacy branch vs `decide_nat_vent_exit()` — was resolved when Phase 6 (Issues #757–#770) deleted the legacy branch; the file's own `_ACKNOWLEDGED_DUPLICATE_GATES` registry has the current, live list.
- [`nat_vent_fsm.transition`](../custom_components/climate_advisor/nat_vent_fsm.py) — the unified `(state, event) -> Transition` table assembling gate/exit/soft-start decisions into one function (Issue #633, Block 5 Phase P completion; soft-start→full-gate escalation modeling added in Phase R prep). Since Phase 6 (Issues #757–#770), this is the sole, permanent decision path for nat-vent — there is no remaining legacy branch or authoritative-flag toggle to model around.
- [`LifecycleDispatcher`](../custom_components/climate_advisor/lifecycle_dispatcher.py), [`LifecycleEvent`/`LifecycleEventType`](../custom_components/climate_advisor/lifecycle_events.py) — generic cross-lifecycle event routing + registry-completeness enforcement, built once for reuse by every lifecycle FSM (nat-vent, door/window pause, override+grace); now the audit-trail mechanism the retired shadow engine's diagnostics used to serve — Issue #633

**Historical, no longer live** — retired in Phase 6 (Issues #757–#770); kept here only so old commit/issue references resolve, not as a pointer to current code: the offline whole-engine shadow-pair comparator (Issue #611), `ClimateAdvisorCoordinator`'s live shadow-engine mirroring/diagnostic wiring and `_sync_shadow_inputs()` (Issues #613/#615/#631), its AST coverage registry test, `ClimateAdvisorShadowEngineStatusSensor` (Issue #613), the `_natvent_fsm_authoritative` per-engine cutover flag and its full-corpus decision-equivalence comparator (Issue #633 Phase R Steps 2–3), the per-lifecycle cutover switches (Phase R Step 4, retired already by Issue #729), and `coordinator._evaluate_nat_vent_fsm()`'s three-way comparison wiring (Issue #633). None of these files or symbols exist in the current codebase (confirmed by grep before this section was rewritten).

### First live disagreement: confirms a declared scope boundary, not a bug (Issue #633) — historical

Within a minute of the 0.6.13 deploy, the (since-retired) shadow-engine diagnostic sensor logged its first live disagreement: `production=inactive fsm=active_full_gate`, at the exact moment a contact sensor opened (a fresh 600s debounce window starting). Traced via full log context, not assumed: production's real `check_natural_vent_conditions()` returned early — both calls that tick were complete no-ops — because its `_idle_open` widening (`self._any_monitored_sensor_open() and _hvac_off_244 and not self._sensor_debounce_pending and not self._grace_active`) requires `not self._sensor_debounce_pending`, and the debounce timer had just started; with `_idle_open` false and `_grace_active` false, the outer guard returns before ever reaching the reactivation-gate check. `nat_vent_fsm.py`'s v1 transition function had no notion of sensor debounce state at all — it evaluated `decide_nat_vent_gate()` unconditionally whenever `paused_by_door=False`, so it activated on the same favorable indoor/outdoor reading (72°F/67°F) production was correctly still withholding judgment on.

This was exactly the `_idle_open` widening scope boundary the module's own docstring had already declared out of v1 — not a defect. It didn't recur on the next cycle (the debounce window settled). This incident is retained as a historical data point from the migration; the shadow-engine comparison mechanism that surfaced it no longer exists — `lifecycle_dispatcher.py` now carries the cross-lifecycle audit trail instead.

## Phase R prep: soft-start escalation modeled + opt-in cutover switch (Issue #633, epic #594)

**Steps 1–4 below are historical** — kept as a record of how the migration was proven safe, but every mechanism they reference (the `_natvent_fsm_authoritative` flag, per-lifecycle switches, shadow-engine comparators) was permanently deleted in Phase 6 (Issues #757–#770). The nat-vent FSM is now the sole, unconditional decision path with no flag or switch gating it.

**Step 1:** Modeled the soft-start escalation gap in `_transition_from_active()` to re-check `decide_nat_vent_gate()` when state was `ACTIVE_SOFT_START`, mirroring production's own logic exactly. **Step 2:** Feature-flagged `_natvent_fsm_authoritative` to swap read authority from hand-written code to `nat_vent_fsm.transition()` in the soft-start-escalation branch. **Step 3:** Proved decision-equivalence via differential comparator over the full golden+pending corpus, fixing an unrelated harness bug (`_canon_action_log()` context UUID comparison). **Step 4:** Deployed per-lifecycle switch `switch.climate_advisor_nat_vent_fsm_authoritative` (non-persistent, restarted on proven legacy path). Issue #729 later collapsed 3 independent switches into one axis; Phase 6 deleted the entire dual-engine shell once FSMs proved reliable in production for weeks.
