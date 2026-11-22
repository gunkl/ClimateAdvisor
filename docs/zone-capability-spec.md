<!-- Nav: ← [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md) | → [zone_capability.py](../custom_components/climate_advisor/zone_capability.py), [automation.py `_zone_capability`](../custom_components/climate_advisor/automation.py) | ↔ [comfort-edge-selection-spec.md](comfort-edge-selection-spec.md), [activity-report-table.md](activity-report-table.md), [configuration-options.md](configuration-options.md) -->

# Zone Capability — Territory Spec (Tier 3)

Issue #1699 (phase 1: the resolver, the controls and every command path). Feature request #1560; heat-only / cool-only zones #1544 (phase 2 finishes the plan, briefing, promises, incidents, chart, Comfort Score, cost and AI context on top of this resolver). Authoritative owner of: what a zone can do right now (heat, cool, both, neither), why not, and how every Climate Advisor command respects it.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant get? | A seasonal home can say "no cooling while it is cold outside", "no heating while it is warm outside" or "heat only this season". Climate Advisor then behaves exactly as for a heat-only (or cool-only) thermostat: on a near-freezing night it never arms cool because the house sits in the cooling margin of the sleep ceiling (#1560, Nov 12). | [Purpose](#1-purpose) |
| Where is the rule? | `zone_capability.py` (pure). The engine's `_zone_capability()` / `_effective_capabilities()` feed it; `_get_thermostat_capabilities()` stays the EQUIPMENT input. | [Resolver](#2-the-resolver) |
| When does a lockout engage and release? | Cool: engages at outdoor < X, releases at outdoor ≥ X + 3.6 °F (2 °C). Heat: engages at outdoor > Y, releases at ≤ Y − 3.6 °F. A missing reading keeps the state; none since startup = not engaged. In memory only. | [Hysteresis](#3-hysteresis) |
| Can a lockout leave the house too cold or too hot? | No: a policy lock (outdoor or season) steps aside while the house is deeper past that side's edge than the day's settling tolerance (`comfort_deadband_*_f`, R1: strictly greater), and locks again at ≤. Missing equipment never releases. Unknown indoor or band: stays locked. | [Safety release](#4-safety-release) |
| What happens to a write CA should not send? | Refused at the choke points (`_set_hvac_mode`, `_ensure_hvac_mode_for_write`, `_set_temperature_impl`) as `HvacCommandResult.CAPABILITY_BLOCKED`: INFO log, no event, not a failure (no #1302/#1308 re-send, no incident, no `hvac_mode_not_applied`). | [Backstop](#6-backstop) |
| Which Activity rows and Status line? | `hvac_lockout_engaged` / `hvac_lockout_released` (one per genuine change, plus `safety` rows for the release); Status: "Cooling off: outdoor 3 °C (below 16 °C)". | [Events](#8-events-status-diagnostics) |
| Which test proves what? | `tests/test_zone_capability_1699.py`. | [Test map](#test-map) |

## 1. Purpose

**What the person at home experiences.** On a cold night (outdoor 7 °C, 18–22 °C sleep band) the house sits at 21.5 °C, inside the fixed 2 °F arming margin of the 22 °C ceiling, so the comfort-edge selector arms cool (deliberate margin arming, comfort-edge spec section 1). On a single-setpoint thermostat nothing then guards the floor until the house reaches the heat margin. The occupant wanted "heating stays armed whenever it is cold outside". The same question — *what can this zone do right now?* — is also what a heat-only home (electric baseboards) needs answered consistently.

Settings (options flow only, all off by default; nothing changes for anyone who does not set them): `cool_lockout_outdoor_temp`, `heat_lockout_outdoor_temp` (optional outdoor temperatures, stored in °F like every temperature option), `hvac_season` (Auto / Heat only / Cool only, default Auto). The form rejects a heat lockout at or below the cool lockout (`lockout_overlap`).

## 2. The resolver

`resolve_zone_capability()` answers, for heat and cool: `available`, `reason` (`equipment` > `season` > `outdoor_lockout`, the first thing that removes it), `policy_locked`, `safety_released`, `depth_f` and `since` (when the policy state last changed). Equipment is what is possible (the thermostat's `hvac_modes`; a thermostat that advertises neither heat nor cool is unknown and counts as both, the pre-#1699 behaviour); the policies only narrow it, never widen it.

`_effective_capabilities()` masks the `ThermostatCapabilities`: an unavailable family's mode is removed, and the band modes (`heat_cool`, `auto`) go too while either side is unavailable, so the thermostat cannot run the locked side by itself. `mode_allowed(mode)`: heat / cool need their family; a band mode needs both; every other mode (off, fan_only, dry) always may. `bank_allowed(family)`: available AND not policy-locked (a bank is savings, never released by the safety rule).

### State matrix (the #1699 contract)

| Zone state | House inside the band | Near / past the ceiling | Near / past the floor | Outdoor missing |
|---|---|---|---|---|
| Both available (today) | nearest edge armed, as now | cool armed | heat armed | unchanged |
| Heat-only (equipment) | heat armed | advisory, not a CA failure (no cool write; phase 2: wording) | heat armed | n/a |
| Cool-only (equipment) | cool armed | cool armed | advisory, not a CA failure | n/a |
| Cool locked out (outdoor / season) | heat armed | advisory with reason; cool runs past the tolerance (safety release) | heat armed | keep last state |
| Heat locked out (outdoor / season) | cool armed | cool armed | advisory with reason; heat runs past the tolerance | keep last state |
| Lockout engages while the locked side is armed | switch to the other side at once (the classification re-runs; the dwell lock does not apply when leaving an unavailable family), or Off through the existing "no capable mode" path if the zone has neither | | | |
| Neither side available (e.g. heat-only equipment + season Cool only) | `_apply_comfort_band` "no capable mode for active=…" (INFO), nothing armed | same | same | n/a |
| Indoor or band unknown | single-family zone: that family (the leaf decides it before the missing-input check, #1489); policy locks stay locked | | | |

## 3. Hysteresis

`step_cool_lockout(engaged, outdoor_f, threshold_f)` and its mirror `step_heat_lockout`. Stepped on every outdoor reading by `AutomationEngine.update_outdoor_temp()` (called from `coordinator._apply_outdoor_temp`, the one place every outdoor reading lands: the 30-minute cycle, the 5-minute tick and the outdoor sensor listener). The state lives in memory only (`_cool_lockout_engaged`, `_heat_lockout_engaged`); no stored-schema change. After a restart there is no lockout until the first outdoor reading, which normally arrives in the first update cycle.

## 4. Safety release

Owner decision 2026-11-22. While the house is deeper past the locked side's edge than the tolerance, that side is available: cool on `indoor − ceiling > tolerance`, heat on `floor − indoor > tolerance`. Tolerance = `AutomationEngine._comfort_deadband_for_day_type(day_type)` (the raw day value, also on the day's native side). The band is the one the caller is deciding on (the selector's floor/ceiling; elsewhere the resolved band in force). It applies to the outdoor lockouts and the season; never to missing equipment; with an unknown indoor reading, band or tolerance the lock stays. This keeps #821/#823 (a lockout that left the floor unguarded) from coming back.

## 5. Every command path

| Caller | Gate |
|---|---|
| Selector (`_comfort_family_inputs` → `decide_comfort_family`, incl. the unknown-family / hysteresis-gap fallback, the reactive check, the ODE preview and override adoption) | Effective capabilities on the decision's band. The leaf's single-family answer comes before the missing-input check. The FSM skips the dwell lock when leaving an unavailable family. |
| `_apply_comfort_band` | Effective capabilities on the band; neither side = "no capable mode". |
| `_set_temperature_for_mode` (every comfort restore) | Through the selector; backstop on the write. |
| Blind mode restores: door/window close (`_pre_pause_mode`), dashboard resume (band modes), whole-house fan release and the stranded-suppression restore (`_pre_fan_hvac_mode`), the #1308 restore re-send without a classification | `_zone_mode_allowed()`; a locked snapshot is not replayed: the comfort selector restores instead (`_restore_hvac_mode_gated`) or the selector write that follows picks the mode. |
| TOU banks (Home/Guest `apply_tou_precondition`, Away/Vacation `apply_tou_away_vacation_precondition`, the #1409 occupancy-change bank) | `_zone_bank_allowed(family)`: suppressed (INFO), and `_tou_precondition_active()` is False so the selector keeps the band. |
| Overnight pre-cool `handle_pre_cool` | `_zone_bank_allowed("cool")` → `skipped_cool_unavailable`. |
| ODE ceiling guard | `_zone_mode_allowed("cool")` on the last band and the decision's indoor reading → write skipped (INFO). |
| Setpoint retries / nudges, post-fan verify, remote-sensor offset | Backstop (the remote offset loop treats `CAPABILITY_BLOCKED` as "nothing to do", no back-off warning). |
| Off writes (pauses, nat-vent, blower) | Never gated: Off is always allowed. |

The occupant's own thermostat changes are never reversed because of a lockout; manual override precedence is unchanged.

## 6. Backstop

`_refuse_capability_blocked(mode, reason)` at the three write choke points refuses heat / cool / band-mode writes the zone cannot use now (safety release included). It runs before any stamp (`_write_seq`, the commanded-mode ring buffer, verify, retry). Result `CAPABILITY_BLOCKED`: `hvac_outcome_label` = `not_sent`, `_mode_write_record` held reason `no_capable_mode`; `_schedule_hvac_resend` / `_hold_restore_until_landed` act only on `FAILED` / `UNCONFIRMED`, so no re-send chain. One INFO line per (mode, reason) per 10 minutes, then DEBUG.

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

## 10. Not in phase 1 (phase 2 / #1544)

The day plan and timeline, briefing, promises, incidents and compliance, Comfort Score, chart band, cost estimate and AI context still read the equipment / classification and may describe the locked side; the chart's TOU bank and pre-cool band are not yet masked. The unserved-edge advisory wording ("no cooling equipment" vs "Cooling off: …") beyond the Status line is phase 2.

## Test Map

| Contract | Test (`tests/test_zone_capability_1699.py`) |
|---|---|
| Hysteresis, missing reading, never known | `TestCoolLockoutHysteresis`, `TestHeatLockoutHysteresis` |
| Every state-table row, safety boundary (== stays locked, > releases), season, equipment + season = nothing, band modes | `TestStateTable` |
| Overlap rejected | `test_overlapping_thresholds_are_rejected`, `test_options_reject_a_heat_lockout_at_or_below_the_cool_lockout` |
| Leaf single family without indoor; FSM dwell bypass | `test_leaf_single_family_needs_no_indoor_reading`, `test_fsm_leaving_an_unavailable_family_ignores_the_dwell_lock` |
| Backstop: quiet, not failed, nothing sent, no re-send | `TestBackstop` |
| TOU banks and pre-cool suppressed | `TestBanksSuppressed` |
| Events once per change, Status line, diagnostics | `TestEventsStatusDiagnostics` |
| #1560 Nov 12 replay: no cool written, heat armed | `TestNov12Replay` |
| Lock engages while cool armed: heat at once | `test_lock_engaging_while_cool_is_armed_switches_to_heat_at_once` |
| Heat lockout, house past the tolerance: heat runs, then locks again | `test_heat_lockout_warm_day_cold_house_heats_past_the_tolerance_then_locks_again` |
| Occupant change not reversed | `test_occupant_cool_change_is_not_reversed_by_the_lockout` |
| Options stored in °F, blank clears, off by default | `test_options_*` |
