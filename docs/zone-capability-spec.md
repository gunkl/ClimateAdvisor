<!-- Nav: ← [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md) | → [zone_capability.py](../custom_components/climate_advisor/zone_capability.py), [automation.py `_zone_capability`](../custom_components/climate_advisor/automation.py) | ↔ [comfort-edge-selection-spec.md](comfort-edge-selection-spec.md), [activity-report-table.md](activity-report-table.md), [configuration-options.md](configuration-options.md) -->

# Zone Capability — Territory Spec (Tier 3)

Issue #1699 (phase 1: the resolver, the controls and every command path). Feature request #1560; heat-only / cool-only zones #1544 (phase 2, done in 0.9.75: the plan, briefing, promises, incidents, chart, Comfort Score, cost and AI context read this resolver through `zone_service()`, section 10). Authoritative owner of: what a zone can do right now (heat, cool, both, neither), why not, and how every Climate Advisor command respects it.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant get? | A seasonal home can say "no cooling while it is cold outside", "no heating while it is warm outside" or "heat only this season". Climate Advisor then behaves exactly as for a heat-only (or cool-only) thermostat: on a near-freezing night it never arms cool because the house sits in the cooling margin of the sleep ceiling (#1560, Nov 12). | [Purpose](#1-purpose) |
| Where is the rule? | `zone_capability.py` (pure). The engine's `_zone_capability()` / `_effective_capabilities()` feed it; `_get_thermostat_capabilities()` stays the EQUIPMENT input. | [Resolver](#2-the-resolver) |
| When does a lockout engage and release? | Cool: engages at outdoor < X, releases at outdoor ≥ X + 3.6 °F (2 °C). Heat: engages at outdoor > Y, releases at ≤ Y − 3.6 °F. A missing reading keeps the state; none since startup = not engaged. In memory only. | [Hysteresis](#3-hysteresis) |
| Can a lockout leave the house too cold or too hot? | No: a policy lock (outdoor or season) steps aside once the house is deeper past that side's edge than the day's settling tolerance (`comfort_deadband_*_f`, R1: strictly greater), and locks again only when the house is back at the edge (depth ≤ 0) and the mode-switch dwell allows it (no chatter). Missing equipment never releases. Unknown indoor or band: stays locked. | [Safety release](#4-safety-release) |
| What happens to a write CA should not send? | Refused at the choke points (`_set_hvac_mode`, `_ensure_hvac_mode_for_write`, `_set_temperature_impl`) as `HvacCommandResult.CAPABILITY_BLOCKED`: INFO log, no event, not a failure (no #1302/#1308 re-send, no incident, no `hvac_mode_not_applied`). | [Backstop](#6-backstop) |
| Which Activity rows and Status line? | `hvac_lockout_engaged` / `hvac_lockout_released` (one per genuine change, plus `safety` rows for the release); Status: "Cooling off: outdoor 3 °C (below 16 °C)". | [Events](#8-events-status-diagnostics) |
| What do the plan, briefing, promises and judges count on (phase 2)? | `AutomationEngine.zone_service()` (`zc.ZoneService`): served = available AND not policy-locked (`bank_allowed`); a safety release is never planned or promised. The unserved side is advice; a breach of it is outside what the equipment (or lockout) allows. | [Phase 2 surfaces](#10-phase-2-1544-every-surface) |
| Which test proves what? | `tests/test_zone_capability_1699.py` (phase 1), `tests/test_zone_surfaces_1544.py` and `tools/zone_matrix.py` (phase 2). | [Test map](#test-map) |

## 1. Purpose

**What the person at home experiences.** On a cold night (outdoor 7 °C, 18–22 °C sleep band) the house sits at 21.5 °C, inside the fixed 2 °F arming margin of the 22 °C ceiling, so the comfort-edge selector arms cool (deliberate margin arming, comfort-edge spec section 1). On a single-setpoint thermostat nothing then guards the floor until the house reaches the heat margin. The occupant wanted "heating stays armed whenever it is cold outside". The same question — *what can this zone do right now?* — is also what a heat-only home (electric baseboards) needs answered consistently.

Settings (options flow only, all off by default; nothing changes for anyone who does not set them): `cool_lockout_outdoor_temp`, `heat_lockout_outdoor_temp` (optional outdoor temperatures, stored in °F like every temperature option), `hvac_season` (Auto / Heat only / Cool only, default Auto). The form rejects a heat lockout less than 2 °C (3.6 °F) above the cool lockout (`lockout_overlap`): with the release hysteresis, a closer pair could have both engaged at once.

## 2. The resolver

`resolve_zone_capability()` answers, for heat and cool: `available`, `reason` (`equipment` > `season` > `outdoor_lockout`, the first thing that removes it), `policy_locked`, `safety_released`, `depth_f` and `since` (when the policy state last changed). Equipment is what is possible (the thermostat's `hvac_modes`; a thermostat that advertises neither heat nor cool is unknown and counts as both, the pre-#1699 behaviour); the policies only narrow it, never widen it.

`_effective_capabilities()` masks the `ThermostatCapabilities`: an unavailable family's mode is removed, and the band modes (`heat_cool`, `auto`) go too while either side is unavailable, so the thermostat cannot run the locked side by itself. `mode_allowed(mode)`: heat / cool need their family; an advertised band mode (heat_cool / auto) stays allowed on equipment alone (on a heat-only Tado / Netatmo `[off, heat, auto]`, `auto` is the schedule mode) and is refused only while a policy lock is engaged on either side; every other mode (off, fan_only, dry) always may. With no policy lock, `_effective_capabilities()` is the equipment's own capabilities, unchanged. `bank_allowed(family)`: available AND not policy-locked (a bank is savings, never released by the safety rule).

### State matrix (the #1699 contract)

| Zone state | House inside the band | Near / past the ceiling | Near / past the floor | Outdoor missing |
|---|---|---|---|---|
| Both available (today) | nearest edge armed, as now | cool armed | heat armed | unchanged |
| Heat-only (equipment) | heat armed | advisory, not a CA failure (no cool write; phase 2: advice wording, no plan step or incident) | heat armed | n/a |
| Cool-only (equipment) | cool armed | cool armed | advisory, not a CA failure | n/a |
| Cool locked out (outdoor / season) | heat armed | advisory with reason; cool runs past the tolerance (safety release) | heat armed | keep last state |
| Heat locked out (outdoor / season) | cool armed | cool armed | advisory with reason; heat runs past the tolerance | keep last state |
| Lockout engages while the locked side is armed | switch to the other side at once (the classification re-runs; the dwell lock does not apply when leaving an unavailable family), or Off through the existing "no capable mode" path if the zone has neither | | | |
| Neither side available (e.g. heat-only equipment + season Cool only, cool-only equipment + cool lockout) | `_apply_comfort_band` "no capable mode" (INFO); a thermostat still in a mode the zone cannot use is switched Off (`_off_when_zone_has_neither_family`, Off is never gated); already Off: nothing written | same | same | n/a |
| Indoor or band unknown | single-family zone: that family (the leaf decides it before the missing-input check, #1489); policy locks stay locked | | | |

## 3. Hysteresis

`step_cool_lockout(engaged, outdoor_f, threshold_f)` and its mirror `step_heat_lockout`. Stepped on every outdoor reading by `AutomationEngine.update_outdoor_temp()` (called from `coordinator._apply_outdoor_temp`, the one place every outdoor reading lands: the 30-minute cycle, the 5-minute tick and the outdoor sensor listener). The state lives in memory only (`_cool_lockout_engaged`, `_heat_lockout_engaged`); no stored-schema change. After a restart there is no lockout until the first outdoor reading, which normally arrives in the first update cycle.

## 4. Safety release

Owner decision 2026-11-22; hysteresis from the phase-1 review. `next_safety_release(released_before, depth, tolerance, relock_allowed)`: a locked side is released once the house is deeper past its edge than the tolerance (cool: `indoor − ceiling > tolerance`; heat: `floor − indoor > tolerance`), stays released while the house is still past the edge (depth > 0), and locks again at depth ≤ 0 once the mode-switch dwell (`comfort_mode_switch_min_interval_s`, since the newest mode change) allows a routine switch. Before the hysteresis a house hovering at edge + tolerance flipped cool and heat on every reading (both directions bypassed the dwell). Tolerance = `AutomationEngine._comfort_deadband_for_day_type(day_type)` (the raw day value, also on the day's native side). It applies to the outdoor lockouts and the season; never to missing equipment; with an unknown indoor reading, band or tolerance the lock stays.

**Who decides.** The release is a recorded state per family (`_zone_safety_released`, in memory). Only the comfort-edge selector moves it (`_comfort_family_inputs` → `_zone_capability(decide=True)`), measuring depth against the band the selector decides on: the schedule band (home, sleep, Away, Vacation), never a TOU or pre-cool bank target (a held pre-cool band is dropped while cool is locked, below). Every other reader (the write backstop, the band write, banks, the ODE gate, diagnostics, Status) uses the recorded decision, so a write the selector released is never refused against a different band, and diagnostics never move it. A policy lock that engages or ends resets the family to locked. Safety rows are announced only from the selector path.

**Pre-cool hold.** `_hold_pre_cool_band()` returns the plain sleep band and ends the held pre-cool (`_pre_cool_band_in_force` cleared) while cool is not bank-allowed, so the house is not cooled toward the banking target through a cool lockout or the Heat-only season.

## 5. Every command path

| Caller | Gate |
|---|---|
| Selector (`_comfort_family_inputs` → `decide_comfort_family`, incl. the unknown-family / hysteresis-gap fallback, the reactive check, the ODE preview and override adoption) | Effective capabilities on the decision's band. The leaf's single-family answer comes before the missing-input check. The FSM skips the dwell lock when leaving an unavailable family. |
| `_apply_comfort_band` | Effective capabilities on the band; neither side = "no capable mode". |
| `_set_temperature_for_mode` (every comfort restore) | Through the selector; backstop on the write. |
| Blind mode restores: door/window close (`_pre_pause_mode`), dashboard resume (band modes), whole-house fan release and the stranded-suppression restore (`_pre_fan_hvac_mode`), the #1308 restore re-send without a classification | `_zone_mode_allowed()`; a locked snapshot is not replayed: the comfort selector restores instead (`_restore_hvac_mode_gated`, which records the mode it actually wrote in `_last_gated_restore_mode`; the stranded-suppression row names that mode, and none when nothing was written) or the selector write that follows picks the mode. |
| TOU banks (Home/Guest `apply_tou_precondition`, Away/Vacation `apply_tou_away_vacation_precondition`, the #1409 occupancy-change bank) | `_zone_bank_allowed(family)`: suppressed (INFO once per schedule and day, then DEBUG, since the 5-minute bank check asks again), and `_tou_precondition_active()` is False so the selector keeps the band. In the coordinator `_tou_bank_owns_setpoint()` and `_tou_bank_in_force()` are False for a locked family, so the 30-minute cycle writes the band edge instead of skipping it for a bank that never comes, and `current_hold` shows the schedule band. |
| Overnight pre-cool `handle_pre_cool` | `_zone_bank_allowed("cool")` → `skipped_cool_unavailable`. |
| ODE ceiling guard | `_zone_mode_allowed("cool")` on the last band and the decision's indoor reading → write skipped (INFO). |
| Setpoint retries / nudges, post-fan verify, remote-sensor offset | Backstop (the remote offset loop treats `CAPABILITY_BLOCKED` as "nothing to do", no back-off warning). |
| Off writes (pauses, nat-vent, blower) | Never gated: Off is always allowed. |

The occupant's own thermostat changes are never reversed because of a lockout; manual override precedence is unchanged.

## 6. Backstop

`_refuse_capability_blocked(mode, reason)` at the three write choke points refuses heat / cool / band-mode writes the zone cannot use now (the recorded safety release included). In observe-only it is not consulted: those writes answer `DRY_RUN` first, so the #1300 / #1306 "nothing was sent" checks keep working. With automation on, a restore whose every write came back `CAPABILITY_BLOCKED` starts no automation grace (nothing was sent). The dashboard Resume's manual grace is the occupant's own and is unchanged. It runs before any stamp (`_write_seq`, the commanded-mode ring buffer, verify, retry). Result `CAPABILITY_BLOCKED`: `hvac_outcome_label` = `not_sent`, `_mode_write_record` held reason `no_capable_mode`; `_schedule_hvac_resend` / `_hold_restore_until_landed` act only on `FAILED` / `UNCONFIRMED`, so no re-send chain. One INFO line per (mode, reason) per 10 minutes, then DEBUG.

## 7. Re-run on change

`update_outdoor_temp()` returns True when a family's policy lock engaged or released; `coordinator._apply_outdoor_temp` then requests a refresh (the same call `climate_advisor.force_reclassify` makes), so a cool armed as the cool lockout engages switches to heat within the same minute instead of the next 30-minute cycle.

## 8. Events, Status, diagnostics

- `hvac_lockout_engaged` / `hvac_lockout_released`: `{family, reason, outdoor_f, threshold_f, season}` from `_announce_zone_policy()` (on each outdoor reading and at the start of every `apply_classification()`, which also announces the season after a restart). The safety release adds rows from `_announce_zone_safety()` with `{family, reason, safety: true, depth_f, outdoor_f}`. One row per genuine change; dedup `(family, reason[, "safety"])` within 600 s. A family the equipment cannot run is tracked but not announced. Catalogued in [activity-report-table.md](activity-report-table.md).
- Status: `zone_lockout_status` (API, with `_msg`), one line on the Status card while a policy lock is active: `status.zone_lockout.{cool,heat}_outdoor` ("Cooling off: outdoor {outdoor} (below {threshold})", user's unit) or `..._season`.
- Diagnostics: `zone_capability` section (`zone_capability_snapshot()`: effective availability and reasons per family, season, thresholds, engaged flags, last outdoor). `thermostat_snapshot` and the thermostat profile keep the equipment capabilities.

## 9. Variables

| Variable | Default | Unit | Range | Owner | Meaning |
|---|---|---|---|---|---|
| `cool_lockout_outdoor_temp` | unset (off) | °F stored, user's unit shown | −20–110 °F / −30–45 °C | `const.py` `CONF_COOL_LOCKOUT_OUTDOOR_TEMP` | No cooling while outdoor is below it |
| `heat_lockout_outdoor_temp` | unset (off) | °F stored | same; must be above the cool lockout | `CONF_HEAT_LOCKOUT_OUTDOOR_TEMP` | No heating while outdoor is above it |
| `hvac_season` | `auto` | — | `auto` / `heat_only` / `cool_only` | `CONF_HVAC_SEASON`, `DEFAULT_HVAC_SEASON` | Removes one side for the season |
| `LOCKOUT_RELEASE_HYSTERESIS_F` | 3.6 | F | constant | `zone_capability.py` | Release point past the threshold (2 °C) |
| Safety-release tolerance | `comfort_deadband_*_f` | F | see comfort-edge spec | `AutomationEngine._comfort_deadband_for_day_type()` | Depth past the edge that releases a policy lock |

## 10. Phase 2 (#1544): every surface

**Occupant first:** a heat-only home (electric baseboards, #1348) on a hot day is never promised AC or a pre-cool, never planned with an AC run and never told Climate Advisor failed to keep it cool; it gets windows, blinds and ventilation advice. A cool-only home gets "close up, the sun is your heat". A seasonal home with cooling locked out (outdoor lockout, Heat-only season) is treated the same way, with the lockout named as the reason.

**One source, two views.** `AutomationEngine.zone_service()` -> `zc.service_from(self._zone_capability())` (recorded state; never moves the safety release). `ZoneService(heat, cool, heat_reason, cool_reason, defended_heat, defended_cool)`:
- **served** (`serves`, `heat` / `cool`) = `bank_allowed(family)`: PLANNING and PROMISING (plan, briefing, chart band, banks, Next Automation). A safety release is never planned or promised.
- **defended** (`defends`) = `zone.family(f).available`, the safety release included: JUDGING (incidents, Comfort Score, the stall alert, P7, the AI cross-validation line). A release that runs and still fails to hold the edge is a real failure (review finding, owner decision 1 narrowed to planning). `released(f)` = defended but not served: the plan's and the chart's CURRENT hour show that side running; later hours stay unserved.
- **day-level** (`coordinator.day_zone_service()`): the briefing, the day mode it shows and the daily record's `hvac_mode_recommended` project an OUTDOOR lockout over the rest of today's hourly forecast (`step_cool_lockout` / `step_heat_lockout`, same hysteresis); a lockout the forecast releases today counts as served for the day (a desert summer: locked at a 57 °F dawn, released by the 98 °F afternoon). Equipment and the season are not projected.

 `shown_mode(mode)`: both served -> unchanged; one -> that family (the single-family leaf arms it whatever the day says); none -> off. The coordinator reads it as `coordinator.zone_service()` (both families when there is no engine or it cannot answer). No display surface reads `_get_thermostat_capabilities()` (that stays the equipment input for diagnostics and the thermostat profile).

| Surface | Where | Rule |
|---|---|---|
| Day plan / timeline (#1542) | `coordinator._timeline_snapshot` -> `can_heat` / `can_cool` / `released_now`; `operation_timeline.zone_masked` (in `build_timeline` and `TimelineLatch.update`); `PlanSignature` carries `can_heat` / `can_cool` (reason `zone`) | Day modes shown; no pre-cool, ceiling guard, unserved TOU bank or away widening. Window periods are NOT gated on the families: they are the unserved side's advice (a heat-only hot evening is told to open up; the benefit gate counts the discomfort saved). [operation-timeline-spec](operation-timeline-spec.md#zone-capability-issue-1542) |
| Night strategy | `coordinator._timeline_night_plan` | `can_heat` / `can_cool` from the snapshot |
| Briefing (#1488) | `briefing.generate_briefing(zone_service=)`, `coordinator._build_briefing_text` | Advisory bodies and TLDR row with the reason. [briefing-spec](briefing-spec.md#zone-capability-issue-1488) |
| Classifier day mode shown | briefing (above); daily record `hvac_mode_recommended` (Analysis "HVAC mode"); degraded Next Automation | `shown_mode` |
| TOU bank / pre-cool | `_tou_precondition_window_tuple` (chart band, predicted curve) None for an unserved bank; Status TOU line gated by `_tou_bank_family_allowed`; `_maybe_schedule_pre_cool` schedules nothing without cooling (asks again next cycle); a fired trigger answering `skipped_cool_unavailable` clears the Status line; `_shown_pre_cool_status` hides a "scheduled" line while cooling is unserved | No bank or pre-cool shown that will not run |
| ODE ceiling guard | Plan step via `ceiling_breach` (masked); degraded Next Automation "AC holds the ceiling" needs cooling; the chart's forward regime draws an unserved family's hour as off (the current hour shows a released side running) | The CONTROL guard reads the masked (honest) curve but asks `_zone_mode_allowed("cool")` before it logs a breach or writes (moved ahead of the breach log in the review), so a zone that cannot cool gets no breach INFO line and no write |
| Predicted indoor curve | `_build_predicted_indoor_future(zone_service=)` (every coordinator caller) | An unserved day mode is passive drift; the nearest-edge heat override needs heat; no pre-cool target without cooling |
| Chart band | `get_chart_data`: `band_advisory` `{family, reason}`; `defense_lines` None on the unserved side; `chart_v2.js` draws that edge sparser and names it ("Target Band (cooling edge advisory: no equipment)") | Advisory, never a promise |
| Incidents / compliance / Comfort Score | `_is_tolerated_comfort_deviation` | Past an edge the zone does not DEFEND: tolerated (no incident, no minutes); a released side is judged. [incident-classes](incident-classes.md#unserved-edge-issue-1544) |
| Stall alert | `_update_comfort_stall` -> `decide_comfort_stall(defended=)` | A direction the zone does not defend is `excluded_by="zone_not_defended"` (season Cool only on a cold day: no "house is cold, nothing running" push) |
| Promises (P7) | `Reading.serves_heat` / `serves_cool` = the defended view (production and `PromiseProbe`), `check_p7_comfort_kept` | Not judged past an edge the zone does not defend. [household-promises-spec](household-promises-spec.md#p7-and-an-edge-the-zone-does-not-serve-issue-1544) |
| Next User Action | `_compute_next_action` | No heating served and a window open: "Close windows and doors to keep the warmth in", never "help / enable the heater" |
| AI context | `ai_skills_context.build_hvac_entity_context` (`zone_service:` line), `build_state_cross_validation_context` ("advisory edge", never `[FLAG]`) | |
| Cost estimate | `learning.estimate_daily_cost` | Verified, unchanged: measured per-family minutes; an unserved family runs 0 minutes and needs no power rating |
| Status lockout line | `zone_lockout_status` (phase 1) | Unchanged: policy locks only; a heat-only zone gets no permanent Status line (the chart band and briefing carry "no equipment") |

**Matrix (the #1544 method, made permanent).** `tools/zone_matrix.py` runs every golden through the real engine (headless harness, the coordinator where the scenario uses one) as `[off, heat]`, `[off, cool]`, and a heat-and-cool thermostat with the season Heat only, the season Cool only, and an outdoor cool lockout no forecast releases (a run where no outdoor reading ever arrives has no lockout and is marked `not_locked`), plus the golden as authored (`both`, the positive window baseline). Any failure building a surface is the run's error, never "not checked"; the test asserts a plan was judged for every coordinator scenario with a classification and both briefing variants (full and TLDR) for every scenario with one, and that a free-cooling (free-heating) window the `both` run is offered is still offered heat-only (cool-only). It asserts: no `climate.set_hvac_mode` / `set_temperature` naming a missing mode and no dual setpoint on a single-mode device (devices only: the season run's safety release may cool by design); no plan step or plan day in the missing family, no pre-cool / ceiling guard (heat-only) or pre-heat (cool-only); no briefing line promising the missing family. The golden files are not edited and their own assertions are not judged there. `tests/test_zone_surfaces_1544.py::test_matrix_no_surface_names_a_missing_mode` runs the full set in the unit tier (one case per golden).

## Test Map

| Contract | Test (`tests/test_zone_capability_1699.py`) |
|---|---|
| Hysteresis, missing reading, never known | `TestCoolLockoutHysteresis`, `TestHeatLockoutHysteresis` |
| Every state-table row, safety boundary (== stays locked, > releases), season, equipment + season = nothing, band modes | `TestStateTable` |
| Overlap rejected (2 °C gap) | `test_overlapping_thresholds_are_rejected`, `test_options_reject_a_heat_lockout_at_or_below_the_cool_lockout`, `test_options_reject_a_heat_lockout_inside_the_hysteresis` |
| Safety-release hysteresis, dwell on re-lock, no chatter | `TestSafetyReleaseHysteresis` |
| Recorded decision: backstop trusts the selector, diagnostics never move it, policy change resets | `TestRecordedDecision` |
| Held pre-cool ends when cool locks | `TestPreCoolHold` |
| Neither family: thermostat switched Off | `TestBackstop::test_zone_with_neither_family_turns_the_thermostat_off` |
| Observe-only DRY_RUN first; refused restore starts no grace | `TestObserveOnlyAndGrace` |
| Advertised `auto` on a heat-only unit | `TestBandModes` |
| Restore records what was written | `TestGatedRestoreRecord`, `test_stranded_restore_records_the_mode_actually_written` |
| Coordinator re-run, TOU ownership, log once | `test_coordinator_*`, `test_tou_suppression_logged_once_per_window` |
| Leaf single family without indoor; FSM dwell bypass | `test_leaf_single_family_needs_no_indoor_reading`, `test_fsm_leaving_an_unavailable_family_ignores_the_dwell_lock` |
| Backstop: quiet, not failed, nothing sent, no re-send | `TestBackstop` |
| TOU banks and pre-cool suppressed | `TestBanksSuppressed` |
| Events once per change, Status line, diagnostics | `TestEventsStatusDiagnostics` |
| #1560 Nov 12 replay: no cool written, heat armed | `TestNov12Replay` |
| Lock engages while cool armed: heat at once | `test_lock_engaging_while_cool_is_armed_switches_to_heat_at_once` |
| Heat lockout, house past the tolerance: heat runs, then locks again | `test_heat_lockout_warm_day_cold_house_heats_past_the_tolerance_then_locks_again` |
| Occupant change not reversed | `test_occupant_cool_change_is_not_reversed_by_the_lockout` |
| Options stored in °F, blank clears, off by default | `test_options_*` |
| Phase 2: every surface, per family and for a policy lock; the golden matrix | `tests/test_zone_surfaces_1544.py` |
