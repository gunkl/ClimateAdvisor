<!-- Nav: ← [08-COMPUTATION-REFERENCE.md §6f](08-COMPUTATION-REFERENCE.md#6f-comfort-family-nearest-edge-arming-issue-1066--supersedes-827843858867) | → [comfort_family_decision.py](../custom_components/climate_advisor/comfort_family_decision.py), [comfort_family_fsm.py](../custom_components/climate_advisor/comfort_family_fsm.py), [automation.py `_comfort_family_inputs`](../custom_components/climate_advisor/automation.py) | ↔ [scheduler-spec.md](scheduler-spec.md), [grace-periods-spec.md](grace-periods-spec.md), [activity-report-table.md](activity-report-table.md) -->

# Comfort-Edge Selection — Territory Spec (Tier 3)

Issue #1073 (supersedes #1071; includes #1070). Authoritative owner of: which thermostat mode (heat or cool) is armed, when it may change, every variable that governs that, and the invariants R1-R9 each tied to a named test. If another doc disagrees with this one about mode switching, this spec wins and the other doc is a bug.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What is the one-sentence rule? | Below the floor heats, above the ceiling cools, near an edge arms that edge; but an AGAINST-THE-GRAIN switch away from a side that ran (or was deliberately commanded) inside the window (the side that ran stands in even when the thermostat is Off / heat_cool / auto / unavailable, Issue #1149) is held while the house has not gone more than the tolerance PAST the edge, including the margin zone inside the band. Deeper than the tolerance past the edge always switches at once. | [The rule](#1-purpose-and-the-rule) |
| Which setting means "tolerance after the opposite side ran"? | `comfort_deadband_{hot,warm,mild,cool,cold}_f` (hot/cold 5 F, others 2 F). The arming margin is a separate, fixed 2 F (not a setting). | [Variable table](#4-variable-table) |
| What counts as "the opposite side ran"? | Equipment actually ran (any commander, including the user), OR a mode change / user override of that side happened inside `comfort_family_recency_window_min`. One source, persisted. | [R3](#3-invariants-r1-r9) |
| How do I know a flip was held, and why? | `comfort_switch_held` activity row (with `zone` = `breach` or `margin`) + INFO log; Debug tab shows "holding X: ... (until ~HH:MM)" (absolute time, your temperature unit). | [Events](#7-new-and-changed-activity-events) |
| Where is every mode write recorded? | `thermostat_mode_set` row from the single `_set_hvac_mode` / `_ensure_hvac_mode_for_write` choke points (re-affirms excluded). | [Events](#7-new-and-changed-activity-events) |
| Which test proves invariant Rn? | `tests/test_comfort_settling_guard.py::test_R<n>_...` | [Test map](#test-map) |
| What is unverified? | A few doc facts and open decisions (G7); the unavailable-to-available re-apply path (G9) is resolved in section 8, item 1. | [Open questions](#8-open-questions-unverified) |

## Scope

- **Files:** `custom_components/climate_advisor/comfort_family_decision.py` (pure leaf), `comfort_family_fsm.py` (anti-chatter wrapper), `automation.py` (`_comfort_family_inputs`, `_comfort_deadband_for_day_type`, `_current_thermostat_family`, `_resolve_comfort_family_via_fsm`, `comfort_family_temperature_check`, `_set_hvac_mode`, `_ensure_hvac_mode_for_write`, `_record_commanded_mode`, restore callers of `_set_temperature_for_mode`), `coordinator.py` (equipment-active tracking, TOU window push), `ode_ceiling_guard.py` consumer in `automation.py`.
- **Entry points:** `decide_comfort_family()`, `comfort_family_fsm.transition()`, `AutomationEngine._resolve_comfort_family_via_fsm()`.
- **Not covered:** the band edges themselves (docs/08 §5, §5d, §6e `select_comfort_band()`); TOU target numbers (`scheduler-spec.md`, "never a new number" stays); nat-vent / WHF / door-window ownership internals (their own specs); the house-off-target stall alert (docs/08 §6e, #1050) except where it reads the same deadband keys.

## 1. Purpose and the rule

**What the occupant experiences.** A single-setpoint thermostat guards one edge at a time. Appliances overshoot (a compressor stops about half a degree past its setpoint; thermostats report whole degrees). Without a settling rule, a TOU cool bank, an overnight pre-cool, or the occupant's own cooling is followed within minutes by the furnace starting because the house reads 1 F under the floor. That is the flip-flop this spec prevents, without ever trading away comfort past a user-visible tolerance.

**The rule (plain English).**
1. Indoor below the floor arms heat; above the ceiling arms cool (a breach).
2. Within the fixed arming margin of an edge, that edge is pre-armed (nearest-edge arming, unchanged from #1066).
3. Settling guard (new, #1073): an AGAINST-THE-GRAIN switch (the target is not the day's native side; mild has no native side so both sides qualify) away from a family that ran or was deliberately commanded/overridden (user, TOU bank, ODE guard) inside `comfort_family_recency_window_min` is HELD while the house has not gone more than the tolerance (`comfort_deadband_{day_type}_f`) PAST the target edge. **The guard covers the margin zone as well as the breach zone**: after a cool bank, pre-cool or the occupant's own cooling the house usually reads 68-70 first, which is inside the 2 F heat arming margin (not yet under the floor), and a later overshoot to 67 must not then run the furnace. The signed distance past the edge is `floor - indoor` for heat and `indoor - ceiling` for cool: positive in the breach zone, zero or negative inside the band. The guard releases when the window lapses or the house goes deeper than the tolerance past the edge (R1). The hysteresis-gap "keep current mode" path is unchanged. **When the thermostat's live mode is unknown (Off, heat_cool, auto, unavailable) the family that ran (or was deliberately commanded) most recently inside the window stands in as "the family being left" (Issue #1149):** if the selector wants the OTHER family, against the grain, within the tolerance, the selector arms the family that ran instead (for example cool at the ceiling, which idles because the house is below the ceiling) and records the hold. If both families ran inside the window the newest wins, and if the newest is the family the selector wants there is nothing to hold. With nothing run inside the window the selector adopts as before (breach/margin arm, else the day's default family).
4. The day's natural side (hot/warm: cool; cool/cold: heat) has tolerance 0: never held. Mild has no natural side: both sides get the mild value.
5. A deep breach (depth greater than tolerance) always switches immediately, ignoring the dwell lock.

Symmetric: the same code path handles heat-after-cool and cool-after-heat. Depth = degrees past the edge (floor - indoor, or indoor - ceiling), reported as a non-negative `depth_f` (0.0 inside the band) together with `zone` (`breach` or `margin`). When the window lapses a margin switch proceeds normally (the 600 s dwell lock still applies to non-breach switches). A hold never moves the dwell clock.

## 2. Context matrix

Every context that changes the band or owns the thermostat. "Band owner" is the function computing that context's floor/ceiling; "Armed side" is which edge is guarded; "Flip-flop rule" is how the settling guard applies. Line numbers are deliberately omitted (drift); function names verified by grep in the worktree.

| Context | Band owner (file :: function) | Armed side | Flip-flop rule |
|---|---|---|---|
| Hot / warm / mild / cool / cold day, home or guest, awake | `automation.py :: select_comfort_band()` (`[comfort_heat, comfort_cool]`) | Nearest edge (R-margin), tie-break `ComfortBand.active` only when thermostat mode unknown | Guard as in section 1; native side tolerance 0 |
| Home vs guest | same; guest dispatches to `handle_occupancy_home()` | Same as home | Same as home (R6) |
| Away | `select_comfort_band()` setback branch; `handle_occupancy_away()` / `_apply_occupancy_away_vacation_decision()` | Selector on setback band (empty house below setback floor heats) | Same guard; tolerance applies to the setback edge |
| Vacation | `select_comfort_band()` deep setback; `handle_occupancy_vacation()` / `_apply_occupancy_away_vacation_decision()` | Selector on deep-setback band | Same guard |
| Sleep window | `select_comfort_band()` sleep branch (`sleep_heat`/`sleep_cool`); `nat_vent_gate.py :: resolve_comfort_heat/resolve_comfort_cool` for sleep-aware edges | Nearest edge of sleep band | Same guard; tolerance size unchanged by the band move (R6) |
| Wake-up (band step up) | `automation.py :: handle_morning_wakeup()` -> `select_comfort_band()` | Selector | Edge moves; tolerance applies to the new edge. Accepted residual: see section 6 |
| Bedtime setback | `automation.py :: handle_bedtime()`, `compute_bedtime_setback()` | Selector on sleep band | Same guard |
| Pre-cool (overnight banking) | `automation.py :: handle_pre_cool()`, `compute_pre_cool_target()` (floored at `sleep_heat + hysteresis`) | Cool (bank) | Pre-cool is a mode writer: it feeds the one clock (R4) and counts as "cool ran" (R3); the morning overshoot is then held, not heated |
| Pre-heat (cold front) | `automation.py :: _schedule_pre_condition()` (`comfort_heat + pre_condition_target`) | Heat | Mode writer feeding the clock; mirrors pre-cool. Behavior otherwise not specified before #1073 (G7); see section 8 |
| TOU bank (Home/Guest) | `scheduler.py :: resolve_tou_phase()`; `automation.py :: apply_tou_precondition()`; `set_tou_precondition_window()` | Bank direction, selector NOT_APPLICABLE while PRECONDITIONING | After the bank the selector resumes; bank overshoot within tolerance is held (closes #1071) |
| TOU coast / cost window (Home/Guest) | `resolve_tou_phase()` reports NONE; normal band | Selector | Guard holds a shallow post-bank undershoot/overshoot until window lapses or depth exceeds tolerance |
| TOU Away/Vacation | `scheduler.py :: resolve_tou_away_vacation_phase()`; `automation.py :: apply_tou_away_vacation_precondition()`; `select_comfort_band(tou_expansion_f=...)` | Bank direction while preconditioning, then selector on setback band | Same as Home/Guest |
| Aggressive savings | `select_comfort_band()` (widens both edges by `CEILING_ESCALATION_SAVINGS_MARGIN_F` = 2.0 F) | Selector on widened band | Same guard on the widened edges |
| Setback modifier (trend) | `classifier.py` sets `setback_modifier`; consumed ONLY by `compute_pre_cool_target()` / `resolve_pre_cool_modifier()` (moves the pre-cool edge); NOT applied to away/vacation edges by the engine or, since Issue #1076, the chart | n/a (moves edges only) | Moves edges, never the tolerance (R6) |
| Nat-vent session | `nat_vent_gate.py :: decide_nat_vent_gate()`; `nat_vent_lifecycle`/`nat_vent_fsm` | Selector NOT_APPLICABLE while nat-vent owns HVAC | No guard; on exit the restore goes through the selector + guard (D3 / S6) |
| Whole-house fan | `automation.py :: _whf_owns_hvac()` | Selector NOT_APPLICABLE while WHF owns HVAC | Same as nat-vent; `_whf_owns_hvac` also blocks active-mode writes in `_set_hvac_mode`/`_set_temperature` |
| Door/window pause | `door_window_*.py`; `automation.py :: handle_door_window_open()`, `handle_all_doors_windows_closed()` | HVAC off while paused; selector skipped | Resume restore goes through the selector + guard with the RESOLVED band (S6/#1070) |
| Manual override + grace | `automation.py :: handle_manual_override()`, `_confirm_override()`, `_on_grace_expired()`, `_override_matches_current_decision()` (adoption compares against the selector's family and the resolved band edge via `_override_decision_mode_and_target()` / `_resolved_comfort_band()`, not `classification.hvac_mode` or the flat band; the override/grace FSM inputs use the same values through `_override_match_inputs()`); `override_*.py` | User's setpoint/mode stands; selector and `comfort_family_temperature_check()` sit out | The override counts as "that side ran" (R3). After expiry the guard applies (R5); see [grace-periods-spec.md](grace-periods-spec.md) |
| Economizer | `economizer_gate.py`, `economizer_fsm.py`; `automation.py :: _deactivate_economizer()` | Fan-assist only, no mode write on activation | Off-restore goes through selector + guard |
| Startup / restart | `coordinator.py :: _do_startup_coalesce()`; `_restore_comfort_mode_changes()` | Selector reads the thermostat's real mode | The mode-change ring buffer and the run timestamps are persisted, so the guard still sees a run from before the restart (R3/R4) |
| Occupancy return | `handle_occupancy_home()` | Selector on the home band | On heat/cool days restore goes through the selector + guard; on warm/mild days nothing is written until the next cycle (UNVERIFIED desired behavior, G7; section 8) |
| Thermostat unavailable -> available | `coordinator.py` (#1049 waking-from-unavailable handling) | Selector reads real mode when it returns (Off/unavailable/unknown = family unknown; the family that ran stands in for the guard, Issue #1149) | Resolved (G9, Issue #1075): breach re-arms on the first temperature tick; a margin-zone reading waits for the next 30-minute refresh (worst case ~30 min, plus up to 5 if the reconnect lands in startup coalescing); see section 8 |
| ODE ceiling guard (predictive cool) | `ode_ceiling_guard.py :: decide_ode_ceiling_guard()`; shell in `automation.py` | Cool write only | Reads the SAME "ran recently" source as the guard (S4) and feeds the one clock; write skipped when the selector is guarding the floor |

## 3. Invariants R1-R9

Plain-English intent is in the plan; the precise statements are here. "Tolerance" = `comfort_deadband_*_f` for the day type (0 on the natural side). "Window" = `comfort_family_recency_window_min`. "Ran(side)" = see R3.

| # | Precise statement |
|---|---|
| R1 | If the house is more than `tolerance` PAST the target edge, the selector arms that side immediately: no hold, no dwell lock, no window. |
| R2 | If the switch is against the day's grain and the family being left `Ran` within the Window, and the signed distance past the target edge is `<= tolerance` (this includes the margin zone, where the distance is zero or negative), the decision is HOLD (emit `comfort_switch_held` with `zone`, do not write a mode). Identical for heat-after-cool and cool-after-heat. The hold ends when the Window lapses or the distance past the edge exceeds the tolerance (R1). "The family being left" is the thermostat's live family; when that is unknown (Off, heat_cool, auto, unavailable) it is the family that `Ran` most recently inside the Window (Issue #1149), and the HOLD target is that family. If neither ran, there is no hold (plain adoption). |
| R3 | `Ran(side)` = max(last equipment-active time for that side, last DELIBERATE mode change to that side, last user override of that side) within the Window. Only the mode-change reasons `user`, `tou` and `ode_guard` count as deliberate (`_DELIBERATE_MODE_REASONS` in `automation.py`); a `selector`, `restore`, `pause`, `nat_vent`, `startup` or `other` write says nothing about the equipment having run, so it never arms the guard on its own. There is exactly one persisted source; it survives restart; the selector and the ODE ceiling guard both read it. |
| R4 | There is one "time since last mode change" clock. Every mode writer (selector, TOU, pre-cool/pre-heat, restore, ODE guard, user change) feeds it through the `_set_hvac_mode` / `_ensure_hvac_mode_for_write` choke points; it is persisted. The 600 s dwell lock reads it and never blocks an R1 switch. |
| R5 | When an override or grace expires, the system does not undo the user's action because of the overshoot it caused: the user's side counts as "ran" (R3) and R2 applies, bounded by R1 and the Window. |
| R6 | The rules apply identically in every context in section 2. A band change (wake, bedtime, away, savings, pre-cool trend modifier) moves the edges; it never changes the tolerance or the Window. |
| R7 | Control is identical with thermal-model confidence none, low, high or stale. The model may not alter a hold in this change. |
| R8 | Every decision (switched, held, locked out, not applicable) is logged at INFO and appears in the activity record once per genuine change (dedup per [Dedup Contract](activity-report-table.md#dedup-contract)). Every actual thermostat mode write emits `thermostat_mode_set`; re-affirms are excluded. |
| R9 | Every variable governing mode switching has a row in the [Variable table](#4-variable-table) with default, unit, clamp, owner. A new hard-coded timer without a row is a defect. A test compares the table to `const.py`. |

### Test Map

Craftsman agents create these; names are the contract.

| Invariant | Test |
|---|---|
| R1 | `tests/test_comfort_settling_guard.py::test_R1_deep_breach_switches_immediately` (heat and cool variants) |
| R2 | `tests/test_comfort_settling_guard.py::test_R2_shallow_breach_after_opposite_ran_holds` (both directions; TOU cool bank overshoot; TOU heat bank overshoot; home, guest, away, vacation); margin zone: `::test_R2_hold_applies_to_margin_zone_switches` and `tests/test_comfort_settling_margin_zone.py` (reviewer reproductions, TOU bank-end margin reading then overshoot for home/guest/away/vacation both directions, margin switch after the window lapses, native margin switch immediate, mild both sides, #1066 cold-morning regression) |
| R3 | `tests/test_comfort_settling_guard.py::test_R3_ran_source_includes_equipment_commands_and_user_and_survives_restart` |
| R4 | `tests/test_comfort_settling_guard.py::test_R4_single_clock_fed_by_every_writer_and_persisted` |
| R5 | `tests/test_comfort_settling_guard.py::test_R5_override_expiry_does_not_undo_user_action` (cool by hand then grace ends; heat mirror) |
| R6 | `tests/test_comfort_settling_guard.py::test_R6_same_rules_in_every_context` (wake/bedtime steps, pre-cool morning, aggressive savings, restores through the resolved band, door-close restore on sleep/away bands) |
| R7 | `tests/test_comfort_settling_guard.py::test_R7_model_confidence_does_not_change_control` (none/low/high/stale) |
| R8 | `tests/test_comfort_settling_guard.py::test_R8_every_decision_logged_and_event_once` |
| R9 | `tests/test_comfort_settling_guard.py::test_R9_variable_table_matches_const` (parses this spec's Variable table against `const.py`) |
| R2 with an unknown live family (Issue #1149): thermostat Off / heat_cool / auto / unavailable after a run; R1 and window release; native side; both ran (newest wins); nothing ran; reactive path; leaf and FSM | `tests/test_comfort_settling_unknown_family.py` (`TestUnknownFamilyHoldsAfterTheOppositeRan`, `TestUnknownFamilyReleases`, `TestBothFamiliesRan`, `TestLeafSettlingFromFamily`, `TestFsmAdoptsTheHold`) |
| Natural side exemption (D2=X) | `tests/test_comfort_settling_guard.py::test_R2_natural_side_never_held` |
| Mixed-day and mild-day rows of section 5 | `tests/test_comfort_settling_guard.py::test_R2_worked_examples_table` |
| Single-mode thermostats | `tests/test_comfort_settling_guard.py::test_R6_single_mode_thermostat_unaffected` |
| Stale hold released when the selector is skipped | `tests/test_comfort_settling_margin_zone.py::TestStaleHoldReleased` |
| Fixed `thermostat_mode_set` reason labels (no entity or schedule id) | `tests/test_comfort_settling_margin_zone.py::TestModeSetReasonIsFixed` |
| CA reversing an occupant's mode change is recorded | `tests/test_comfort_settling_margin_zone.py::TestReversalOfUserModeIsRecorded` |
| Debug label: absolute time, user unit | `tests/test_comfort_settling_margin_zone.py::TestDebugLabel` |
| Side effects documented in section 9 | `tests/test_comfort_settling_margin_zone.py::TestDocumentedSideEffects` |

## 4. Variable table

Verified against `custom_components/climate_advisor/const.py` and `comfort_family_decision.py` in the worktree on authoring (2026-10-11). R9: a test must keep this table and `const.py` in sync. All temperatures in the table are Fahrenheit internally.

| Variable | Default | Unit | Clamp / range | Owner | Meaning |
|---|---|---|---|---|---|
| `comfort_deadband_hot_f` | 5.0 | F | 2.0 - 8.0 | `const.py` `CONF_COMFORT_DEADBAND_HOT_F`; read by `AutomationEngine._comfort_deadband_for_day_type()` | **New meaning (D1=A, restores #827):** tolerance after the opposite side ran, applied to the against-grain (heat) side on a hot day. Native side 0. |
| `comfort_deadband_warm_f` | 2.0 | F | 1.0 - 5.0 | same | Same, warm day (against-grain = heat) |
| `comfort_deadband_mild_f` | 2.0 | F | 1.0 - 5.0 | same | Same, mild day. Mild has no native side, so both sides get this value |
| `comfort_deadband_cool_f` | 2.0 | F | 1.0 - 5.0 | same | Same, cool day (against-grain = cool) |
| `comfort_deadband_cold_f` | 5.0 | F | 2.0 - 8.0 | same | Same, cold day (against-grain = cool) |
| Arming margin | 2.0 (`COMFORT_MIN_ARMING_MARGIN_F`) | F | min(2.0, band width / 3); below `COMFORT_MIN_USABLE_MARGIN_F` = 1.0 means breach-only (no interior arming) | `comfort_family_decision.py :: effective_margin()` | How close to an edge the side is pre-armed. **Fixed, not a setting.** Replaces the #1072 use of the deadband keys as the margin |
| `comfort_family_recency_window_min` | 120.0 | min | 15.0 - 360.0 | `const.py` `CONF_COMFORT_FAMILY_RECENCY_WINDOW_MIN` | "Ran recently" window. Now used by the settling guard AND the ODE ceiling guard (one source, S4) |
| `comfort_mode_switch_min_interval_s` | 600.0 | s | user-configurable (see `CONFIG_METADATA`) | `const.py` `CONF_COMFORT_MODE_SWITCH_MIN_INTERVAL_S` / `COMFORT_MODE_SWITCH_MIN_INTERVAL_S` | Dwell lock between non-breach switches. One clock fed by every mode writer, persisted (R4). Never blocks an R1 switch |
| `COMFORT_REACTIVE_RETRY_S` | 120.0 | s | constant | `const.py` | After a selector write the reactive check does not re-trigger a full classification for the same wanted family within this time; a different family / opposite breach is not limited |
| `COMFORT_RECENT_COMMAND_WINDOW_S` | 30.0 | s | constant | `const.py`; read by `automation.py :: _current_thermostat_family()` and `_record_commanded_mode()` | A mode CA commanded less than 30 s ago wins over the live `climate` state (the entity lags the command); also the window in which a repeated command is the same change, not a new one |
| `OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F` | 1.0 | F | constant | `const.py` | Setpoint match tolerance when adopting a manual override that matches the automation's current decision (`_override_matches_current_decision()`) |
| `CLASSIFICATION_HYSTERESIS_F` | 2 | F | constant | `const.py` | Day-type threshold dead zone to prevent threshold bouncing (classification, not mode switching, but moves which tolerance row applies) |
| `manual_grace_seconds` (`DEFAULT_MANUAL_GRACE_SECONDS`) | 1800 | s | `CONFIG_METADATA` | `const.py` | Manual override grace duration |
| `automation_grace_seconds` (`DEFAULT_AUTOMATION_GRACE_SECONDS`) | 300 | s | `CONFIG_METADATA` | `const.py` | Automation grace duration |
| `NAT_VENT_HYSTERESIS_F` / `nat_vent_hysteresis_f` | 1.0 | F | constant + config key | `const.py` | Outdoor must be this far below indoor to re-activate nat-vent after an outdoor-warm exit |
| `NAT_VENT_REACTIVATION_LOCKOUT_S` / `nat_vent_reactivation_lockout_s` | 300 | s | constant + config key | `const.py` | Minimum time between an outdoor-warm exit and the next re-activation (aligned with `FAN_MIN_TOGGLE_INTERVAL_S`/`REVISIT_DELAY_SECONDS`, see const.py note) |
| `NAT_VENT_EXIT_SUSTAIN_S` | 90.0 | s | constant | `const.py` | Nat-vent exit sustain |
| `NAT_VENT_CUTOFF_REASON_SUSTAIN_S` | 90.0 | s | constant | `const.py` | Cutoff-reason sustain |
| `DEFAULT_COMFORT_HEAT` / `DEFAULT_COMFORT_COOL` | 68 / 74 | F | config | `const.py` | Default comfort floor / ceiling (docs 70/75 were stale) |
| `CEILING_ESCALATION_SAVINGS_MARGIN_F` | 2.0 | F | constant | `const.py` | Aggressive-savings widening of both comfort edges |
| `COMFORT_MODE_CHANGE_LOG_CAP` | 200 | entries | constant | `const.py` | Cap on the persisted mode-change ring buffer (feeds R3/R4). Entry `reason` is one of `selector` / `pause` / `restore` / `tou` / `ode_guard` / `nat_vent` / `user` / `startup` / `other` (`_MODE_CHANGE_REASONS`); only `user` / `tou` / `ode_guard` are deliberate (`_DELIBERATE_MODE_REASONS`) and count as "ran" for the guard. The 600 s dwell clock reads the newest entry of this buffer, so it is persisted and survives a restart |
| `COMFORT_HOLD_LOG_CAP` | 100 | entries | constant | `const.py` | Cap on the persisted settling-guard hold-episode log (`AutomationEngine.comfort_holds`: `start_ts`, `end_ts`, `end_reason` recovered / switched / lapsed, `candidate_family`, `zone` breach / margin / null, `max_depth_f`, `tolerance_f`, `model_confidence`); `zone` is the decision's `hold_zone` passed through, escalating margin to breach if any reading in the episode is a breach (old stored rows without it load as null = unknown); read by the control-quality report |

Legacy duplicate: `_last_hvac_heating_active` / `_last_hvac_cooling_active` (30-minute grain, `automation.py`, also read by the HVAC-fan restrict check) and `coordinator._equipment_last_active` are merged into the R3 source by S4; the HVAC-fan restrict consumer must keep working off the merged source.

## 5. Worked examples

All use band floor 68, ceiling 74. "Hold" means R2 applies.

**D1 (hot day, TOU cool bank ends at 68, AC overshoots to 67 at the start of the peak window).**

| | Before #1072 | Today (#1072) | After #1073 (D1=A) |
|---|---|---|---|
| What the furnace does | waits until below 63 F or 120 min pass | starts at once | waits until the house is below 63 F (depth > 5) or 120 min pass |

**D2 (natural side exempt; tolerance only on the against-grain side).**

| Situation | Today | After #1073 |
|---|---|---|
| Hot/warm day, AC ran 40 min ago, house falls to 67 | Heat now | Hold (heat is against the grain: tolerance 5 hot / 2 warm) |
| Warm day, furnace ran 06:30-09:00, sun pushes the house to 76 at 10:30 | Cool now | Cool now (cool is natural on a warm day: tolerance 0) |
| Cold day, furnace ran 30 min ago, overshoot to 75 | Cool now | Hold (cool is against the grain) |
| Cold morning, house 65, nothing ran for hours | Heat now | Heat now |
| Mild day, AC ran 30 min ago, you switched the thermostat Off (or it is in heat_cool / auto / just came back online), house 67 (depth 1 F, tolerance 2 F) | Heat now (the guard was skipped because the live mode is unknown; Issue #1149) | Hold: Climate Advisor arms cool at the ceiling (the AC idles, the house is below the ceiling) and does not start the furnace. Heats once the house is deeper than 2 F past the floor (below 66) or the window lapses. The mirror (furnace ran 30 min ago, house 75) holds heat. Cold day: heat is native, so it still heats at once. |
| Mild day, AC ran 30 min ago, house 66 | Heat now | Hold (depth 2 F past the floor; tolerance 2 F). Heats once the house is deeper than 2 F past the floor (below 66) or the window lapses. Mild has no native side: both sides get 2 F. Boundary: depth equal to the tolerance holds, strictly greater switches (R1/R2). |
| You cool by hand to 66 on a mild day; grace ends | Heat now | Hold (your action counts as a recent cool run, R5) |

**TOU bank-end example.** Hot day, cost window starts 16:00, cool bank holds 68 until 16:00, AC stops with indoor 67 (depth 1). At 16:00 `resolve_tou_phase()` reports NONE, the selector resumes, wants heat (floor breach), but cool ran/was commanded within 120 min and 1 <= 5, so it holds. If indoor falls to 62.9 (depth > 5) it heats at once (R1). Otherwise at 18:00 the window lapses and heat arms. Cooling resumes naturally when indoor rises to the ceiling margin.

## 6. Accepted residuals

1. **Wake-up after pre-cool.** If pre-cool ran inside the Window and the wake step raises the floor, a house up to the tolerance under the new floor waits (at most the tolerance, at most the Window). Bounded, logged, accepted. A shorter wait requires a separate decision.
2. **Bank overshoot beyond the tolerance heats.** A TOU/pre-cool overshoot deeper than `comfort_deadband_*_f` is an ordinary deep breach (R1): comfort wins, the furnace runs.
3. A wrong day classification gives the wrong natural-side exemption (D2=X makes day type an input to the tolerance size, never to which edge is armed).
4. Dwell-lock and hold can briefly leave the house up to the tolerance past an edge; this is the intended trade, surfaced by `comfort_switch_held`.

## 7. New and changed activity events

All registered in `ai_skills_context.py` (`EVENT_RENDERERS`) and catalogued in [activity-report-table.md](activity-report-table.md).

**`thermostat_mode_set`** (new): one row per actual thermostat mode write (re-affirms of the mode already set excluded; identical repeats collapse via the Dedup Contract).
Payload: `{zone, from_mode, to_mode, reason, source, indoor_f, floor_f, ceiling_f, outcome}`. `zone` is always the generic label `thermostat` (activity records end up in public issues; neither the entity id nor the friendly name is ever included).
- `reason`: a FIXED label looked up from `source` (`_MODE_SET_REASON_LABELS` in `automation.py`), never free text, because callers' reason strings can contain an entity id (door/window pause) or a TOU schedule id and activity records end up in public issues. The labels: `selector` "comfort range decision", `pause` "door or window opened", `restore` "restoring after a pause", `tou` "time-of-use schedule", `ode_guard` "predicted ceiling breach", `nat_vent` "natural ventilation", `user` "you changed it at the thermostat", `startup` "startup", `other` "other". The caller's free text is not logged by this path either.
- `source`: exactly the ring-buffer reason code of the write: `selector`, `pause`, `restore`, `tou`, `ode_guard`, `nat_vent`, `user`, `startup`, `other` (`_MODE_CHANGE_REASONS`). `startup` is reserved: there is no startup-specific mode-write path today (a restart reconcile goes through the selector and is labelled `selector`), so it is currently never emitted. Pre-cool and pre-heat writes are labelled by whichever context they run under (`tou`/`selector`/`other`), not by their own code.
- `outcome`: always `requested`. The row is written when the change is recorded, before the awaited climate call (so concurrent writes see it), so it never claims the thermostat obeyed. Confirmation or failure is shown by the existing `hvac_mode_applied_recovered` / `hvac_command_recovered` / `hvac_mode_not_applied` / `hvac_write_blocked_whf_active` rows (Issue #1049).
Emitted at the single `_set_hvac_mode` / `_ensure_hvac_mode_for_write` choke points, not per caller.

**`comfort_switch_held`** (new): emitted once per genuine hold (dedup per Dedup Contract), INFO log `Comfort edge switch held: ...` (a hold that starts or ends on a reactive per-reading tick is recorded on that tick, not at the next 30-minute pass; repeats of the same hold log at DEBUG).
Payload: `{candidate_family, zone, depth_f, tolerance_f, opposite_ran_min_ago, window_min, indoor_f, floor_f, ceiling_f}`; `zone` is `breach` (past the edge) or `margin` (inside the band, near the edge) and `depth_f` is non-negative (0.0 inside the band). Dedup: `_recent_duplicate("comfort_switch_held", (candidate, current), window_seconds=600)`, and a hold that re-enters within 10 minutes of the previous episode's end (same candidate) REOPENS that episode instead of adding a new one, so a flickering reading yields one INFO line, one event and one `comfort_holds` episode. The Debug-tab line shows the absolute expiry (`until_ts` = the moment the family last ran + window, stored when the hold starts/refreshes) and formats depth/tolerance in the user's temperature unit. A hold is cleared (episode closed, end reason `lapsed`) whenever the selector is skipped: TOU precondition, door/window pause, manual override or override confirmation, nat-vent/WHF ownership.

**`comfort_family_switch`** (changed): now also emitted when the prior thermostat mode was unknown (`from_family` = `"unknown"`); previously skipped. Payload (verified against the emitter in `_resolve_comfort_family_via_fsm`): `resolved_family`, `from_family`, `reason`, `margin_applied_f`, `indoor_f`, `floor_f`, `ceiling_f`, `breach`. (The Debug-tab dict `_last_comfort_switch` uses `from`/`to`; the ACTIVITY event key is `from_family`.)

`comfort_family_switch_locked_out` is unchanged (dwell lock). No event type is removed.

## 8. Open questions (UNVERIFIED)

1. **Resolved (Issue #1075, G9) -- unavailable -> available re-apply path.** Traced against code and locked by `tests/test_thermostat_reconnect_reapply.py` (real `_async_thermostat_changed` and real engine; no behavior change). (a) `coordinator.py :: _async_thermostat_changed` runs `_async_run_temp_reactive_checks()` when the new state's `current_temperature` attribute is not None and differs from the old one; a reconnect normally satisfies this because the `unavailable` state carried no temperature. (b) `is_availability_recovery()` (`const.py`) only gates the override-detection branches, never the reactive checks, so a reconnect is not read as an occupant override (#1049). (c) During startup coalescing (`STARTUP_COALESCE_SECONDS` = 300) `_suppress_during_startup_coalescing()` returns early, before the reactive checks. (d) `AutomationEngine._current_thermostat_family()` is None for Off/unavailable/unknown (unless CA commanded a mode within `COMFORT_RECENT_COMMAND_WINDOW_S`), so `_comfort_family_would_switch()` fires on that reactive tick only when the reading is a BREACH (outside the band); a reading inside a margin zone or in the band does not. (e) A margin-zone reading therefore waits for the coordinator's 30-minute refresh (`update_interval`), where `apply_classification()` -> `_apply_comfort_band()` writes the mode (family: the nearer edge's margin zone, else the day's `default_family`). **Net:** usually restored on the first temperature tick when the house is in breach; otherwise worst case about 30 minutes, plus up to 5 more when the reconnect happens during startup coalescing. No wrong-mode-first path was found: in the traced and tested cases, with the family unknown the reactive check either re-arms the breached edge or writes nothing. Not decided: a re-apply-on-reconnect hook to close the margin-zone wait (separate decision, not recommended now).
2. **Occupancy return on warm/mild days (G7).** `handle_occupancy_home()` restores only for heat/cool classifications (docs/08 §18 E10); warm/mild write nothing until the next cycle. Desired behavior not decided.
3. **Pre-heat (cold front) mode spec (G7).** Written as a mode writer feeding the clock; its exact interaction with the guard when the cold front arrives is not independently tested.
4. **Origin of the 5 F default** beyond #827 "configurable" is not verified.
5. **Recently-commanded 30 s window** is now `COMFORT_RECENT_COMMAND_WINDOW_S` in `const.py` (variable table row); no hard-coded copy remains.
6. **Stall detector coupling (verified, Issue #1073).** `coordinator.py` (~L10087) calls `stall_margins(_native_family(_day_type), ae._comfort_deadband_for_day_type(_day_type), ...)`, the same two helpers and the same five `comfort_deadband_*_f` keys the settling guard reads, so the stall margin and the guard tolerance cannot drift apart. `tests/test_comfort_settling_guard.py::TestR9::test_stall_detector_margins_are_unchanged_by_the_meaning_change` pins it. Nothing to re-verify.
7. **Resolved (Issue #1073, S6).** `_set_temperature_for_mode()` and every restore caller now resolve through the selector and the resolved band (`_resolved_comfort_band()`: sleep, away/vacation, aggressive savings), and override adoption uses `_override_decision_mode_and_target()`. No restore caller bypasses the resolved band. Retained here so the #1070 limitation is legible as superseded.

## 9. Documented side effects of the single "ran" source and the selector-based adoption

1. **Event-grain "ran" stamp also sharpens the #835 HVAC-fan restriction.** `AutomationEngine._hvac_fan_restriction_block_reason()` (`hvac_fan_restrict_mode`) reads `_last_hvac_cooling_active` / `_last_hvac_heating_active`. Since #1073 the coordinator's thermostat listener feeds those stamps at EVENT grain (previously a ~30-minute cycle grain), so "cooled within the last 2h" is now accurate to the minute and a restriction that used to clear up to 30 minutes late clears on time. Same stamps, same 2 h window, no new setting. Test: `TestDocumentedSideEffects::test_event_grain_ran_stamp_feeds_the_hvac_fan_restriction`.
2. **Override adoption compares to the SELECTOR's family on every day type.** `_override_matches_current_decision()` asks `_override_decision_mode_and_target()` (the selector preview on the resolved band) instead of `classification.hvac_mode`. Example: on a cold day (classification mode `heat`) with the house at 73.5 F on a 68-74 band, the selector reads "cool" (inside the cool margin), so an occupant's `heat` override is NO LONGER adopted as "matching" and stays an override until it ends; at 70 F the selector keeps heat and the same override is adopted. Test: `TestDocumentedSideEffects::test_heat_override_on_a_cold_day_near_the_ceiling_is_no_longer_adopted`.
3. **`_last_commanded_hvac_mode` means "CA-issued only".** A user's thermostat mode change is recorded in the mode-change ring buffer (reason `user`) but deliberately NOT in this field: the coordinator's override detector fires on a live mode different from `_last_commanded_hvac_mode` (or the classification mode), so if the field followed the user, their return to the original mode would read as a fresh override and the golden `override_self_resolve_transient` would become a second `override_detected`.

4. **An occupant's "off" is never adopted as "matching" on a mild day (Issue #1159).** The selector-based adoption above covers heat/cool overrides. A non-setpoint `off` override is adopted only once CA has actually paused HVAC for a door/window (`_ca_paused_hvac()`: `_paused_by_door`; a merely open sensor does not count, unlike the #1119 hold in `_override_decision_mode_and_target()`). On a mild day the classification label `off` does not mean CA plans off (it arms the band), so the Off is confirmed, kept through the manual grace and not adopted at the next refresh, even if a window opens meanwhile. Test: `tests/test_override_off_mild_day.py`.

## Code Reference

- [`decide_comfort_family`](../custom_components/climate_advisor/comfort_family_decision.py) / [`effective_margin`](../custom_components/climate_advisor/comfort_family_decision.py) / `_native_family` — the pure leaf and the helpers.
- [`comfort_family_fsm.transition`](../custom_components/climate_advisor/comfort_family_fsm.py) — dwell lock wrapper.
- [`AutomationEngine._comfort_family_inputs`](../custom_components/climate_advisor/automation.py), `_comfort_deadband_for_day_type`, `_current_thermostat_family`, `_resolve_comfort_family_via_fsm`, `comfort_family_temperature_check`, `_set_hvac_mode`, `_ensure_hvac_mode_for_write`, `_record_commanded_mode`, `_set_temperature_for_mode`, `_on_grace_expired`.
- [`coordinator._equipment_last_active`](../custom_components/climate_advisor/coordinator.py) and `_do_startup_coalesce` — run tracking and startup.
- [`decide_ode_ceiling_guard`](../custom_components/climate_advisor/ode_ceiling_guard.py) — second reader of the "ran" source.
- Constants: [`const.py`](../custom_components/climate_advisor/const.py) (`COMFORT_DEADBAND_*`, `COMFORT_FAMILY_RECENCY_WINDOW_MIN_*`, `COMFORT_MODE_SWITCH_MIN_INTERVAL_S`, `COMFORT_REACTIVE_RETRY_S`, `OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F`, `CLASSIFICATION_HYSTERESIS_F`).
