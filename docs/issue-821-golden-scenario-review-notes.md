# Issue #821 — golden scenario conflict notes (temporary, for owner review)

**RESOLVED.** The project owner reviewed this analysis, confirmed it, and approved
retiming all 5 scenarios (each got a follow-up tick ≥90s after the instantaneous
crossing, per the recommendations below; `issue_637_paused_during_grace_nat_vent_exit`
needed its outdoor-reversal trigger moved earlier to leave 90s of sustain-confirmation
room before its own 10-minute grace period lapsed). All 5 are signed
(`python tools/simulate.py --sign <name>`), `--check-integrity` is clean, and the full
91-scenario golden suite passes. The fast-loop finding below (§ separate section) was
resolved as a **deliberate, reasoned design boundary** — `fan_thermostat_check()` stays
un-gated on purpose — documented in `docs/08-COMPUTATION-REFERENCE.md` §6f and the
`automation.py` comment at that call site. This file is kept for the historical
investigation record, not as an open action item.

**Not a permanent doc.** Originally written for the project owner's own review of the 5
LOCKED golden scenarios that failed under Issue #821's sustain-confirmation change, per
the Golden Simulation Test Policy (owner sign-off required before any golden JSON or
`test_production_harness.py` assertion is touched).

## The one pattern behind all 5 failures

Every one of these 5 scenarios' failing assertion is checked at **the exact same
timestamp as the underlying instantaneous temperature crossing** that triggers the
exit condition — i.e., each asserts the exit has already committed by the very tick the
condition first reads true, with **no second evaluation ≥90s later** (`NAT_VENT_EXIT_SUSTAIN_S`)
before the assertion checkpoint. Structurally, this is exactly the shape of the bug
Issue #821 was investigating: real Zone 1 data (2026-08-31 13:06:46–13:30:00Z) showed
nat-vent's exit/re-entry firing on a single instantaneous reading with no requirement
that the condition hold. Sustain-confirmation intentionally defers the exit past a
single-tick crossing now — these 5 goldens' expected outcomes may have been (unknowingly)
encoding that exact old, buggy behavior. That's an assessment for you to confirm, not
something this pass concluded on its own authority.

None of the 5 scenarios provide a follow-up tick ≥90s after the crossing before their
assertion runs, so under the new behavior the exit is still pending (deferred, not
lost) at the moment each assertion checks — production would still fire the exit on
its very next real evaluation cycle (well over 90s away in real operation), so nothing
here suggests the *mechanism* is broken; only that these assertion checkpoints were
timed to the old instant-commit boundary.

## Per-scenario detail

### 1. `away_natvent_exits_at_comfort_ceiling`
- **Assertion**: `@2026-07-10T14:00:00 expect='nat_vent_not_active'` — AWAY_CEILING exit.
- **Actual**: `'setback_applied'` (nat-vent still active/unexited at that instant).
- **Conflict**: The scenario's own comment says the previous mid-run assertion was
  *removed* because "it used the final engine snapshot ... making it impossible to
  assert mid-run state" — i.e., 14:00 was already chosen as a late, generous checkpoint,
  not a tight one. That makes this the most informative of the 5: even a multi-hour gap
  between the AWAY_CEILING crossing and the assertion doesn't help, because (best
  evidence available from the harness's tick cadence) there is no re-evaluation of
  `check_natural_vent_conditions()` for this scenario strictly ≥90s after indoor first
  reaches `comfort_cool` and before the 14:00 checkpoint — the harness's discrete tick
  boundaries land the crossing and the checkpoint in the same evaluation. If your review
  confirms indoor is continuously at/above the ceiling for the whole 14:00 window, the
  fix here is likely just moving the checkpoint (or adding one more tick) past the 90s
  mark, not touching the exit logic itself.

### 2. `issue-359-fan-state-machine`
- **Assertion**: `@2026-06-28T08:11:00 expect='nat_vent_comfort_floor_exit'` — COMFORT_FLOOR
  exit. Own reason text: *"nat_vent_temperature_check called with indoor=70°F = comfort_heat
  floor. Comfort-floor exit fires"* — phrased as firing at the exact crossing tick.
- **Actual**: `'nat_vent_fan_off'` — the mid-session thermostatic cycling-off state
  (a different, non-exit mechanism inside `nat_vent_temperature_check()`) fires instead,
  because the hard-exit branch is deferred pending sustain-confirmation and the function
  falls through to its cycling logic for that tick.
- **Conflict**: Textbook single-tick assertion — indoor first touches the floor and the
  exit is asserted in the same breath. Sustain-confirmation defers commit to a later
  tick; this scenario has none before/around the checkpoint.

### 3. `issue_637_paused_during_grace_nat_vent_exit`
- **Assertion**: `@2026-08-14T13:09:00 expect='paused_during_grace'`, driven by an
  OUTDOOR_RISE exit. Own reason text: *"Nat-vent exits (outdoor-rise reversal) while the
  monitored sensor is still open ..."* — again phrased as firing at the crossing.
- **Actual**: `'natural_ventilation'` — nat-vent still shows active; the exit (and the
  `_paused_by_door` handoff it drives, which is the actual thing this scenario exists to
  prove reachable — PAUSED_DURING_GRACE, Issue #637) hasn't committed yet.
- **Conflict**: Same single-tick pattern. Worth flagging specifically: this scenario's
  entire *point* is proving a specific flag-combination reachable (PAUSED_DURING_GRACE
  via the OUTDOOR_RISE exit path) — if the fix is "add a later tick," the new tick must
  still land while the 13:00 grace period (10 min) is active, i.e. before 13:10:00. There
  is only ~1 minute of headroom after 13:09:00 before the grace-active precondition this
  scenario is testing would itself lapse — worth designing the retimed assertion (or an
  earlier trigger for the outdoor reversal) with that in mind rather than moving the
  checkpoint out mechanically.

### 4. `nat_vent_thermostat_cycling`
- **Assertion**: `@2026-06-12T13:00:00 expect='nat_vent_comfort_floor_exit'`. Own reason
  text: *"Indoor=68°F reaches comfort_heat floor=68°F — hard nat-vent exit"* — same
  crossing-tick phrasing as #2.
- **Actual**: `'nat_vent_fan_off'` — same cycling-off fallback as #2, same mechanism.
- **Conflict**: Identical pattern to #2, different scenario. Notably this scenario
  already demonstrates the fan cycling on/off *within* an active session earlier in its
  own timeline (10:30/11:30 assertions, both still passing) — so the sustain-confirmation
  gate is arguably *more* appropriate here than most, since this scenario's own premise
  is "the reading dips near a boundary and recovers, don't treat every dip as terminal."
  The hard-exit assertion at 13:00 may simply need a same-treatment retiming.

### 5. `window_close_stops_whole_house_fan`
- **Assertion**: `@2026-06-12T10:00:00 expect='nat_vent_outdoor_rise_exit'`. Own reason
  text: *"outdoor=72.5F >= indoor=72F — nat-vent outdoor-rise exit fires"* — crossing-tick
  phrasing again.
- **Actual**: `'natural_ventilation'` — exit not yet committed.
- **Conflict**: Same single-tick pattern as #2/#3/#4. The scenario's next assertion
  (`@11:00:00 expect='resumed'`, all sensors closed, fan stops) is a full hour later and
  currently still passes — so there's ample room to retime the 10:00 checkpoint forward
  without disturbing the scenario's later assertions, if that's the fix you land on.

## What the original investigation pass did NOT do (superseded — see RESOLVED note above)

- Did not modify any of the 5 golden JSON files at the time this was written — all 5
  were subsequently retimed and signed once the project owner approved doing so.
- Did not modify `test_production_harness.py`'s assertions or its golden-loading
  logic — still true; only the 5 golden JSON files themselves were retimed.
- Did not change `NAT_VENT_EXIT_SUSTAIN_S` (90s) to try to make these pass — that
  constant was sized independently from real Zone 1 thermal-response data
  (`k_active_heat = 6.78°F/hr`), not backed into these 5 scenarios' timing. Still
  unchanged — the scenarios were retimed to fit the constant, not the reverse.

## One separate, already-fixed finding surfaced during this investigation

`tests/test_fan_thermostat_decision_compare.py` and `test_fan_thermostat_two_phase.py`
were in the original 24-failure list but are **not** part of the golden-scenario
question above, even though they replay `tools/simulations/golden/2026-03-28-overnight.json`.
Root-caused and fixed (not a golden JSON change): `tools/sim_harness/fan_thermostat_decision_compare.py`'s
`_classify_observation()` assumed any observed `_exit_nat_vent()` call meant
`STOP_VIA_NAT_VENT_EXIT` — but Issues #620/#755 (both predate #821) already routed
`STOP_DEACTIVATE` and `STOP_COOLED_TO_FLOOR` through the same `_exit_nat_vent()` choke
point. This was a latent, pre-existing comparator bug; confirmed unrelated to sustain-confirmation
by tracing `_resolve_fan_fsm_state()`'s own real returned outcome directly for the one
disagreeing golden (`2026-03-28-overnight`) — it was genuinely `STOP_COOLED_TO_FLOOR`,
matching the pure function, not a divergence. Issue #821 only shifted tick timing enough
for this one golden to exercise the gap for the first time. Fixed by having the comparator
disambiguate via the `reason=` string passed to `_exit_nat_vent()`, the same way it
already disambiguated `_deactivate_fan()` calls. `test_real_outcomes_never_include_stop_deactivate_or_stop_cooled_to_floor`
was renamed/narrowed to `test_real_outcomes_never_include_stop_deactivate` since
`STOP_COOLED_TO_FLOOR` is now correctly counted as a real, observed outcome (it always
was one — it just wasn't visible before). Golden JSON content: untouched.
