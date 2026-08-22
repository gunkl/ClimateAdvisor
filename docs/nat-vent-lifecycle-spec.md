<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → [nat_vent_lifecycle.py](../custom_components/climate_advisor/nat_vent_lifecycle.py) | ↔ [Grace Periods Spec](grace-periods-spec.md) -->

# Nat-Vent Lifecycle — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What are the 4 nat-vent session states and how do they map to flags? | `INACTIVE`, `ACTIVE_FULL_GATE`, `ACTIVE_SOFT_START`, `PAUSED_REACTIVATION_LOCKOUT` — derived purely from `_natural_vent_active`/`_nat_vent_soft_start`/`_paused_by_door`/`_nat_vent_outdoor_exit_time`. | [State Transitions](#state-transitions) |
| Where is the derivation computed, and is it load-bearing? | `nat_vent_lifecycle.py::derive_nat_vent_lifecycle_state()`, exposed read-only via `AutomationEngine.nat_vent_lifecycle_state`. Purely observational — not called from any production decision path. | [Scope](#scope) |
| Does every nat-vent exit hand off through the same choke point? | No — `_exit_nat_vent()` is the choke point for most exits (10 call sites, per `tests/test_nat_vent_exit_lockout_coverage.py`'s AST scan), but the comfort-floor exit inside `check_natural_vent_conditions()`, the away-mode ceiling exit, the ODE ceiling-escalation exit, and two reconcile/bedtime paths mutate the flags directly instead. | [Exit Paths](#exit-paths-not-unified-through-a-single-function) |
| What is the reactivation lockout and how long is it? | 300s default (`NAT_VENT_REACTIVATION_LOCKOUT_S`). Originally armed only by the outdoor-warm-rise exit; Issue #641 extended arming to the proactive-floor, ceiling-threshold, and fan_thermostat_check's tick-level direction-reversal exits, once all three were found to hand off into the same paused-reactivation race the lockout exists to prevent. | [State Transitions](#state-transitions) |
| What happens when nat-vent exits and the sensor is still open — pause or grace? | `_exit_nat_vent()` forks: sensor still open → hands off into the pause lifecycle; sensors closed → hands off into the grace lifecycle. See `grace-periods-spec.md` for what happens next in either lifecycle. | [Handoff to Pause/Grace](#handoff-to-pausegrace) |
| How was this spec's accuracy verified? | Differential replay: `derive_nat_vent_lifecycle_state()` run against the real final engine flags from all 74 golden + 4 pending scenarios (Issue #606), plus 3 hand-reasoned ground-truth scenarios. | [Verification](#verification) |
| Is every `_exit_nat_vent()` call site's lockout-arming decision enforced, not just documented? | Yes — `tests/test_nat_vent_exit_lockout_coverage.py` AST-scans every call site and requires each to be explicitly classified `"arms lockout"` / `"exempted: <reason>"`, with a positive control that checks the claim against the actual `set_outdoor_exit_time=True` argument (Issue #641). | [Incident: WHF fast-cycling](#incident-whf-fast-cycling-proactive-floor-exit-vs-instant-reactivation-issue-641) |
| Is there a hard floor on how fast CA can physically toggle the fan, independent of any exit/reactivation logic? | Yes — `AutomationEngine._fan_toggle_rate_limited()` (Issue #641), a defense-in-depth backstop inside `_activate_fan()`/`_deactivate_fan()`: any reversal within `FAN_MIN_TOGGLE_INTERVAL_S` (300s) of CA's own last real toggle command is suppressed and logged at INFO (a repeat block within the same deferral window logs at DEBUG only, Issue #649). No longer raised as an incident — Issue #649 removed `fan_rapid_cycling`, since a blocked-and-deferred toggle is the floor working correctly, not an anomaly; it's surfaced via an accurate Activity Report event payload and a WHF status-card suffix instead. Never affects genuine user/RF-remote fan actions. | [Follow-up: rate-limit reporting](#follow-up-rate-limit-reporting-was-misleading-and-mis-framed-as-an-incident-issue-649) |
| Does `check_natural_vent_conditions()`'s exit chain always decide WHY a session ends? | No — confirmed by direct experiment (Issue #608): `nat_vent_temperature_check()` and `fan_thermostat_check()` (both dispatched from the same `temp_update`/thermostat-attribute-change trigger, both already pure-extracted by Issue #441) run FIRST and independently implement equivalent comfort-floor and outdoor-rise-style stops — they win the race and exit the session before `check_natural_vent_conditions()`'s chain ever evaluates, for every golden scenario tested. **Update (Issue #737):** `fan_thermostat_check()`'s stop is no longer a hand-duplicated inline check (Issue #435 unified it via `decide_fan_thermostat_check()`) — only `nat_vent_temperature_check()`'s legacy branch remains a genuine hand-rolled duplicate today; see the Known Duplicate-Logic Race section's update note. | [Known Duplicate-Logic Race](#known-duplicate-logic-race-issue-608-finding) |
| Has a shadow engine instance been proven safe to construct alongside production, offline? | Yes — `tools/sim_harness/shadow_engine_pair.py` (Issue #611) proves, across 60 offline-eligible golden+pending scenarios, that a dry_run=True shadow instance never issues a real action AND never changes what production itself does. | [Offline Whole-Engine Shadow-Pair Validation](#offline-whole-engine-shadow-pair-validation-issue-611-subtask-o) |
| Is there a real, live shadow engine running inside the coordinator today, and is its coverage complete? | Yes to both — `coordinator.shadow_automation_engine` (Issue #613), permanently `dry_run=True`, mirrors all 13 real nat-vent entry points, the 3 input-data attributes they depend on (Issue #615), the 7 grace/override lifecycle-gate fields plus companions (Issue #631) and `_grace_protects_override` (Issue #639), and `_fan_active` via a raw-copy fix for a mirror that looked wired but was inert (Issue #716) — enforced by an AST-based coverage registry test (13 tracked fields total as of #716) so new gaps can't ship silently. Zero occupant/HVAC impact; surfaced via a diagnostic sensor, not any Status-tab card. | [Live Shadow Engine](#live-shadow-engine-issue-613-subtask-q) |
| Does the shadow engine's `_fan_active` mirroring actually work, and is `lifecycle_dispatcher.py` wired into production yet? | `_fan_active` mirroring was inert until Issue #716 (its two real writers both `return` early under `dry_run`, before a `_mirror_to_shadow()` replay could ever reach the field) — fixed via the same `_sync_shadow_inputs()` raw-copy mechanism as the grace/override fields. Yes, `lifecycle_dispatcher.py` is wired into production (Issue #717) — but as a same-instance emit/consume round-trip, not a cross-instance mirror; the FSM input builders still read canonical attributes directly. | [§ Live Shadow Engine — Fan-Active Mirroring Was Inert](#incident-fan-active-shadow-mirroring-was-inert-issue-716) |
| Can the nat-vent FSM actually drive real HVAC/fan hardware yet? | As of Issue #729, `AutomationEngine._natvent_fsm_authoritative` is fixed at construction (`True` on the FSM engine identity, `False` on the legacy one) — the standalone `switch.climate_advisor_nat_vent_fsm_authoritative` entity described below (Step 4) was retired; `switch.climate_advisor_shadow_engine_primary` is now the single control axis, choosing which whole engine identity is primary. | [Phase R prep](#phase-r-prep-soft-start-escalation-modeled--opt-in-cutover-switch-issue-633-epic-594) |

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
- `check_natural_vent_conditions()` calls `decide_nat_vent_exit()` directly and unconditionally for its exit chain — it was never a hand-duplicated *branch* in the first place (the `_natvent_fsm_authoritative` split near this call only gates the separate soft-start-escalation read, not the exit chain itself). No longer part of this duplication.
- `nat_vent_temperature_check()`'s **legacy** (`_natvent_fsm_authoritative=False`) branch is the only one still live: it hand-rolls a 2-check subset (manual-override-conflict, then comfort-floor) of `decide_nat_vent_exit()`'s 5-reason chain instead of calling it. Since `_natvent_fsm_authoritative` defaults `False` and no production coordinator flips it, this is the branch every real install runs today.

This one remaining instance is now enforced by a durable static check rather than only described in prose — see `tests/test_duplicate_gate_detection.py` in [Code Reference](#code-reference) below, which AST-scans for structurally-duplicated gate conditions across the decision-leaf modules and requires every finding to be classified in a registry (same enforcement shape as `test_shadow_engine_coverage.py`'s `_COVERAGE_REGISTRY`). Consolidating the remaining call site is still flagged as follow-up work, not undertaken in this pass — but it can no longer silently regress further (e.g. by a 4th independent copy appearing) without the registry test failing.

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

## Offline Whole-Engine Shadow-Pair Validation (Issue #611, subtask O)

Epic #594's sequencing item **O** ("offline validation, zero live risk — generalize the existing shadow-mode comparator from single-function to whole-engine comparison") is now covered for nat-vent by `tools/sim_harness/shadow_engine_pair.py`. It replays each scenario through three fully independent (engine, fake_hass, scheduler) stacks — a solo `baseline`, a paired `production` (role="production", dry_run=False), and a `shadow` (role="shadow", dry_run=True) built via N2's isolated construction (`AutomationEngineCallbacks` implicit per-call independence + `role` kwarg, #604/#605) — and checks three properties:

1. `production`'s action_log matches `baseline`'s exactly — merely constructing/running a shadow instance alongside production changes nothing about what production itself does.
2. `shadow`'s action_log is empty — `dry_run=True` enforcement holds; a shadow engine never issues a real service call.
3. `derive_nat_vent_lifecycle_state()` agrees between `production` and `shadow` at scenario end — both run identical code today, so this is the pre-divergence baseline Phase 4/Q's live agreement diagnostic will need.

This is a real, if narrow, test of the epic's own flagged HIGH-risk finding ("a shadow engine's own timer firing... can trigger production's `apply_classification()`... gated only by production's own live `dry_run=False`") — narrow because it runs entirely in engine-only mode (`use_coordinator=False`); a coordinator-level shadow instance sharing real listener wiring is Q's scope, not O's. `tests/test_shadow_engine_pair.py` sweeps all 60 offline-eligible golden + pending scenarios (18 `use_coordinator=True` scenarios are out of this harness's scope) plus 3 positive controls (forced dry_run bypass, forced lifecycle disagreement, forced production contamination) proving each of the three checks actually catches what it claims to catch, not just reports "clean" unconditionally.

**A real canonicalization gap was found and fixed during this validation**, not merely a testing artifact: `differential.py`'s existing `_canon_action_log()` falls back to `repr(obj)` for a service call's `context` field (a real HA `Context`, carrying a random per-call UUID by design). Reusing it produced 7/78 false-positive "divergences" — two independent runs of *identical* code, differing only because their Context objects had different random ids/memory addresses, not because production actually behaved differently. `shadow_engine_pair.py` uses its own `_canon_action_log_ignoring_context()` instead, excluding that field from the comparison it needs (domain/service/entity_id/data/timestamp).

## Live Shadow Engine (Issue #613, subtask Q)

Epic #594's sequencing item **Q** ("live shadow mode — a genuine second engine instance computing decisions from the same live inputs, fully inert per N2's redesign, with agreement/disagreement surfaced via a new diagnostic sensor") is now live for the nat-vent lifecycle. `coordinator.shadow_automation_engine` (superseding N2's `None` placeholder) is a real `AutomationEngine`, constructed alongside production at coordinator `__init__`, `role="shadow"`, `dry_run=True` set immediately and never toggled — there is no owner-facing switch for it this phase (that's Phase 5 / subtask R).

**Callback isolation** (`coordinator._build_shadow_automation_callbacks()`): the four callables N2's investigation traced as capable of reaching production are structurally cut off, not dry_run-gated —

- `sensor_check`, `sensor_debounce_pending`, `get_fan_physical_state`, `is_recent_fan_command` — pure reads, safely SHARED with production's own bundle (they observe ground truth, they don't act).
- `emit_event` — shadow-local (`coordinator._on_shadow_emit_event`), a capped list, never the production `_event_log` ring buffer or its pre-cool-reschedule side effect.
- `request_refresh`, `post_grace_fan_check`, `reclassify` — no-op lambdas. Each is invoked as a plain synchronous call in `automation.py` (never wrapped in `hass.async_create_task`), so a lambda returning `None` is a genuine no-op.
- `revisit` — left unset (`None`), not a no-op lambda: `_schedule_revisit()` invokes it as `hass.async_create_task(revisit_cb())`, which would crash on a lambda returning `None` (not awaitable). `None` makes `has_revisit_callback` False, so the follow-up timer is never scheduled — correct for an engine whose actions are always dry-run anyway.

**Mirrored call sites** — the nat-vent lifecycle's real entry points, each production call immediately followed by `await coordinator._mirror_to_shadow(method_name, *same_args)`:

- `apply_classification()` — all 4 coordinator.py call sites (regular cycle, startup coalesce, daily briefing generation, post-WHF-release reassertion).
- `handle_door_window_open()` / `handle_all_doors_windows_closed()` — the real `_async_door_window_changed` listener, both the debounced-open branch and the all-closed branch.
- `check_natural_vent_conditions()` — the "any sensor still open" re-evaluation inside the regular cycle.
- `nat_vent_temperature_check()` / `fan_thermostat_check()` — mirrored **unconditionally** on every thermostat temperature tick (not gated on production's own flags, unlike the production call site): both methods internally no-op on the SHADOW's own state when bound to the shadow instance, which is exactly the "does the shadow reach the same conclusion independently" property this diagnostic exists to check. Per the Known Duplicate-Logic Race finding above, `fan_thermostat_check()` is usually the function that actually *exits* a session on this dispatch path — Issue #615 found it missing from the original mirror list.
- `on_fan_turned_off()` — both call sites in the fan-entity/WHF listeners (sync method; `_mirror_to_shadow()` calls it without awaiting).
- `reconcile_fan_on_startup()` — both call sites (`trigger="ha_restart"` and `trigger="backstop_30min"`). The `ha_restart` site is the exact reproduction of the Issue #615 live incident (see below).
- `handle_bedtime()`, `handle_manual_override_during_pause()` — single coordinator.py call sites each.
- `resume_from_pause()` — mirrored from `api.py`'s `ClimateAdvisorResumeFromPauseView.post()`, the one decision method triggered from the REST API rather than the coordinator.
- `restore_state()` — mirrored at coordinator startup, alongside production's own restore. Still does **not** restore `_natural_vent_active` or `_current_classification` on either engine (that method's own documented clean-slate design) — only the fan-activity hints `reconcile_fan_on_startup()` reads.

**Input-data parity** (`coordinator._sync_shadow_inputs()`, called unconditionally at the top of every `_mirror_to_shadow()` invocation): several of the methods above read `self._last_outdoor_temp` / `self._hourly_forecast_temps` / `self._thermal_model` / `self._occupancy_mode` as engine-instance state, not as method arguments — the coordinator sets these directly on `self.automation_engine` at several call sites `_mirror_to_shadow()`'s per-decision replay never touched. Copies straight from `self.automation_engine`'s current values (never from local cycle variables), so it can't drift from whatever production most recently observed, and needs exactly one call site instead of matching every place production sets these. Issue #631 (see incident below) extended the same function with a second field group: `_grace_active`, `_manual_override_active`, `_fan_override_active`, `_override_confirm_pending`/`_mode`/`_source`, `_paused_with_hvac_already_off`, and their companion mode/source/time/duration fields. Issue #639 added `_grace_protects_override` to the same raw-copy group (same "internal timer/derived field, no mirror call site can reach it" rationale). **Issue #716** added a fourth field, `_fan_active`: `fan_thermostat_check()`'s shadow mirroring existed at the method level but was inert, because the field it actually keys off is set by `_activate_fan()`/`_deactivate_fan()`, and both of those methods `return` early under `self.dry_run` before ever assigning `_fan_active` — since the shadow engine is permanently `dry_run=True`, no `_mirror_to_shadow(...)` replay of either method could ever reach the field on the shadow instance. The raw copy in `_sync_shadow_inputs()` is the only mechanism that reaches it, and — same benefit as the other raw-copied fields — it transparently covers every production-side writer of `_fan_active` (including the coordinator's own stale-flag correction at the "Thermostat set to off" branch of `_async_thermostat_changed`) without needing a dedicated mirror call per writer.

### Incident: shadow stuck "inactive" for hours (Issue #615)

Shipped 2026-08-08 (v0.6.3); within hours, `sensor.climate_advisor_shadow_engine_status` was found stuck `disagree` (`production=active_full_gate`, `shadow=inactive`) after a brief agreement right at restart. Root-caused via docs → live logs → code (not assumed): `handle_door_window_open()`'s nat-vent gate read `self._last_outdoor_temp`, which defaults to `None` and was never set on the shadow engine — its gate could never fire. A systematic AST-based audit (not another spot-check) found this was one of **9 gaps**, not the only one: 8 fully-unmirrored entry points (`fan_thermostat_check`, `on_fan_turned_off`, `reconcile_fan_on_startup`, `handle_bedtime`, `resume_from_pause`, `handle_manual_override_during_pause`, plus the 3 input-data attributes) and a partially-mirrored `apply_classification()` (2 of 4 call sites). Blast radius was confirmed zero for production HVAC safety — isolation (`dry_run`, no-op callbacks) was never implicated — the harm was entirely confined to the diagnostic sensor being untrustworthy, which could just as easily produce a false "agree" as a false "disagree" once the obvious gap was patched in isolation.

**Two engine-internal mechanisms were investigated and found NOT to need separate coordinator-level mirroring**: `_reconcile_fan_physical_drift()` (a 5-minute self-rescheduling thermo-backstop timer, `_start_fan_thermo_backstop()` → `async_call_later`) and `_re_pause_for_open_sensor()` (a grace-expiry timer callback). Both are scheduled and run entirely internally by whichever `AutomationEngine` instance they're bound to — once the entry points that start their governing timers (`_activate_fan()`, `_start_grace_period()`, reached internally from already-mirrored decision methods) run correctly on the shadow, these fire automatically on the shadow's own timers with no coordinator involvement needed.

**Durable enforcement, not another one-time patch**: `tests/test_shadow_engine_coverage.py` AST-scans `automation.py` for every method that directly assigns one of the tracked fields (`_TRACKED_FIELDS` in that file — 13 fields as of Issue #716, up from the original 4 nat-vent-only fields; grown twice since, by #631's 7 grace/override fields and #639's `_grace_protects_override`, on top of the original 4 — treat the module constant as the source of truth, this count will drift again) and requires each to be classified in an explicit registry (`"mirrored"` / `"internal"` / `"exempted: <reason>"`) — the same enforcement shape as `tests/test_executor_offload.py`'s `_BLOCKING_METHODS` registry. A new method that mutates lifecycle state and isn't registered fails the test immediately.

### Incident: shadow disagreed for 2h38m during a live grace period (Issue #631)

Live recheck on 2026-08-13 found `sensor.climate_advisor_shadow_engine_status` had shown `disagree` (`production=inactive shadow=active_full_gate`) continuously from 2026-08-12 21:02:02 to 23:40:36 — every ~30-min cycle for the full length of an active manual-override grace period, not a single self-healing tick like the earlier #618/#620 investigation. Root cause: `check_natural_vent_conditions()` (a mirrored method) gates nat-vent reactivation on `not self._grace_active`, but `_grace_active` — set by `_start_grace_period()`, called from `_confirm_override()` and `handle_fan_manual_override()` — was never synced to the shadow. Five whys traced this to the #615 audit's own scope: its `_TRACKED_FIELDS` coverage-registry test tracked only the 4 fields `derive_nat_vent_lifecycle_state()` reads, which don't include `_grace_active` even though a mirrored decision method gates on it.

A full audit (every setter of the 7 grace/override fields, cross-referenced against every existing mirror call site) found two distinct unreachable-by-mirroring categories: (1) setters called directly from coordinator.py/api.py but never followed by a `_mirror_to_shadow(...)` call (`handle_fan_manual_override`, `handle_manual_override`/`start_override_confirmation`, direct `clear_manual_override` calls, `cancel_override`), and (2) setters fired only by a private `async_call_later` timer inside `AutomationEngine` with no coordinator call site to attach a mirror to at all (`_on_grace_expired`, the `_confirm_override_expired` timer's clear path).

**Fix — extended `_sync_shadow_inputs()`, not new mirror call sites.** An earlier draft of this fix added explicit `_mirror_to_shadow(...)` calls at the 4 unmirrored direct-call chains; this was rejected on two grounds: it reintroduces the exact "duplicate each write at its call site" anti-pattern `_sync_shadow_inputs()` was built to eliminate for outdoor temp/forecast/thermal-model, and calling the real setter methods on the shadow would start genuine `async_call_later` timers against the shared `hass` event loop for no benefit over a raw copy refreshed on every mirrored cycle (which, given how frequently mirrored calls fire, catches any of the 4 setter chains within one cycle regardless of which one changed the field). `tests/test_shadow_engine_coverage.py`'s registry gained entries for all 10 newly-discovered setters, each classified `"exempted"`/`"internal"` with the specific reason it isn't mirrored — plus `restore_state`, which was already mirrored in code but had never been added to the registry.

**Offline harness (Issue #611) cross-check**: `tools/sim_harness/run_production.py`'s scenario dispatcher supports `bedtime` (11 golden/pending scenarios exercise it) and `reconcile_fan_on_startup` (2 scenarios) as event types — but its "shadow" role replays the *same* event list against both production and shadow stacks directly, not through selective coordinator-level mirroring, so it was never structurally vulnerable to this bug class and its Phase 3 "60/60 clean" result stands unaffected. It has **no** event-type support at all for `resume_from_pause` or `handle_manual_override_during_pause` — a real, separate gap in offline scenario coverage, noted here but out of this fix's scope.

**Isolation of the mirror itself** (`coordinator._mirror_to_shadow()`): any exception from the shadow call, and separately any exception from `_sync_shadow_inputs()` or the diagnostic recompute that follows it, is caught, logged at WARNING, and swallowed — never re-raised. A bug in the shadow engine's decision code, or a `None`/mock-shaped `shadow_automation_engine` (several older tests partially instantiate the coordinator via `object.__new__()` without a full `__init__`), must never be able to affect `_async_update_data()`'s own control flow. Supports both sync (`on_fan_turned_off()`) and async decision methods, awaiting the result only if it's actually awaitable.

**Diagnostic** (`coordinator._update_shadow_engine_diagnostic()`, `coordinator.shadow_engine_diagnostic`): reuses `derive_nat_vent_lifecycle_state()` (Issue #606) — the same pure function both engines' state already agreed on in Phase 3's offline sweep — computed for both `automation_engine` and `shadow_automation_engine` against the real live clock, recomputed after every mirrored call. Surfaced via `ClimateAdvisorShadowEngineStatusSensor` (`sensor.py`), a `diagnostic`-category entity: state is `"agree"` / `"disagree"` / `"inactive"` (before the first mirrored decision), attributes carry both derived states and the check timestamp. Deliberately **not** wired into any of the four occupant-facing Status-tab cards (`docs/07-...`/CLAUDE.md's Status Card Ontology, Issue #527) — the shadow engine has zero HVAC impact; this is an internal architecture-validation signal, not an automation-behavior explanation.

**Cleanup**: `coordinator.async_shutdown()` calls `shadow_automation_engine.cleanup()` alongside production's — the shadow engine schedules its own real `async_call_later` timers (grace, fan min-cycle, thermo backstop) directly on the real HA event loop regardless of `dry_run` (which only guards the service-call choke points), so it needs the same timer cancellation at shutdown.

### Incident: fan-active shadow mirroring was inert (Issue #716)

`fan_thermostat_check()` has been in the "Mirrored call sites" list above since Issue #615 — the *method* itself is reached correctly on every thermostat temperature tick. But the field its shadow-agreement check actually keys off, `_fan_active`, was never actually being reproduced on the shadow instance: `_fan_active`'s only two real writers, `_activate_fan()` and `_deactivate_fan()`, both `return` early under `if self.dry_run:` before ever assigning the flag — and the shadow engine is permanently `dry_run=True`. A `_mirror_to_shadow("fan_thermostat_check", ...)` replay calls the mirrored method correctly, but the method's own internal calls to `_activate_fan()`/`_deactivate_fan()` on the shadow instance always short-circuit before touching `_fan_active`, so the field could never be reproduced on the shadow no matter how faithfully the outer method call was mirrored. This is the same *class* of gap #615/#631 each found — a method reached correctly, but a field inside it unreachable — just discovered a third time in a different lifecycle field.

**Fix:** same mechanism as the Issue #631 grace/override fields, not a new one: `_sync_shadow_inputs()` gained a raw copy, `se._fan_active = ae._fan_active`, refreshed on every mirrored cycle regardless of which of `_activate_fan()`, `_deactivate_fan()`, or the coordinator's own stale-flag correction in `_async_thermostat_changed()` last set the value on production. `tests/test_shadow_engine_coverage.py`'s `_TRACKED_FIELDS` registry gained `_fan_active` (13 tracked fields total now), and `_activate_fan`/`_deactivate_fan` were classified `"internal"` in `_COVERAGE_REGISTRY` with the dry_run-early-return rationale. A dedicated coverage test, `test_fan_thermostat_check_mirrored_at_all_three_call_sites`, also closed a related per-caller gap found during the same audit: `fan_thermostat_check()` has 3 real coordinator.py call sites (indoor-temp listener, outdoor-temp listener, thermostat attribute-change dispatch), and only the third had ever been mirrored — the indoor and outdoor listeners are now mirrored too.

**What this means for the "internal timers don't need separate mirroring" framing above:** the `_reconcile_fan_physical_drift()`/`_re_pause_for_open_sensor()` conclusion in the Issue #615 incident write-up above is still correct — those two mechanisms genuinely run entirely on whichever engine instance owns them, with no coordinator involvement needed once their governing entry points run correctly on the shadow. `_activate_fan()`/`_deactivate_fan()` are a different case from those two: they aren't internal-only mechanisms that need no mirroring — they're real field writers that a normal `_mirror_to_shadow()` replay structurally cannot reach (the `dry_run` guard defeats it before the assignment), which is why they needed the same raw-copy treatment as the Issue #631 grace/override fields instead.

### Dispatcher wiring status (Issue #717)

`lifecycle_dispatcher.py` — built alongside the FSMs (Issue #633) but until now never wired into a real decision path — is wired into production as of Issue #717 (PR #720). `AutomationEngine` now emits real `DOOR_PAUSE_STARTED/ENDED`, `GRACE_STARTED/ENDED`, `OVERRIDE_CONFIRMED/CLEARED`, and `NAT_VENT_SESSION_STARTED/ENDED` events at its genuine transition chokepoints, including a `NAT_VENT_SESSION_STARTED/ENDED` pair derived from a before/after diff of `_natural_vent_active` around `_decision_pass()` — the single serialization point every nat-vent write site already passes through. This is orthogonal to the live shadow engine described in this section: the dispatcher wiring is a same-instance emit/consume audit trail inside a single `AutomationEngine` (production emits events and production itself consumes them), not a second, cross-instance mirror the way `coordinator.shadow_automation_engine` is. The FSM input builders (`_build_nat_vent_fsm_inputs()`/`_build_door_window_fsm_inputs()`) still read the canonical `_paused_by_door`/`_grace_active`/`_manual_override_active`/`_natural_vent_active` attributes directly, not a dispatcher-synced mirror — an earlier draft routed them through dispatcher-only mirror attributes and was reverted, since a same-instance emit/consume round-trip can never actually go stale the way the shadow engine's genuine cross-instance mirror could. See [02-ARCHITECTURE-REFERENCE.md § FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer) for the full wiring writeup and PR #720's commit message for the detailed before/after reasoning.

**Issues #721/#722 (follow-up):** two more cross-reads were investigated for the same re-sourcing — `handle_manual_override_during_pause()`/`resume_from_pause()`'s `_paused_by_door` guard (#721), and `door_window_fsm.py`'s `whf_owns_hvac` input (#722). Both hit the identical test-fixture conflict that caused the "reverted" outcome above: several test files set the canonical attribute directly (`engine._paused_by_door = True`, `engine._pre_fan_hvac_mode = "cool"`), bypassing the dispatcher, then exercise the guarded code immediately. Both stayed canonical. #722 additionally closed a real gap in its own write-site count: `_pre_fan_hvac_mode` has 4 real writers, not the 2 originally named (`_suppress_hvac_for_whf()`/`_release_whf_and_reclassify()`) — `_deactivate_fan()`'s two stranded-suppression-release branches (`08-COMPUTATION-REFERENCE.md` §9c, Issue #618) also clear it. A new `_resolve_whf_hvac_suppression()` chokepoint now covers all 4, emitting `WHF_HVAC_SUPPRESSED`/`WHF_HVAC_RELEASED` into `_dispatched_whf_owns_hvac` — audit-trail only, same as every other `_dispatched_*` mirror.

**Issue #724 (separate mechanism — shadow raw-copy, not dispatcher):** found during the #721/#722 verification pass as a distinct gap in `coordinator.py`'s `_sync_shadow_inputs()` (the cross-instance shadow mirror described earlier in this section), not `lifecycle_dispatcher.py`. `_pre_fan_hvac_mode` was never in that raw-copy block, so `shadow_automation_engine._whf_owns_hvac()` was permanently `False`. The issue as filed called this dormant; investigation found it live-reachable instead: `_sync_paused_by_door_with_live_sensors()` (called from 4 already-mirrored entry points) reads `_whf_owns_hvac()` as an early-return guard before calling `_pause_for_door_window()`, which sets `_paused_by_door` — a field the shadow diagnostic's `mirror_agrees`/`door_window_mirror_agrees` axes directly compare. Without the raw copy, a genuine WHF session with a window open (WHF's designed use case) made the shadow engine incorrectly self-pause while production correctly did not, a real false-disagreement source, not merely future risk. Fixed with the same one-line raw-copy pattern as `_fan_active` (Issue #716): `se._pre_fan_hvac_mode = ae._pre_fan_hvac_mode`. Zero production/HVAC impact — the shadow engine is permanently `dry_run=True`.

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
the same enforcement shape as `tests/test_shadow_engine_coverage.py`'s registry. A new
exit reason added later without a classification fails immediately. Separately,
`CONF_FAN_MIN_RUNTIME_PER_HOUR`'s cycling decisions (`desired_state.py`'s
`decide_fan_cycle_on`/`decide_fan_cycle_off`) now floor their computed on/off phase
durations at the same 300s minimum — a configured value outside roughly [5, 55]
minutes would otherwise schedule its own off/on command inside the rate-limit window,
stranding the fan far longer than the configured cycle intended.

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

## Code Reference

- [`derive_nat_vent_lifecycle_state`](../custom_components/climate_advisor/nat_vent_lifecycle.py) — pure derivation
- [`AutomationEngine.nat_vent_lifecycle_state`](../custom_components/climate_advisor/automation.py) — read-only property
- [`decide_nat_vent_exit`](../custom_components/climate_advisor/nat_vent_exit.py) — pure exit-chain decision (Issue #608)
- [`_exit_nat_vent`](../custom_components/climate_advisor/automation.py#L5098) — the primary (not sole) exit choke point
- [`decide_nat_vent_gate`, `decide_nat_vent_soft_start_gate`](../custom_components/climate_advisor/nat_vent_gate.py) — entry gates
- [`is_reactivation_locked_out`](../custom_components/climate_advisor/nat_vent_reactivation_lockout.py) — lockout predicate
- [`AutomationEngine._fan_toggle_rate_limited`](../custom_components/climate_advisor/automation.py) — hard fan-toggle rate-limit backstop, independent of exit/reactivation logic (Issue #641)
- [`tests/test_nat_vent_exit_lockout_coverage.py`](../tests/test_nat_vent_exit_lockout_coverage.py) — AST-based coverage registry for every `_exit_nat_vent()` call site's lockout-arming decision (Issue #641)
- [`tests/test_duplicate_gate_detection.py`](../tests/test_duplicate_gate_detection.py) — AST-based structural-duplication ("DRY") checker: flags 2+ functions independently reimplementing the same gate/threshold condition (alpha-renamed operand matching, not textual diff), across `AutomationEngine` and the pure decision-leaf modules. Registry-enforced (Issue #737) — currently tracks `nat_vent_temperature_check()`'s legacy branch vs `decide_nat_vent_exit()` (this section's remaining live duplicate) plus 2 other confirmed real instances and 1 reviewed coincidental match; see the file's own `_ACKNOWLEDGED_DUPLICATE_GATES` registry for the full, current list and how to extend it.
- [`run_shadow_pair_scenario`](../tools/sim_harness/shadow_engine_pair.py) — offline whole-engine shadow-pair comparator (Issue #611)
- [`ClimateAdvisorCoordinator._mirror_to_shadow`, `_build_shadow_automation_callbacks`, `_update_shadow_engine_diagnostic`](../custom_components/climate_advisor/coordinator.py) — live shadow engine wiring (Issue #613)
- [`ClimateAdvisorCoordinator._sync_shadow_inputs`](../custom_components/climate_advisor/coordinator.py) — input-data parity, full-coverage decision mirroring (Issue #615), extended to grace/override state (Issue #631)
- [`tests/test_shadow_engine_coverage.py`](../tests/test_shadow_engine_coverage.py) — AST-based coverage registry, durable enforcement (Issue #615, #631)
- [`ClimateAdvisorShadowEngineStatusSensor`](../custom_components/climate_advisor/sensor.py) — diagnostic sensor (Issue #613)
- [`nat_vent_fsm.transition`](../custom_components/climate_advisor/nat_vent_fsm.py) — the unified `(state, event) -> Transition` table assembling the 3 pieces above (Issue #633, Block 5 Phase P completion). v2 (Phase R prep, epic #594) models the soft-start→full-gate escalation via the same `decide_nat_vent_gate()` call the entry path already makes; the `_idle_open` widening remains unmodeled but is re-classified as a caller-side triggering precondition, not omitted decision logic — see the module's own docstring.
- [`AutomationEngine._natvent_fsm_authoritative`](../custom_components/climate_advisor/automation.py) — Phase R, Step 2 cutover flag (default `False`). When set, `check_natural_vent_conditions()`'s active-session soft-start-escalation read routes through `nat_vent_fsm.transition()` instead of a hand-duplicated inline copy of the same math; the exit-chain decision was already calling the identical pure function directly, so nothing changed there.
- [`nat_vent_fsm_authoritative_compare.py`](../tools/sim_harness/nat_vent_fsm_authoritative_compare.py), [`tests/test_nat_vent_fsm_authoritative_compare.py`](../tests/test_nat_vent_fsm_authoritative_compare.py) — full-corpus decision-equivalence proof that flipping the flag is a behavioral no-op (diffs the entire event_log/action_log, not a derived state label) — Issue #633, Phase R Step 3.
- ~~`ClimateAdvisorCoordinator.set_natvent_fsm_authoritative`/`natvent_fsm_authoritative`, `ClimateAdvisorNatVentFsmAuthoritativeSwitch`~~ — Phase R, Step 4's per-lifecycle cutover switch, **retired by Issue #729**: `_natvent_fsm_authoritative` is now fixed at engine construction, not an independent runtime toggle. `switch.climate_advisor_shadow_engine_primary` is the single remaining control axis. Kept here as history of how Step 4 originally shipped.
- [`ClimateAdvisorCoordinator._evaluate_nat_vent_fsm`](../custom_components/climate_advisor/coordinator.py) — live wiring (Issue #633): runs `nat_vent_fsm.transition()` against production's real current readings as a **third**, independent comparison point alongside the existing production/shadow mirror. v1 scope, deliberately narrow: only triggered from `check_natural_vent_conditions`'s mirror — the one mirrored method that unambiguously corresponds to nat-vent's own periodic gate/exit re-evaluation (not `apply_classification`/`reconcile_fan_on_startup`'s mirrors, neither of which actually runs the gate/exit chain in production). The FSM's own tracked state (`coordinator._nat_vent_fsm_state`) is never written onto either engine — pure comparison, zero actuation surface, same isolation posture (try/except, swallow, log) as the shadow mirror itself. Surfaced via the same `ClimateAdvisorShadowEngineStatusSensor` as a `nat_vent_fsm_state` attribute; folded into the sensor's overall `agree`/`disagree` value alongside the mirror comparison.
- [`LifecycleDispatcher`](../custom_components/climate_advisor/lifecycle_dispatcher.py), [`LifecycleEvent`/`LifecycleEventType`](../custom_components/climate_advisor/lifecycle_events.py) — generic cross-lifecycle event routing + registry-completeness enforcement, built once for reuse by every future lifecycle FSM (door/window pause, then override+grace) — Issue #633

### First live disagreement: confirms a declared scope boundary, not a bug (Issue #633)

Within a minute of the 0.6.13 deploy, `sensor.climate_advisor_shadow_engine_status`'s new `nat_vent_fsm_state` attribute logged its first live disagreement: `production=inactive fsm=active_full_gate`, at the exact moment a contact sensor opened (a fresh 600s debounce window starting). Traced via full log context, not assumed: production's real `check_natural_vent_conditions()` returned early — both calls that tick were complete no-ops — because its `_idle_open` widening (`self._any_monitored_sensor_open() and _hvac_off_244 and not self._sensor_debounce_pending and not self._grace_active`) requires `not self._sensor_debounce_pending`, and the debounce timer had just started; with `_idle_open` false and `_grace_active` false, the outer guard returns before ever reaching the reactivation-gate check. `nat_vent_fsm.py`'s v1 transition function has no notion of sensor debounce state at all — it evaluates `decide_nat_vent_gate()` unconditionally whenever `paused_by_door=False`, so it activated on the same favorable indoor/outdoor reading (72°F/67°F) production was correctly still withholding judgment on.

This is exactly the `_idle_open` widening scope boundary the module's own docstring already declared out of v1 — not a new defect. It didn't recur on the next cycle (the debounce window settled). Left as a live, concrete data point rather than "fixed": sensor debounce timing is fundamentally a door/window-lifecycle concern (the timer, and the flag it gates, both belong to that lifecycle, not nat-vent's), which is exactly why door/window pause is next in the plan's sequencing — its own FSM, once built, will expose debounce state to nat-vent through the cross-lifecycle event channel (`lifecycle_dispatcher.py`) rather than nat-vent needing to model a concern that isn't its own.

## Phase R prep: soft-start escalation modeled + opt-in cutover switch (Issue #633, epic #594)

Re-scoped Phase R (the epic's final "owner-controlled cutover" step) after re-analyzing
its cutover mechanism for DRY-ness: the FSM's `transition()` calls the *same* pure
`decide_*()` functions production's own entry points already call directly, so the
decision logic was never duplicated — only the read/write of *state* was. Cutover is
therefore not "build a second actuator alongside production," it's "make the FSM's
state the thing production's existing choke points condition on, instead of the ad-hoc
flags." One set of `_activate_fan`/`_deactivate_fan`/`_set_hvac_mode` calls, always.

**Step 1 — modeled the soft-start escalation gap.** `_transition_from_active()` now
re-checks `decide_nat_vent_gate()` whenever the current state is `ACTIVE_SOFT_START`,
mirroring `automation.py`'s own "soft-start → full nat-vent upgrade" block (Issue #540)
exactly — same pure function, same condition, no new decision logic. The `_idle_open`
widening remains unmodeled but is re-classified (see Code Reference above) rather than
left as an open gap: it gates *whether* the FSM's entry logic runs a given tick, which
Step 2's cutover makes moot by construction — once production's own call site is what
invokes `transition()`, the FSM only ever runs when that precondition already held.

**Step 2 — read-authority swap, feature-flagged.** `AutomationEngine._natvent_fsm_authoritative`
defaults `False`. When set, the soft-start-escalation branch inside
`check_natural_vent_conditions()`'s active-session block computes its answer via
`nat_vent_fsm.transition()` instead of a second, hand-written copy of the same
`decide_nat_vent_gate()` call — a DRY fix, not a behavior change. The exit-chain
decision immediately below was already calling `decide_nat_vent_exit()` directly, so
there was nothing to swap there.

**Step 3 — decision-equivalence, not just state-label agreement.** Every prior FSM
coverage test (this spec's own "Live Shadow Engine" section, `test_shadow_engine_pair.py`)
proved the FSM's *derived state label* matched production's flags. None proved that
flipping a "let the FSM decide" switch leaves the *real commanded actions* unchanged.
`nat_vent_fsm_authoritative_compare.py` + its test file close that gap: `diff_runs()`
over the full golden+pending corpus with the flag forced on, diffing the entire
`event_log`/`action_log` against an untouched baseline. This surfaced a real,
unrelated bug in the shared differential-harness itself — `_canon_action_log()` was
comparing each action's raw `Context` object (a fresh `uuid4()` per call, by design,
mirroring real HA) via `repr()`, which can never agree between two independent runs
regardless of behavior. Fixed by excluding `context` from comparison (real decision
content — domain/service/data/ts — is unaffected). A positive control confirms the
comparator can still detect a genuinely broken FSM gate; the corpus itself doesn't yet
contain a soft-start scenario, a documented gap the positive control substitutes for
by exercising the branch directly.

**Step 4 — per-lifecycle switch (retired, see below).** `switch.climate_advisor_nat_vent_fsm_authoritative`,
same on/off mechanics as the pre-existing `automation_enabled` switch, but deliberately
**not** persisted across restart — this is the first switch in the Block 5 migration
capable of letting a bug in new code reach real hardware (every prior phase was
`dry_run=True` shadow-only by construction), so an unattended restart always comes back
up on the proven legacy path rather than silently carrying the choice forward.

**Superseded by Issue #729.** The owner asked for two changes: (1) hold whatever state a
switch was last set to across a restart, instead of always resetting to legacy — a
deliberate trade of the auto-reset safety property above for a persisted, requested
control-model change; (2) collapse the 3 independent per-subsystem switches (this one,
door/window's, override/grace's) into a single axis, since each engine object's flags
never actually needed to vary independently in practice. `_natvent_fsm_authoritative` is
now fixed at construction — `True` on the FSM engine identity, `False` on the legacy one —
and `switch.climate_advisor_shadow_engine_primary` chooses which whole identity is
primary, persisted, via a config-entry reload rather than a live in-process swap.

**Not yet done**: no golden/pending scenario exercises nat-vent soft-start, so the
Step 3 corpus comparator's "clean across the corpus" result is real but narrower than
it looks — the swapped branch is only genuinely exercised by the direct-engine positive
control, not by any scenario file. Door/window pause and override/grace each have their
own known state/flag inconsistency against production (see `grace-periods-spec.md`)
that needs resolving as part of their own future Step 1, before either can repeat this
sequence.
