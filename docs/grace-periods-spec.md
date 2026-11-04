<!-- Nav: ← [07-AUTOMATION-FLOWCHART.md](07-AUTOMATION-FLOWCHART.md) | → [automation.py](../custom_components/climate_advisor/automation.py) + [coordinator.py](../custom_components/climate_advisor/coordinator.py) | ↔ [state-persistence.md](state-persistence.md) -->

# Grace Period State Machine — Territory Spec (Tier 3)

## Anchors

| Question | Short answer (≤2 sentences) | → Full answer |
|---|---|---|
| What triggers a manual grace period vs an automation grace period? | Manual grace starts after a user-initiated HVAC change (thermostat override during pause, fan manual change, or dashboard resume). Automation grace starts whenever Climate Advisor itself resumes HVAC after a door/window closes or after natural ventilation ends -- only when that restore was actually sent: observe-only (automation off) starts none (Issue #1300). In observe-only a dashboard Resume clears the pause but starts no grace either (Issue #1306). | [§ Grace Period Types](#grace-period-types) |
| How long does each grace type last by default, and can it be configured? | Manual grace defaults to 1800 s (30 min); automation grace defaults to 300 s (5 min). Both durations are configurable via `manual_grace_seconds` and `automation_grace_seconds` in config; a value of 0 disables grace entirely. | [§ Grace Period Types](#grace-period-types) |
| What does an active grace period suppress? | A door or window opening during an active grace that answers to the OCCUPANT (a dashboard-resume manual grace, with no thermostat override behind it) does not pause HVAC unless a fan session would start. Climate Advisor's OWN grace (automation source: window-close resume, nat-vent exit resume) never holds the pause (Issue #1142, rule 3: the only exception is a manual override): the window gets the same answer as outside a grace -- start nat-vent when the nat-vent FSM says so, otherwise pause. | [§ Manual Grace — What It Suppresses](#manual-grace) |
| When a grace timer fires, what are the three possible outcomes? | (1) Within planned window period → clear grace silently. (2) A sensor is still open → clear grace flags, then schedule `_re_pause_for_open_sensor()`. (3) All sensors closed → clear grace flags, optionally send notification. | [§ Timer Lifecycle — Expiry Callback](#timer-lifecycle) |
| Is grace state persisted across HA restarts? | Mostly no (Issue #282 — clean slate), with two narrow exceptions (Issue #1006, Issue #1045). Override state (`_manual_override_active`, `_override_confirm_pending`) and pause state (`_paused_by_door`, `_pre_pause_mode`) are never restored — `restore_state()` deliberately leaves both at `__init__`'s clean defaults; the door/window listener and `_do_startup_coalesce()` (Issue #321/#523) re-derive pause state from the sensor's live state within the first 5 minutes. As of Issue #1006, a **`fan_off`-triggered manual grace with remaining duration IS re-armed** after the clean-slate reset, via `_maybe_restore_fan_off_grace()` — confirmed live 2026-09-28 that a restart landing minutes into a 3-hour fan-off grace silently discarded it and let nat-vent reactivate the fan 5 minutes later with zero memory of the user's action. Scoped narrowly to `fan_off` only (the one manual trigger that never sets `_manual_override_active`/`_fan_override_active`). As of Issue #1045, a persisted **`fan_manual_override`** manual grace is *stashed* at restore and re-armed (override + grace together, via `handle_fan_manual_override()`) by the startup fan reconcile when the fan is still physically running — see [§ Fan Manual-Override Restart Survival](#fan-manual-override-restart-survival-issue-1045). `dashboard_resume` and `override_confirmed` are NOT restored (Issue #1046, closed not planned); automation-sourced 300 s graces stay clean-slate by decision. | [§ Pre-Pause Mode Storage — HA Restart](#pre-pause-mode-storage), [§ Fan-Off Grace Restart Persistence (Issue #1006)](#fan-off-grace-restart-persistence-issue-1006), [§ Fan Manual-Override Restart Survival (Issue #1045)](#fan-manual-override-restart-survival-issue-1045) |
| Climate Advisor wrote a setpoint and the cloud thermostat reports it 30-120 s later -- does that start a manual override and a 1 h pause? | No (Issue #1232). The person at home is not blamed for the house's own write. Climate Advisor remembers each setpoint it writes for 180 s (`OWN_WRITE_ECHO_WINDOW_S`); a thermostat setpoint change that carries the write's HA Context, or equals its value after rounding to the thermostat's step, is that write coming back and is not an override. A different value, or the same value after 180 s, is still judged as a possible occupant change. An occupant who dials in exactly the value CA just wrote, with no Context to separate them, is read as the echo (nothing to override: the plan already holds that temperature); a change made by a signed-in person (HA Context with a `user_id`) never is. The 30 s time guard stays as a backstop. | [Setpoint echo recognition by value](#setpoint-echo-recognition-by-value-issue-1232) |
| What happens to a thermostat or fan change the occupant makes in the first 5 minutes after a restart? | Nothing is detected: override detection is suppressed for the whole startup coalescing window and the day's plan is applied when it ends. This is intended (owner decision, Issue #1235) — changes during the window are not supported, and tracking them would turn restart artifacts into overrides again (#321). Just after the window, the ordinary echo guards apply: a mode change within 3 s, or a setpoint change within 30 s, of CA's two coalesce-end writes (t = 0 and about +10 s) is not detected either (and, since Issue #1232, neither is a setpoint change to the value CA wrote, for 180 s). | [08 §11 Startup Coalescing Window](08-COMPUTATION-REFERENCE.md#startup-coalescing-window--occupant-changes-are-not-supported-owner-decision-issue-1235) |
| What happens to an active grace period when occupancy changes? | Only leaving home with an override ends a grace (Issue #1222): when `handle_occupancy_away()` / `handle_occupancy_vacation()` clear a manual override (or a pending confirmation), the override-protecting grace behind it ends with it, through `_end_grace_left_protecting_nothing()` (`grace_cancelled`, reason `occupancy_away` / `occupancy_vacation`). Every other grace (fan-off hold, window-close resume, dashboard resume, nat-vent-exit resume) runs to expiry regardless of occupancy, and `handle_occupancy_home()` never touches grace. | [§ Occupancy Interaction](#occupancy-interaction) |
| What is the override confirmation delay — how does it work and what does it gate? | A debounce window (default 600 s) between detecting a thermostat mode change and formally accepting it as a manual override. While pending, `apply_classification()` returns early, blocking all HVAC commands. If the mode self-corrects within the window (transient glitch), the event is discarded — no grace period starts. | [§ Override Confirmation Delay](#override-confirmation-delay) |
| What are all the callsites that clear a manual override, and under what conditions? | Eight sites: `_grace_expired()` (3 branches), scheduled handlers (bedtime/wakeup), dashboard cancel buttons (2), occupancy handlers (away/vacation). Each logged at INFO with reason. Important caveat: bedtime/wakeup must snapshot `_fan_override_active` *before* calling `clear_manual_override()`, since that call clears the flag as a side effect — see [§ Shared Scheduled-Band Gate](#shared-scheduled-band-gate-issue-498)'s "Capture-before-clear hazard". | [§ What Clears a Manual Override](#what-clears-a-manual-override) |
| What is the difference between `clear_manual_override()` and `cancel_override()`, and which should a new callsite use? | `clear_manual_override()` clears override flags only — used by scheduled handlers (bedtime/wakeup) and the away/vacation occupancy handlers that supersede an override. It does not touch the grace; those three callers end the override's grace themselves right after, through the one shared helper `_end_grace_left_protecting_nothing(reason, remote_timer_hours=...)` (Issues #874, #1222), which only ever ends an override-protecting grace. `cancel_override()` (Issue #508) additionally cancels grace and forces the same fan-reconcile + coordinator-refresh a natural grace expiry performs — it is the single entry point for "user/system deliberately ends this override right now" (both dashboard "Cancel..." buttons, and the `adopted_matching_decision` path). A new callsite meaning deliberate cancellation must call `cancel_override()`, not compose the primitives by hand. | [§ Deliberate Cancellation — `cancel_override()` (Issue #508)](#deliberate-cancellation-cancel_override-issue-508) |
| What happens if grace is left active with no override behind it (e.g. a future callsite bypasses `cancel_override()`)? | `coordinator._check_orphaned_grace()` (Issue #508), run once per coordinator update (the 30-minute `update_interval` plus on-demand refreshes such as the 5-minute revisit and sensor or fan events -- not every ~30 s), self-heals: if `_grace_active=True`, `_grace_protects_override=True` (Issue #530 — see next row), and neither `_manual_override_active` nor `_fan_override_active` is set, it force-cancels grace and emits `stuck_grace_recovered` with `reason="grace_without_override"`. It never fires while a manual change is still pending in its confirmation window (`_override_confirm_pending`, Issue #1216): a pending change is a manual change that has no confirmed override flag yet, so the grace is not orphaned. The decision lives in `override_orphaned_grace.decide_orphaned_grace(..., override_confirm_pending=...)`, which the watchdog calls. This is the mirror of the pre-existing Issue #321 stuck-grace check (which catches the opposite shape — an override stuck active past its own due grace-end-time); the two checks are independent and cannot both fire on the same state. | [§ Orphaned Grace Self-Heal (Issue #508)](#orphaned-grace-self-heal-issue-508) |
| Can every grace type be "orphaned" in the Issue #508 sense — does the watchdog apply to fan-off/window-close/resume grace too? | No (Issue #530 fix). `_start_grace_period(trigger=...)` sets `_grace_protects_override = trigger in _GRACE_TRIGGERS_PROTECTING_OVERRIDE` (only `"fan_manual_override"` and `"override_confirmed"`) — the watchdog now only fires for grace that was genuinely started to protect a real override. Before this fix, the watchdog inferred "orphaned" purely from absent override flags, which is also the NORMAL, by-design shape of fan-off/drift-correction/window-close/dashboard-resume grace — every one of those was being killed within about one event-loop tick of starting. | [§ Orphaned Grace Self-Heal (Issue #508)](#orphaned-grace-self-heal-issue-508) |
| A fan-off arrives seconds after an RF-remote-timer grace expires — is that a fresh event? | No (Issue #530). `_on_grace_expired()` arms `_timer_boundary_settle_until` (2 min) whenever the expiring grace's `_fan_remote_timer_hours` was set. A fan-off inside that window is treated as the tail of the SAME timer boundary — routed straight through `_exit_nat_vent()` instead of starting an independent new grace. | [§ RF-Timer Boundary Settle Window (Issue #530)](#rf-timer-boundary-settle-window-issue-530) |
| A default-duration manual fan-on's grace expires while the fan is still running (or CA reactivates it after a grace expiry) — when the occupant later switches that fan off, does a fan-off grace (hold) start? | Yes, always (Issue #1205, owner decision 2026-11-01). CA always knows how a run should end (an RF timer by time, a no-timer run by its default timer, then CA's own turn-off or adopt), so any fan-off CA did not command outside the RF-timer settle window is a person and starts the normal fan-off grace, in both supported fan modes. The Issue #829 marker `_fan_manual_origin_unclosed` is label-only: it selects the Activity Record wording (`trigger=manual_origin_carryforward`) and never decides or shortens the hold. | [§ Manual-Origin Unclosed Marker (Issue #829, Issue #1205)](#manual-origin-unclosed-marker-issue-829-issue-1205) |
| Is an occupant's Off on a mild day (classification `off`) kept? | Yes (Issue #1159). An occupant Off is always a manual change: confirmed (PATH A) and kept for the manual grace exactly as on hot/cold days, and never adopted as matching. This holds even if CA's own door/window pause (`_paused_by_door`) engages before the confirm or during the grace; nothing is written until the grace ends. | [§ State Machine](#state-machine) |
| Does PATH B (self-resolved transient) send a notification? | Yes (Issue #200). When the thermostat reverts to the mode CA commanded (or to the day's label) within the confirmation window, a push notification is sent: "Brief thermostat adjustment detected — treated as transient. Climate Advisor continues normal operation." | [§ State Machine — PATH B](#state-machine) |
| The occupant changes the thermostat twice inside one grace (for example Cool, then Off or Heat) -- which wins? | The second change, always (Issues #201, #1216). It is held as pending for the whole confirmation window, and neither the orphaned-grace watchdog (#508) nor the first grace expiring can drop it. If it is still different from what automation wants when the window ends, it is confirmed and gets its own grace; Rule 3 re-pauses for an open window or door only at the end of that grace. | [§ Second Override During Active Grace](#second-override-during-active-grace-issue-201) |
| What happens if the user changes to a different mode while a grace period is already active? | The first override is cleared and a fresh 10-minute confirmation window starts for the new mode (Issue #201); the first override's grace timer keeps running meanwhile, and the new change is held as pending, not dropped, until it is confirmed or self-resolves (Issue #1216). The latest user action always wins. | [§ Second Override During Active Grace](#second-override-during-active-grace-issue-201) |
| Where can the user see how much longer an active grace period will run, and what caused it? | Debug tab shows full `grace_end_time` and `last_action_reason`. Status tab shows `_format_grace_remaining()` with a cause label from `_GRACE_TRIGGER_LABELS`, e.g. "grace period (manual) — thermostat override — 30 min (ends 7:14 AM)" — this display moved onto the Status card and reached its current duration+cause-label shape across issues #620→#498→#527→#625 (full history in git blame; current shape is what's shown here). | [§ Debounce and Grace Period System](02-ARCHITECTURE-REFERENCE.md#debounce-and-grace-period-system) |
| Do bedtime/wakeup/pre-cool implement their own copies of the occupancy/override/paused/nat-vent gate checks? | No (Issue #498). All four scheduled/cyclical call sites — `apply_classification()`, `handle_bedtime()`, `handle_morning_wakeup()`, `handle_pre_cool()` — call one shared pure function, `desired_state.decide_scheduled_band_gate()`, instead of hand-copying the checks. This fixed a real bug: `handle_morning_wakeup()`'s copy was missing the fan-override guard entirely, and none of the three scheduled handlers checked paused-by-door at all. | [§ Shared Scheduled-Band Gate (Issue #498)](#shared-scheduled-band-gate-issue-498) |
| Does a fan-off grace actually stop nat-vent from immediately re-engaging, in every code path? | As of Issue #620, yes. `check_natural_vent_conditions()`'s `_idle_open` widening (Issue #244/#402/#504) never checked `_grace_active` — a user turning the WHF off could see it reactivated within seconds if outdoor stayed favorable. Fixed by adding `and not self._grace_active` to `_idle_open`; the Issue #134 comfort-ceiling exception beside it stays for CA's own graces but, since Issue #1205, never overrides the occupant's fan-off hold (a hot house does not bring the fan back inside the hold). | [§ Fan-Off Grace](#fan-off-grace-issue-359) |
| Can a scheduled routine (wake-up, bedtime, pre-cool, TOU pre-conditioning) or the 5-minute revisit switch off a manual heat/cool override because a door/window is open? | No (Issue #1119). `_sync_paused_by_door_with_live_sensors()` returns early while `_manual_override_active` or `_override_confirm_pending` is set, and override adoption never "agrees" while the open-window hold is live (`_override_decision_mode_and_target()` returns `(None, None)`). The occupant's mode holds until grace expiry, which re-pauses if a sensor is still open. A grace with no override (fan-off/automation grace) does not block the re-check. | [§ Manual Grace](#manual-grace) · [§ Shared Scheduled-Band Gate (Issue #498)](#shared-scheduled-band-gate-issue-498) |
| The occupant presses heat/cool on a door/window-paused thermostat while a grace is running (e.g. a whole-house-fan override grace) -- is that a pending change? | Yes (Issue #1158). `override_grace_fsm.transition()` treats `MANUAL_OVERRIDE_DURING_PAUSE` like `OVERRIDE_DETECTED` from a protecting or unprotected grace (a pause and a protecting grace DO coexist: a fan-override grace protects with no thermostat change behind it), landing `_override_confirm_pending=True` so `_manual_change_protected()` shields the press from `apply_classification()` and the open-window guard in `_apply_comfort_band()`. A press made during the pause (origin `"pause"`) is always CONFIRMED at the end of the confirmation window, even when it equals the day's classification label (heat on a cold day, cool on a hot day): during a pause CA's plan is "paused", so the label is not a plan the press could be "self-resolving" to (`override_confirm_split.is_always_confirm_origin()`, sibling of the #1159 Off rule). A pending change that self-resolves under a protecting grace ends without touching that grace (`OVERRIDE_CONFIRM_EXPIRED`). Rule 2 still holds: the press stands a running whole-house fan down. | [§ Manual Grace](#manual-grace) |
| Can a monitored sensor block a routine comfort-restore write even if it was never freshly detected as opening? | As of Issue #620, yes. `decide_scheduled_band_gate()`'s `paused_by_door` input previously reflected only event history (`_paused_by_door`, set by `handle_door_window_open()`/`_exit_nat_vent()`). A sensor open since before either ever ran left it `False` forever. `_sync_paused_by_door_with_live_sensors()` — called at the top of all four `decide_scheduled_band_gate()` callers — now reconciles the flag against live, debounce-settled sensor state first. | [§ Shared Scheduled-Band Gate (Issue #498)](#shared-scheduled-band-gate-issue-498) |
| Does briefly opening a monitored door (e.g. walking outside) instantly pause HVAC, bypassing the configured debounce window? | It could (Issue #623 regression from #620's `_sync_paused_by_door_with_live_sensors()`). `_sensor_debounce_pending()`'s old `_door_open_timers`-only check raced against the event listener that populates that dict — a concurrent coordinator refresh could read "open, not yet registered" and misfire a pause before the debounce window even started. Fixed by also checking each open sensor's HA-authoritative `last_changed` timestamp, immune to listener scheduling order. | [§ Debounce-Timer Registration Race (Issue #623)](#debounce-timer-registration-race-issue-623) |
| Can the routine comfort-band arm (`_apply_comfort_band()`) still write an active HVAC mode through a genuinely open window, even with `_paused_by_door` correctly tracked? | It could (Issue #629). `_apply_comfort_band()` never re-checked live window state itself — it trusted the gate computed once at the top of its caller. A coordinator refresh firing `apply_classification()` in the same tick nat-vent releases ownership could reach it with `_paused_by_door` still `False` (correct when checked) but the window still open, silently committing an active mode via `_set_temperature()`'s bundled `hvac_mode`. Fixed by a structural choke-point guard directly in `_apply_comfort_band()`, mirroring the WHF/AC mutex choke-point in `_set_hvac_mode()`. | [§ Comfort-Band Arm Had No Independent Window Check (Issue #629)](#comfort-band-arm-had-no-independent-window-check-issue-629) |
| Is there a named state enum for override/grace? | Yes: `override_grace_fsm.py` models state as `(OverrideConfirmState, GraceState)` — two small composed pieces, not one flat enum, since grace routinely runs independent of any override. As of Phase 6, the FSM is the sole authority, no longer shadow-only: `_resolve_override_grace_fsm_state()` computes the decision and `_apply_override_grace_fsm_state()` writes it into the canonical flags (`_grace_active`, `_manual_override_active`, etc.), which the rest of production still reads directly — the flags are the FSM's output, not an independent second source of truth. | [§ State Machine](#state-machine) |
| Does `lifecycle_dispatcher.py` supply `_grace_active`/`_manual_override_active`, or just audit them? | Audit only (Issue #717). `_resolve_override_grace_fsm_state()`/`_confirm_override_action()`/`_clear_manual_override_active()` emit real `GRACE_STARTED/ENDED`/`OVERRIDE_CONFIRMED/CLEARED` events at every genuine transition, but `AutomationEngine` is both the sole emitter and sole consumer of its own events today — the canonical flags stay the source of truth throughout this spec; the dispatcher never feeds a decision. | [§ Dispatcher-Based Event Sync](#dispatcher-based-event-sync-issue-717) |
| Does a thermostat coming back from `unavailable`/`unknown` start an override or grace period? | No (Issue #1049). `const.is_availability_recovery(old_state)` excludes it from all four thermostat override-detection sites in `_async_thermostat_changed()` (door/window-pause mode override, re-override during grace, plain mode override, setpoint-only override); a device waking up Off or with no setpoint is a reconnect, not an occupant action. | [Thermostat Availability Recovery](#thermostat-availability-recovery-issue-1049) |
| Does a running grace (automation, fan-off, blower press, dashboard resume) hold the remote-sensor setpoint loop (Issue #1194 b)? | No. The loop's gate is `_remote_offset_gate()`: `_manual_change_protected()` (confirmed override or pending change), the shared scheduled-band gate and an open monitored window (held as is inside its debounce, offset 0 after it). A grace is a timer for an owner, not an owner: no band writer defers on a bare grace, bedtime / wake-up / occupancy clear an override and leave its grace running by design, and holding the loop for CA's own 5-minute resume grace would leave the bedroom cold after every window close. | [08 Remote-sensor outer loop, Gates](08-COMPUTATION-REFERENCE.md#remote-sensor-outer-loop-on-the-thermostat-setpoint-issue-1194-increment-a) |

---

## Scope

**Files:**
- `custom_components/climate_advisor/automation.py` — all grace and pause logic
- `custom_components/climate_advisor/coordinator.py` — door/window state listeners, debounce timer scheduling, manual override detection during pause
- `custom_components/climate_advisor/lifecycle_dispatcher.py` — generic cross-lifecycle pub/sub event router (Issue #633), wired into production as of Issue #717 — see [§ Dispatcher-Based Event Sync](#dispatcher-based-event-sync-issue-717)
- `custom_components/climate_advisor/lifecycle_events.py` — the event vocabulary (`LifecycleEventType`, `LifecycleEvent`) the dispatcher routes, including the `GRACE_STARTED`/`GRACE_ENDED`/`OVERRIDE_CONFIRMED`/`OVERRIDE_CLEARED` events this spec's lifecycle now emits

**Line ranges (automation.py):**
- `_is_within_planned_window_period()`: L706
- `clear_manual_override()`: L799
- `cancel_override()`: L834 (Issue #508 — see [§ Deliberate Cancellation](#deliberate-cancellation-cancel_override-issue-508))
- `on_fan_turned_off()`: L1026 (Issue #530: RF-timer boundary settle-window check at its top — see [§ RF-Timer Boundary Settle Window](#rf-timer-boundary-settle-window-issue-530))
- `clear_fan_override()`: L1180
- `handle_door_window_open()`: L2408
- `handle_all_doors_windows_closed()`: L2607
- `handle_manual_override_during_pause()`: L3532
- `resume_from_pause()`: L3557
- `_start_grace_period()`: L3766 (Issue #530: sets `_grace_protects_override` from `trigger`)
- `_on_grace_expired()`: L3844 (its three branches now call `_cancel_grace_timers()` for the grace-flag
  reset instead of inlining it — deduped in Issue #508; Issue #530 added the settle-window arm at its top)
- `_cancel_grace_timers()`: L3980
- `_re_pause_for_open_sensor()`: L3793
- `_release_whf_and_reclassify()`: L4842 (Issue #530: `_natural_vent_active` guard added — see [§ Shared Scheduled-Band Gate](#shared-scheduled-band-gate-issue-498))
- `restore_state()`: L5503
- `get_serializable_state()`: L5588

**Line ranges (coordinator.py):**
- `_subscribe_door_window_listeners()`: L1146
- `_cancel_all_debounce_timers()`: L1302
- `_any_sensor_open()`: L1350
- `_check_orphaned_grace()`: L1629 (Issue #508, scope corrected in #530 — see [§ Orphaned Grace Self-Heal](#orphaned-grace-self-heal-issue-508))
- `_async_door_window_changed()`: L2960
- `_async_thermostat_changed()` (pause-override detection): L3080

**Note on line-number drift:** these are exact as of Issue #530 but will drift with future edits —
treat them as a navigation aid, not a contract; if a line doesn't match, search for the function name.

**Out of scope for this spec:**
- Natural ventilation internal logic — see [nat-vent-lifecycle-spec.md](nat-vent-lifecycle-spec.md) (Issue #606), which also documents the `_exit_nat_vent()` handoff into this spec's `PAUSED`/`GRACE` states
- Fan min-runtime cycles (separate lifecycle from grace)
- Occupancy setback calculations

---

## Grace Period Types

### Fan-Off Grace (Issue #359)

**Trigger:** User physically turns the fan off (fan_mode attribute → auto, or fan entity → off) while `_fan_active = True` or `_fan_override_active = True`. Detected by `_async_thermostat_changed()` / `_async_fan_entity_changed()`; dispatches to `on_fan_turned_off()`.

**Duration:** `DEFAULT_FAN_OFF_GRACE_SECONDS` (implementation-defined; tracked in `automation.py`).

**What it suppresses:** During active fan-off grace, `_activate_fan()` does not fire even if nat-vent conditions are met. The gate guards nat-vent **re-activation** — CA backs off from immediately restarting the fan the user just stopped.

**Issue #620 fix:** this guarantee was violated for one specific call path until 2026-08.
`check_natural_vent_conditions()`'s `_idle_open` widening (Issue #244/#402/#504 — re-evaluates
a long-open sensor so nat-vent can engage once outdoor cools, even without a fresh open event)
never checked `_grace_active` at all, unlike the comfort-ceiling exception beside it (Issue
#134, which correctly narrowed its own bypass to "grace active AND indoor above comfort_cool").
A user turning the WHF off with a window already open and outdoor still favorable could see it
reactivated within seconds — confirmed live, 2026-08-11. Fixed by adding
`and not self._grace_active` to `_idle_open`'s definition; the Issue #134 ceiling exception is
unaffected. See `issue_620_fan_off_grace_blocks_idle_reactivation` pending scenario for the
reproduction and revert-tested fix confirmation.

**Issue #134 comfort-ceiling bypass: CA's own graces only (Issue #1205, owner decision 2026-11-01).** Issue #134
("grace blindspot during comfort breach") lets nat-vent re-enter during a grace when indoor is ABOVE `comfort_cool`
(strict). It was written for Climate Advisor's own graces: after CA stopped the fan at the comfort floor, a
90-minute automation grace left the house drifting up to 75 F with nothing running. The check tested "any grace", so
it also overrode the PERSON's fan-off hold: with the house above `comfort_cool`, a window open and cooler air outside,
the fan came back on at the next check (about 240 s after the person switched it off). The bypass now applies only
while the running grace is NOT the fan-off grace. One predicate serves every site:
`nat_vent_fsm.overheat_bypasses_grace()` (`grace_active AND NOT fan_off_hold_active AND indoor > comfort_cool`),
used by `_grace_blocks_natvent()` (the FSM's grace short-circuit; `NatVentFsmInputs.fan_off_hold_active` is filled
from `_fan_off_hold_active()` by `_build_nat_vent_fsm_inputs()`), by `check_natural_vent_conditions()`'s entry
precondition, and by the Issue #1095 blocker log (`grace_active` is listed as the blocker for a hot house during a
fan-off hold). Unchanged for CA's own graces (automation / comfort-floor, nat-vent-exit resume, door/window
recommendation grace): a house above `comfort_cool` still re-enters nat-vent during them. In `hvac_fan` mode the
reactivation gate itself refuses above the ceiling (`comfort_cool`, or `comfort_cool + 2 F` with
`aggressive_savings`, Issue #392), so the blower only came back in the `aggressive_savings` band; `whole_house_fan`
has no ceiling and came back at any temperature above `comfort_cool`.

**The hold has its own deadline and survives a thermostat change (Issue #1205, owner decision 2026-11-01).** "Fan
off" and "thermostat X" are two separate requests from the occupant, so a later thermostat change must not cancel the
fan-off hold. Occupant view: the person switches the fan off, then sets the thermostat to Off. About ten minutes later the
Off is confirmed as an override and `_start_grace_period_action()` replaces the fan-off grace with the override's grace
(`override_confirmed`). Before this decision every hold check keyed on "the running grace is `fan_off`", so replacing
the grace lifted all of them: `_protected_active_mode()` exempts Off and the economizer has no override check of its own,
so about two minutes after the Off was confirmed (+13 min) CA started the fan again, long before the pause the person
asked for had ended (both fan modes).

- **One source of truth:** `AutomationEngine._fan_off_hold_until` (a `datetime`) and the predicate
  `_fan_off_hold_active()` (`now < _fan_off_hold_until`; a lapsed deadline is cleared and logged by whichever check first
  sees it). The deadline is the fan-off grace's end time at the moment it started (`_start_grace_period_action()` sets it
  when `trigger == "fan_off"`; the Issue #1006 restart re-arm comes through the same call; a newer fan-off restarts it
  with a new deadline). With the grace disabled (0 s) no grace starts and so no hold.
- **`_grace_is_fan_off_grace()` is kept, but only for "which grace timer is running"** (the grace-protection
  predicate `_grace_protects_no_hvac_on_change()` and the door/window log label). Every question of the form "is the
  occupant's off still in force?" reads `_fan_off_hold_active()`: the `_activate_fan()` backstop, the FSM input
  `fan_off_hold_active` (so the #134 bypass never applies to the hold), the Issue #134 precondition and blocker log in
  `check_natural_vent_conditions()`, the nat-vent start refusal in `handle_door_window_open()`, the min-runtime cycle
  deferral (retry at the hold's own deadline + 5 s) and the once-per-hold keys of the refusal log and the
  hold-beats-ceiling log (keyed on the deadline).
- **A thermostat change after the off does not end the hold.** When a thermostat override's grace (or any other grace)
  replaces the fan-off grace the engine logs INFO `Fan-off hold continues past <trigger> grace: hold_until=... override_mode=...
  override_active=...`. The hold runs to its original deadline; the fan stays off for all of it in both fan modes, with a
  hot or a mild day and a thermostat set to Off, cool or heat (a confirmed heat/cool override additionally keeps CA from
  starting the fan under the Issue #714 rule, which is separate from the hold).
- **How the hold ends (INFO `Fan-off hold ended: reason=... hold_until=...`).** David's rule: "fan off" and "thermostat X"
  are separate requests, so the hold ends early ONLY on (1) the occupant turning a fan on (`occupant_fan_on`,
  `handle_fan_manual_override()`: the newest fan action wins, whole-house fan and blower alike), (2) a dashboard cancel
  that targets the FAN-OFF pause (`grace_cancelled:<reason>`) and (3) a WHF safety stop that cancels a fan-owned grace
  (`_end_fan_override_and_grace()`); otherwise at its own deadline (`expired`, also the fan-off grace expiring).
  Ending a THERMOSTAT override never ends it: not the automatic adoption of a matching override
  (`cancel_override("adopted_matching_decision")`), not a dashboard cancel of the thermostat change, not a superseding
  grace. The two dashboard cancel endpoints (`api.py`) both call `AutomationEngine.cancel_override()`:
  `POST cancel_override` (reason `user_cancel_override`) and `POST cancel_fan_override` (reason
  `user_cancel_fan_override`). `cancel_override()` decides which request the cancel targets: a thermostat change
  (`_manual_change_protected()`: an active override or a pending confirmation) is the target unless the cancel comes
  from the fan card (`user_cancel_fan_override`); with no thermostat change behind it the cancel targets the fan side.
  So override-confirmed then fan-off then "Cancel override" keeps the hold, as does fan-off then a thermostat change
  then "Cancel override", while "Cancel" on a lone fan-off pause ends it. The fan card's cancel is a fan-side cancel
  whatever grace is running: it ends the hold and the fan run and the fan-owned grace (`_cancel_fan_side_only()`)
  and leaves a pending or confirmed THERMOSTAT change and its grace alone (it used to clear them too); the thermostat
  cancel mirrors it and leaves the fan run and the hold alone. A hold with NO grace running (the thermostat override's
  grace came and went) is still cancellable: `cancel_override()` includes `_fan_off_hold_active()` in its precondition and
  the endpoint answers "Fan-off pause ended". Dashboard (`frontend/index.html`): the status payload carries
  `fan_off_hold_active`, and the fan card's button is enabled by `fan_override_active || fan_off_hold_active`. With ONLY a
  hold running it reads "End Fan-Off Pause" and calls `cancel_fan_override`, which ends the hold and answers "Fan-off pause
  ended." (pinned by `tests/test_api.py::...test_cancel_fan_override_view_ends_a_fan_off_pause_with_no_grace_behind_it`).
  "Cancel Override" (thermostat side) is unchanged. Away/vacation handling and the bedtime/wake-up cancels
  only touch override-protecting graces, so they never ended a fan-off grace and do not end the hold either.
- **The occupant's fan run keeps its own deadline too (Issue #1205 F3, mirror on the fan-ON side).**
  `_fan_override_until` is the `fan_manual_override` grace's end at the moment the run started (an RF timer's duration
  included). A thermostat override's grace replacing that grace, and that grace's expiry, adoption or cancel, no longer
  clear `_fan_override_active` (before, a 4 h remote-timer run was cut off about 3.5 h early and in `hvac_fan` mode CA
  then sent `set_fan_mode auto`, stopping the occupant's blower): `_clear_manual_override_active(keep_fan_override=True)`
  restarts the `fan_manual_override` grace for the remaining time (INFO `Fan override continues past thermostat override
  grace: until=...`), so the run ends through the ordinary machinery at its own deadline, with the #530 settle window
  armed only when that fan grace expires. It still ends on its deadline, the occupant's fan-off, the fan card's cancel
  (`user_cancel_fan_override`), a WHF safety stop, bedtime/wake-up, away and every other path that ended it before;
  only "a thermostat override's grace ended" no longer does. The status card also names a hold or fan run when NO grace runs (`_compute_automation_status()` appends the same " — fan held off until H:MM" / " — your fan run continues until H:MM"). Persisted as `fan_override_until`; on restart it feeds the
  Issue #1045 stash when a thermostat grace was the persisted one.
- **Restart:** the deadline is persisted as `fan_off_hold_until` in the serialized engine state and restored by
  `_maybe_restore_fan_off_grace()`. When the fan-off grace itself was the persisted grace it is re-armed as in Issue #1006 and the
  hold with it; when a thermostat override's grace (not restored, Issue #1046) was the persisted one, the hold is
  restored on its own (INFO `Fan-off hold restored across restart`) while its deadline is still in the future. A lapsed
  persisted hold is not restored.
- **Status surfaces:** while the fan-off grace runs the Status card shows it (trigger label "WHF turned off"). Once a
  thermostat override replaces the grace, `_grace_status_detail()` appends " — fan held off until H:MM" (hold in the
  future, running trigger not `fan_off`) and, for the occupant's fan run, " — your fan run continues until H:MM"
  (`_separate_fan_request_detail()`). The Next Automation nat-vent candidate scans from no earlier than
  `ae._fan_off_hold_until`, so it never names a start inside the hold. The household-promise journal keeps the
  fan-off / fan-run episode open to its own deadline (`grace_cancelled` payload `continues`, a thermostat grace's
  `grace_expired` does not close a fan episode inside its deadline), so P4 keeps watching. Over the real event stream: `grace_cancelled` and `override_cleared` carry `continues` when the engine keeps the occupant's fan run (or the hold) past a thermostat override's end, and the journal then skips closing the fan episodes; a fan-side cancel or the run's own `fan_override_cleared` closes them.

**Backstop in `_activate_fan()` (Issue #1205).** Every CA-initiated fan START (nat-vent entry or cycling-on, the
min-runtime cycle, the economizer's window-cooling start, a reactivation) is refused with
`FanCommandResult.SUPPRESSED` ("no working fan": no session is recorded, by nat-vent or by the economizer) while the fan-off hold runs (`_fan_off_hold_active()`, which outlives the fan-off grace when a thermostat override replaces it). The
path-level guards decide first and say why; this catches a path with none (the economizer start had no grace check).
A stop is never refused here, and the occupant's own fan-on never comes through `_activate_fan()`
(`handle_fan_manual_override()`). INFO `Fan activation refused: occupant's fan-off hold active reason=... hold_until=...`
(INFO once per caller per hold, keyed on the caller prefix of the reason and the hold deadline because the reason embeds live temperatures; DEBUG on repeats).

**Economizer during the hold (Issue #1205 S1).** The economizer still decides MAINTAIN or COOL_DOWN on a hot day with a window open, but the backstop refuses the start, so `_apply_economizer_fsm_state_after_activation()` applies INACTIVE: no `_economizer_active`, no "ventilation holding" status or log, and no session. It is re-evaluated on its own next tick (the FSM still sees a change from INACTIVE), so once the hold ends and the conditions still hold, the economizer starts the fan. Before the fix it recorded itself active with the fan off, saw no change on later ticks and never retried.

The min-runtime cycle (`_fan_cycle_on()`; restarted by the occupant's own off) defers instead of being refused: while
the hold runs (also after a thermostat override replaced the grace) it starts nothing, sets no "cycle running" flag and retries at the hold's own deadline + 5 s (INFO
`Min-runtime cycle deferred: occupant's fan-off hold active (retry in Ns)`), so the hour's minimum runtime is delayed,
not lost.

**Semantics are INVERTED vs `fan_manual_override` grace:** `fan_manual_override` grace gates CA from **stopping** a fan the user is running. `fan_off` grace gates CA from **starting** a fan the user just stopped. Both protect the user's deliberate action from being immediately undone by automation, in opposite physical directions.

**Fan-off grace and the door/window pause (Issue #1201):** the grace guards the FAN, not the heating/cooling. Stopping a fan turns no HVAC on, so a window that stays open past the sensor debounce pauses HVAC as usual (owner rule 3) in both supported fan modes (`hvac_fan`, `whole_house_fan`): HVAC off, `_paused_by_door` recorded, `_pre_pause_mode` captured, normal notification; HVAC restores on close. `handle_door_window_open()` still refuses to start nat-vent while the grace runs (Issue #1006 contract: the fan the occupant stopped stays off). An occupant's protected thermostat change still wins over the window (no pause). Predicate: `AutomationEngine._grace_protects_no_hvac_on_change()`.

**Event emitted on trigger:** `fan_cancel` (payload: `fan_before`, `fan_after`). This event is distinct from `nat_vent_fan_off` (HVAC arming-state change — the physical fan may still be running under user control when `nat_vent_fan_off` fires) and from `fan_deactivated` (CA-initiated stop).

**Flags cleared:** `_fan_active = False`, `_natural_vent_active = False`. Critically, `_fan_override_active` is NOT set — this is not a manual override (the user stopped the fan, not started one against CA's intent).

**Never orphaned-grace-eligible (Issue #530):** because this grace type never sets an override flag by design, `_start_grace_period(trigger="fan_off")` (the default `trigger_label` in `_clear_fan_flags_and_start_grace()`) is NOT a member of `_GRACE_TRIGGERS_PROTECTING_OVERRIDE` — `_grace_protects_override` is `False`, so `coordinator._check_orphaned_grace()` never touches it. Before this fix, the watchdog could not distinguish "no override flag because nothing needs protecting" from "no override flag because something is broken," and killed essentially every fan-off grace within about one event-loop tick of starting (see [§ Orphaned Grace Self-Heal](#orphaned-grace-self-heal-issue-508)).

**End condition:** Grace timer fires → `reconcile_fan_on_startup()` is called to re-evaluate the physical fan state. If the fan is still running (edge case), the reconcile step either adopts it as nat-vent or turns it off. If the fan is off (normal case), no action is taken. **Exception (Issue #530):** if the fan-off arrives inside the RF-timer boundary settle window, no fan-off grace is started at all — see [§ RF-Timer Boundary Settle Window](#rf-timer-boundary-settle-window-issue-530). **No other exception (Issue #1205):** a fan-off CA did not command that falls outside that window always starts this grace, including the off of a fan CA took over or restarted after an earlier manual fan-on (the Issue #829 marker only changes the wording) — see [§ Manual-Origin Unclosed Marker](#manual-origin-unclosed-marker-issue-829-issue-1205).

#### Fan-Off Grace and Command-Only Mode (Issue #361)

Grace periods apply regardless of `fan_state_feedback` setting. When `fan_state_feedback=False`
(command-only mode):
- A fan-off grace period still gates nat-vent re-activation (CA will not re-command the fan
  ON while grace is active, even without state feedback)
- A fan-on grace period still prevents CA interference while the user controls the fan
- The post-grace reconciliation callback fires normally; in command-only mode it resets
  `_last_commanded_fan_state` to `None` and lets the next `_async_update_data()` cycle
  assert the desired state

`fan_state_feedback=False` does NOT bypass grace logic — it only changes HOW CA determines
the fan's current state (command tracking vs. physical state read).

---

### Manual Grace

**Trigger:** Any of three user-initiated events:
1. User manually changes the thermostat HVAC mode while `_paused_by_door = True` — detected by `_async_thermostat_changed()` in the coordinator; dispatches to `handle_manual_override_during_pause()` which calls `_start_grace_period("manual")`.
2. User manually changes fan state — `handle_fan_manual_override()` calls `_start_grace_period("manual")` directly.
3. User presses "Resume" on the dashboard — `resume_from_pause()` clears the pause and calls `_start_grace_period("manual")`. **Observe-only (Issue #1306, owner decision 2026-11-03 "clear pause, no grace"):** when `dry_run` is set and the restore came back `DRY_RUN` (or was not attempted), the pause is still cleared (the occupant said they no longer want it) but no grace starts, `_resumed_from_pause` is left as it was (the Status card does not say "resumed — door/window override"), no `grace_started` Activity row is written, the INFO log reads `[DRY RUN] Dashboard resume: door/window pause cleared, restore to ... not sent and no manual grace started`, and the API answers "Pause cleared. Automation is off (observe-only): no thermostat command was sent." Re-enabling automation then re-applies the plan (#1296), or re-pauses within ~30 s for a window still open (#1293 reconcile, Rule 3). With automation on nothing changes.
4. A manual thermostat override is confirmed after the `CONF_OVERRIDE_CONFIRM_PERIOD` window — `_confirm_override()` calls `_start_grace_period("manual")`.
5. **(Issue #486)** User selects a timer duration on the QuietCool RF wall remote — `coordinator._async_fan_remote_changed()` calls the SAME `handle_fan_manual_override()` as trigger #2, but with an optional `duration_override` (seconds) that makes the grace period last exactly as long as the remote's selected timer instead of the configured default. See [fan-remote-spec.md](fan-remote-spec.md) for the full firmware event contract and mapping.

**Duration:** Configurable via `CONF_MANUAL_GRACE_PERIOD` (`manual_grace_seconds`). Default: `DEFAULT_MANUAL_GRACE_SECONDS = 1800` seconds (30 minutes). A configured value of 0 disables manual grace entirely (timer is not started, `_grace_active` remains `False`). **Exception:** an RF remote timer (trigger #5 above) overrides this with its own duration via `_start_grace_period(duration_override=...)`.

**Issue #1142 (owner decision, rule 3) -- an automation grace does not hold an open window.** `handle_door_window_open()` treats a grace whose source is `automation` like the fan-off and blower-press graces: it never holds the pause. Inside it the window is started (full or soft start, per the nat-vent FSM) or paused exactly as outside a grace, for a forecast veto AND a gate failure alike; the grace-expiry re-check (`_re_pause_for_open_sensor()`) asks the same question and reaches the same answer. The text below describes the graces that still can hold it (an occupant-side manual grace with no override behind it, e.g. dashboard resume): even those hold the pause only while no fan session would start now, and that is the nat-vent FSM's answer (guards, lockout and soft start included), not a temperature-only gate.

**What it suppresses:** `handle_door_window_open()` first asks `_manual_change_protected()` (Issue #1133, below) and returns without pausing while the occupant's thermostat change is confirmed or awaiting confirmation. Only after that does it check `self._grace_active` for a grace that has **no** thermostat override behind it (fan-off, window-close, dashboard-resume, whole-house-fan manual grace; a thermostat-blower override grace under `hvac_fan` never blocks the pause, Issue #1172): if that grace is active AND outdoor temperature is at or above `nat_vent_threshold` (i.e., outdoor is NOT cool enough for natural ventilation), the method returns without pausing HVAC. If outdoor is cool enough for natural ventilation, execution falls through to the nat-vent path -- such a grace does not suppress nat-vent activation. (Before #1133/#1148, a *thermostat* manual grace took this same bypass and, because the nat-vent FSM refuses entry while an override is active, always ended in a pause over the occupant's heat/cool.)

**Issue #1227 (owner rule) -- a door/window open for less than its delay never changes anything, even when a grace expires inside the delay.** `_on_grace_expired()` decides "re-pause or clear" from `_any_monitored_sensor_settled_open()` (= `_any_monitored_sensor_holding_open()` AND no sensor still inside its open/close delay, `_sensor_debounce_pending`), not the raw sensor. A sensor still inside its delay counts as not yet open: the grace ends normally (flags cleared, no pause, no ventilation start, no "HVAC paused" message) and the post-delay handler (`handle_door_window_open()` when the delay timer fires, no grace left to defer to) decides then. The same predicate feeds the door/window and override-grace FSM inputs (`any_sensor_open`), the nat-vent exit Off-hold (`_exit_nat_vent()` and the three exit branches that call it for a window still open) and the reconcile's HVAC restore, so no path that writes the HVAC or starts a fan reads the raw sensor. Pinned by `tests/test_brief_open_grace_expiry_1227.py` and `tests_ha/test_brief_open_grace_expiry_1227.py`.

**Issue #1229 -- a second door inside its delay never hides a window open past its delay.** `desired_state.decide_open_sensor_hold()` takes `any_sensor_settled_open` (the per-sensor read above) instead of "any open AND nothing pending house-wide"; its callers (`_apply_comfort_band()` #629 guard, `_sync_paused_by_door_with_live_sensors()`, `_override_decision_mode_and_target()`, `_occupancy_restore_hold_reason()`, and the door/window FSM pause-entry tail, whose `DoorWindowFsmInputs.sensor_debounce_pending` field was removed) all pass it. What the occupant saw before: window A open for an hour, door B opened briefly -- (1) door B's refresh adopted the occupant's manual Cool "pre-expiry" and cancelled its grace with no re-pause, and (2) a Cancel Override (or any comfort-band arm) armed cool into window A, both because door B's pending delay hid window A. Now the pre-expiry adoption cannot match while window A is settled open (the grace runs to expiry and re-pauses), and the guard refuses the arm. Original design gap (#620/#629, centralised by #1050). Start-side mirror (nat-vent reactivation `_idle_open` used the house-wide pending flag): fixed by #1289, which reads `_any_monitored_sensor_open_and_settled()` (open, not dark, past its own delay -- the plain open read, asked per sensor). Pinned by `tests_ha/test_open_window_second_door_1229.py` (two sensors, both cases, revert-proofed against b22acec).

**A window that opens LATER never pauses the occupant's change (Issues #1133, #1148 -- owner decision 2026-10-30, rule 1: "HVAC stays off while a window is open UNLESS a manual override turns it on").** The one predicate is `AutomationEngine._manual_change_protected()` = `_manual_override_active or _override_confirm_pending`. It is deliberately **not** derived from `_grace_active`/`_grace_protects_override`: with `manual_grace_seconds=0` ("Manual Change Pause" = 0) a confirmed override has no grace flag at all, which is exactly why the old `if self._grace_active` guard let the pause through (design gap since #3; #1119 covered only a window that was *already open*). `handle_door_window_open()` returns, logging `Pause skipped: occupant's manual change protected entity=... override_active=... confirm_pending=... grace_active=...`, ahead of every grace branch; nat-vent cannot start over an override (the nat-vent FSM refuses while `override_active`), so there is nothing else to decide. The same predicate now guards `_sync_paused_by_door_with_live_sensors()`, the scheduled-routine waits, `_exit_nat_vent()` and the door-close restore, so every automation-initiated HVAC write answers to one definition. The override still ends through the existing paths (next manual change, Cancel Override, away/vacation, adoption, grace expiry, HA restart); the designed moment to switch off for a still-open window is **grace expiry** -- `_on_grace_expired()` clears the override first and only then schedules `_re_pause_for_open_sensor()` -- and for a grace-less override the next scheduled routine's live re-check once the override has ended. A window opened while the change is only *pending* confirmation is likewise skipped; if the change then self-resolves, `_after_override_self_resolved()` applies the pause immediately (it runs the window re-check first, then the reclassify when something waited on the pending change, and only then the notification, whose failure is isolated and logged as a WARNING). Pinned by `tests/test_protect_manual_change.py` (`TestLaterWindowDoesNotPauseTheOccupantsChange`, `TestGraceExpiryStillSwitchesOffForAnOpenWindow`, `TestBypassDoesNotFallThroughToAPause`) and `tests_ha/test_protect_manual_change_1133.py`.

**Nat-vent exit never rewrites the occupant's change (Issue #1147).** `_exit_nat_vent()` first checks `_manual_change_protected()`. When true (the occupant set the thermostat, e.g. Off, during the session) it stops the fan and releases the WHF's hold on the thermostat (`_deactivate_fan(restore_hvac=False, release_suppression=True)`, so `_pre_fan_hvac_mode` is cleared and `_whf_owns_hvac()` reads False) and does nothing else: no pre-vent mode restore, no `_set_temperature_for_mode()`, no automation grace, and -- the sensor-open mirror -- no door/window pause is recorded. The exits that bypass `_exit_nat_vent()` (the slow-loop comfort-floor exit, both away-ceiling exits, the reconcile no-fan release, bedtime/wake-up/min-runtime/economizer stops) are covered at the one place a fan stop writes a mode back: `_deactivate_fan()` downgrades `restore_hvac` to False while `_manual_change_protected()` (suppression is still released), the slow-loop floor exit also skips its comfort write and automation grace, and the economizer-off comfort-cooling restore is skipped. When a pending change self-resolves with a monitored sensor open, the window pause the pending change had shielded is applied immediately (`_after_override_self_resolved()`), not at the next 30-minute re-check. Before, the sensors-closed branch restored the pre-vent mode ("nat-vent exit resume", Off -> Cool) and only afterwards logged "Grace period start suppressed: manual grace still active".

**A setpoint change made while a door pause has the thermostat Off is the occupant's change (Issue #1131).** Owner question answered: yes, it counts as an override (rule 1: the occupant acted). It does not turn HVAC on, so the window pause stays in force while the window is open; what changed is what the override *means* and what the door closing does. (a) `_occupant_mode_when_ca_holds_off()` records the override against the mode the pause interrupted (`_pre_pause_mode`), not CA's own pause `off` -- the thermostat only reads Off because Climate Advisor put it there (`override_detected.detected_mode` and `_manual_override_mode` are now e.g. `heat`; if HVAC was already Off before the pause there is no restore target and `off` stays). (b) `_restore_comfort_after_door_window_close()` checks `_manual_change_protected()`: HVAC resumes in the pre-pause mode at the **occupant's** setpoint -- mode restore only, no `_set_temperature_for_mode()` comfort write, and no 5-minute automation grace replacing the occupant's manual grace. The mode-change sibling (`handle_manual_override_during_pause()`) is unchanged: it clears the pause because the occupant picked the mode themselves. The alternative -- treating the setpoint tweak like the mode change and clearing the pause -- was rejected because it would leave HVAC Off after the window closes (the occupant asked for heat at 75 and would get nothing until the grace ended) and would turn HVAC on into an open window, contradicting rule 1. Assumption: the thermostat keeps its target temperature across the mode restore; if one resets it, the `override_respected` promise exposes it.

**The occupant's own Off, and a setpoint change after a nat-vent exit, at the door-close restore (Issues #1165, #1157).** Both live in the protected branch of `_restore_comfort_after_door_window_close()` and share one rule: the occupant's newest action wins. (a) #1165: a MODE change to Off during a pause (e.g. the nat-vent-exit pause that leaves the thermostat in cool/heat) is recorded as `off` and kept -- `_protected_change_is_occupant_off()` (mode `off` and source not `setpoint`) makes the close write nothing. Before, the confirm-time call to `_occupant_mode_when_ca_holds_off()` was not gated on the change's source, so the live Off read as CA's own pause Off and the override was recorded as the pre-pause mode, which the close then restored over the Off (seed 1149005000058). The substitution now applies to setpoint-only changes at both detection and confirm. (b) #1157: a nat-vent exit with the window still open records no `_pre_pause_mode` (thermostat Off). A setpoint-only change on it is judged against the comfort family the selector would arm now (`_preview_comfort_family()`, the same side-effect-free preview override adoption uses: it reads `_comfort_family_would_switch()`, never the committing `_resolve_comfort_family_via_fsm()`, so the occupant's change emits no `comfort_family_switch`, commits no dwell/arming state and rate-limits nothing); the day's default family comes from the armed band's active edge, so a mild day (classification `off`) resolves too. That mode is the recorded override mode (so the `override_respected` promise sees the resume as the occupant's own mode) and the close resumes it at the occupant's setpoint. Consequence to know: `_protected_active_mode()` is then non-None while the window is still open, so Climate Advisor's own whole-house-fan / nat-vent start is refused over it (F-D) for the life of the override, where a recorded `off` would have allowed it. Away/vacation is deliberately not given a family: the live Off stays recorded as `off` and the protected close writes nothing, whereas an unprotected close redirects to the setback -- a known asymmetry, not changed here. An ordinary pause over an already-Off thermostat is untouched (the #1068 gate: `_paused_entity == "nat-vent-exit"`). The family is fixed when the occupant speaks, not re-resolved at the close (same as the pre-pause mode in #1131).

**Owner principle (2026-10-31): the latest manual change creates the latest override and replaces the old one; there is no returning to an earlier override.** (1) A press of the WHOLE-HOUSE fan (`handle_fan_manual_override()` with `hvac_fan=False` under `fan_mode` `whole_house_fan` or `both`) REPLACES any thermostat change, pending or confirmed, at ANY grace setting including 0: a pending confirmation is cancelled (timer, bookkeeping and, through the FSM, the confirm flag -- `FAN_OVERRIDE_DETECTED` when a grace starts, `OVERRIDE_CANCELLED` when it does not) so it can never confirm later, and a confirmed override is cleared (`override_cleared`, reason `new_override_during_grace`, via `_clear_thermostat_override_flags()`, which never touches fan state). That removes the old failure where a change made just before the fan was confirmed against the fan's Off -- recorded as `off` on a hot day (blocking every reclassify indefinitely) or discarded as a transient on a mild day. (2) When the fan session ends nothing earlier comes back as an override. The two end paths differ: when the OCCUPANT turns the fan off (`on_fan_turned_off()` -> `_release_whf_and_reclassify()`) the hold is released and Climate Advisor resumes its own plan through a reclassify -- nothing is restored; when CLIMATE ADVISOR stops the fan (`_deactivate_fan()`: sealed-house guard, nat-vent exits, reconcile) that call alone writes the pre-fan mode back, then the plan resumes (with the window shut; with it open, rule 3 keeps HVAC off once the sensor debounce has expired). There is no hand-back helper: the earlier `_occupant_mode_to_restore_after_whf()` / `_restore_occupant_mode_after_whf()` / `_whf_release_restore_mode` machinery was removed as dead under this principle -- the replacement for a safety stop (#1144's `_after_whf_safety_stop`) is the ordinary `_deactivate_fan()` restore path. The replacement is for the whole-house fan ONLY (WHF and HVAC fan are different physics): a press of the thermostat's own fan (`fan_mode` `hvac_fan`, or the thermostat leg of `both`; the coordinator's thermostat `fan_mode` listener passes `hvac_fan=True`) leaves a heat/cool change alone, and a thermostat event carrying a mode change AND a `fan_mode` change registers the mode change only (one event, one root cause). **Fan identity (Issues #1163/#1164).** `AutomationEngine._is_whf_press(hvac_fan)` is the ONE predicate for "a press of the whole-house fan" (`fan_mode` `whole_house_fan`/`both` and not the thermostat blower); it gates BOTH consequences -- replacing the occupant's thermostat change and the HVAC suppression (so the thermostat blower, which coexists with the compressor, never turns HVAC off, under any configuration). A thermostat-fan press dispatches `HVAC_FAN_OVERRIDE_DETECTED` (same grace landing as `FAN_OVERRIDE_DETECTED`, but the confirm axis is preserved), so a pending thermostat change stays pending with its timer intact at any grace; `FAN_OVERRIDE_DETECTED` is kept for the WHF press, which cancels it. The engine records which fan an override belongs to (`_fan_override_is_hvac_fan`) and `on_fan_turned_off(hvac_fan=)` ignores an off report from the OTHER fan: a blower On->Auto no longer ends a WHF RF-timer override (and a WHF-entity off no longer ends a blower override); the coordinator's thermostat `fan_mode` listener passes `hvac_fan=True` in both directions. **Scope (Issue #1197, owner decisions 2026-11-01):** the thermostat blower is Climate Advisor's fan only under `hvac_fan` (and the unsupported `both`) -- `fan_mode_resolver.thermostat_blower_is_ca_fan()`; under `whole_house_fan` a blower change is ignored (INFO log, no override, no grace, no `on_fan_turned_off()`), so a blower override can only exist where there is no whole-house fan entity. **A blower-override or fan-off grace never holds off the door/window pause (Issues #1172, #1201):** `handle_door_window_open()` skips its grace branch when `_grace_protects_no_hvac_on_change()` (trigger `fan_manual_override` + `_fan_override_is_hvac_fan`, or trigger `fan_off`; no protected thermostat change); the pause happens after the sensor debounce and the occupant's blower keeps running (#1180). **Restart (Issue #1170):** `_fan_override_is_hvac_fan` is not persisted; the startup re-arm derives it from config (`hvac_fan=True` under `hvac_fan`, WHF otherwise), so the occupant turning the blower off after a restart ends the override. A mode-change override in the same thermostat event gates only the fan-ON press, never the fan-off direction. A shared helper, `AutomationEngine._replace_thermostat_override()`, is the one "a new override replaces the old one" implementation, used by the coordinator's mode-to-mode supersession and by the WHF press. (3) Issue #1205 (owner decision, final review): a thermostat MODE change while the fan runs does NOT replace the occupant's fan run -- "fan" and "thermostat" are separate requests and neither cancels the other unless they conflict. The coordinator's mode-to-mode path (`_replace_thermostat_override(clear_fan=False)`, the only production caller) replaces the old thermostat override only; the fan run keeps its own deadline (`_fan_override_until`). Rule 2 (WHF and HVAC never together) is enforced by the WHF safety check (`whf_safety_decision` `STOP_MUTEX_FAN` -> `_end_fan_override_and_grace`) when the newer mode is heat/cool; in `hvac_fan` mode nothing conflicts and the blower run continues (a silent clear is worse: on command-only installs `fan_believed_on` reads `_fan_override_active`, so after a clear the mutex could no longer stop a still-running WHF). The #714/#748 stand-down still keeps the occupant's mode; a temperature-only change while the WHF owns HVAC is ignored (never an override, not counted); CA's own fan start refuses over a protected active mode, pending included (F-D). `_occupant_mode_when_ca_holds_off()` remains only for CA's own door/window pause (#1131): the pause's Off is never mistaken for the occupant's mode. `_notify()` never raises (M3); a late echo of CA's own command with a missing prior setpoint is not an override (L6).

**Confirmation source reaches the FSM (Issue #1146).** `_confirm_override_expired()` must clear the confirm bookkeeping (`_clear_override_confirm_action()`, which sets `_override_confirm_source` to None) before it writes the grace flags, so the FSM input builder used to read `setpoint_override=False`, take PATH B and land on (IDLE, NONE) while the real manual timer was armed -- a setpoint-only change whose mode already matched the day's classification (typical on heat/cool days) was never protected and the status card showed no grace. `_resolve_override_grace_fsm_state(..., confirm_source=source)` now hands the source to `_build_override_grace_fsm_inputs(confirm_source=...)` explicitly; `_override_confirm_source` still ends as None. The coordinator's duplicate override/grace tracker (`_evaluate_override_grace_fsm`, its builder copy and ~15 feed call sites, `_override_grace_fsm_state`) was dead diagnostics that carried the same defect and was deleted (Issue #1152); `AutomationEngine._resolve_override_grace_fsm_state()` is the only override/grace FSM dispatch.

**Cancel Override ends an unconfirmed change (Issue #1154).** `cancel_override()` and the dashboard response both ask `AutomationEngine._occupant_override_active()` (`_manual_change_protected()` or a fan override), so a change still inside its confirmation window is cancellable, the confirmation timer is cancelled with it, and the FSM's `OVERRIDE_CANCELLED` from (PENDING, NONE) lands on (IDLE, NONE) with outcome `had_pending_only` (it was a NOOP before).

**An unrelated grace expiry never drops a pending change (Issue #1156).** `_on_grace_expired()` first asks `override_grace_fsm.unprotected_grace_expiry_keeps_pending()` (a change is pending and no confirmed override stands behind it, whoever's grace it is -- Issue #1216 dropped the old "grace not the occupant's own" term; the FSM's `GRACE_TIMER_EXPIRED` from an unprotected grace uses the same function, so engine and FSM cannot disagree): the grace (window-close resume, fan-off cooldown, dashboard resume, or the superseded first override's grace) then ends alone -- no confirmation clear and no re-pause -- and the FSM keeps the confirm axis on `GRACE_TIMER_EXPIRED` from an unprotected grace. A CONFIRMED override deliberately falls through to the old path: its protection is a grace, and fan-off / dashboard Resume replace that protecting grace with an unprotected one while the override stays live, so that grace expiring is the override's end (clear, and re-pause for a window left open, rule 3); keeping it would leave an override with no timer. A second change pending behind a confirmed override is cleared with it (timer included): a pending change must always have its confirmation timer. The occupant's own grace expiring with a window open and nothing pending still re-pauses. With a window open and a change pending (Issue #1216, owner decision), the ending grace does not re-pause over the pending change: it is manual, Rule 3 exempts it (as in #1156/#1119), it is confirmed, gets its own grace, and Rule 3 re-pauses at that grace's end. `_re_pause_for_open_sensor()` returns if a protected change appeared before the task ran, and discards the re-pause record `_on_grace_expired()` had just applied (`_discard_pause_record_for_manual_change()`, shared with `handle_manual_override_during_pause()`).

**Re-armed timers cancel their predecessor (Issue #1135).** `AutomationEngine._rearm_timer()` cancels the handle already in a slot before arming; the setpoint check/retry (`_setpoint_retry_cancel`) and the post-fan verifies (`_fan_on_verify_cancel`, `_fan_off_verify_cancel`) use it so `cleanup()` leaves no ghost timer.

**Already-open sensors (Issue #1119).** The same protection holds for a door/window that was already open when the user made the override (the override-during-pause path, trigger 1, or trigger 4 with a sensor open). Two other paths used to undo it: (a) `_sync_paused_by_door_with_live_sensors()`, the live re-check run at the top of wake-up, bedtime, pre-cool, both TOU pre-conditioning paths and `apply_classification()`, switched HVAC off before those routines reached their own `DEFER_OVERRIDE` gate (design gap since #620). It now returns early while `_manual_override_active` or `_override_confirm_pending` is set; (b) the pre-expiry adoption in `apply_classification()` declared "automation agrees" because the comfort-edge selector wanted the same family, cancelled grace, and the open-window guard then switched HVAC off (regression from #1073). `_override_decision_mode_and_target()` now returns `(None, None)` while the open-window hold is live (`_paused_by_door` or `decide_open_sensor_hold()`), so a heat/cool override cannot match an outcome that is really "off". The designed moment to switch off is grace expiry with a sensor still open (re-pause, below). A grace with no manual override behind it (fan-off grace, automation grace) does **not** block the re-check (owner decision D16). **Manual grace disabled (`manual_grace_seconds=0`, "Manual Change Pause" = 0 minutes) — owner decision, Issue #1119.** A manual heat/cool change made while a door/window is open is confirmed as an override with no grace timer, and it stays: there is no grace expiry, so CA never falls back to the window pause on its own. The re-check and the scheduled routines leave it alone (wake-up/bedtime/pre-cool/TOU skip on `DEFER_OVERRIDE`; `apply_classification()` skips and cannot adopt while the window is open). It ends only through another override-ending path: the occupant's next manual change, the dashboard Cancel Override button, away/vacation, adoption by `apply_classification()` once the window is closed and automation's decision matches, or an HA restart (clean slate). Pinned by `tests/test_override_vs_open_window.py::TestZeroManualGraceKeepsOverrideWithWindowOpen`.

**Unconfirmed changes (Issue #1119 follow-up, owner decision).** While `_override_confirm_pending` is set, `handle_morning_wakeup()`, `handle_bedtime()` and `handle_pre_cool()` now wait — no re-check, no `clear_manual_override()`, no band arm — exactly as `apply_classification()` and both TOU pre-conditioning paths already did (#786 Fix 2). This applies with the door open or closed: a change made just before wake-up/bedtime/pre-cool is no longer replaced by the routine. Wake-up emits `morning_wakeup_skipped` and bedtime `bedtime_setback_skipped` (both `reason="override_confirm_pending"`; bedtime also sets `DailyRecord.setback_skipped_reason`); pre-cool returns `"skipped: override confirmation pending"`. The check (`_scheduled_routine_waits_for_pending_override()`) runs first, ahead of the occupancy check, the same order as `apply_classification()`. When the confirm timer fires, a still-divergent change becomes a protected override (the routine has already been skipped; later scheduled runs skip on `DEFER_OVERRIDE`), and a reverted change self-resolves and the next 30-minute `apply_classification()` cycle arms the time-resolved band (sleep or day). A pre-cool skipped this way is not retried that night.

**Notification:** Off by default (`CONF_MANUAL_GRACE_NOTIFY` defaults to `False`). Configurable to on.

**End condition:** Timer fires → `_grace_expired()` callback executes one of three branches (see [Timer Lifecycle](#timer-lifecycle)).

---

### Automation Grace (Window-Close Grace)

**Trigger:** Any of two Climate Advisor-initiated resumptions:
1. All monitored doors/windows transition to closed — `handle_all_doors_windows_closed()` restores `_pre_pause_mode`, calls `_start_grace_period("automation")` (L1231).
2. Natural ventilation ends because all sensors closed — after fan deactivation and HVAC mode restore, `_start_grace_period("automation")` is called (L1214).

**Duration:** Configurable via `CONF_AUTOMATION_GRACE_PERIOD` (`automation_grace_seconds`). Default: `DEFAULT_AUTOMATION_GRACE_SECONDS = 300` seconds (5 minutes). A configured value of 0 disables automation grace.

**Observe-only starts no automation grace (Issue #1300).** The grace says "Climate Advisor just changed the thermostat". With automation off (observe-only) every restore write comes back `DRY_RUN` and nothing reaches the thermostat, so there is no change of ours to cover. All three post-restore sites -- the window-close restore (`_restore_comfort_after_door_window_close()`, trigger `sensor_closed_resume`), `_exit_nat_vent()`'s sensors-closed branch and the slow-loop comfort-floor exit in `check_natural_vent_conditions()` (both `nat_vent_exit_resume`) -- go through `_start_automation_grace_unless_restore_unsent()`: while `dry_run` is set and every restore command came back `DRY_RUN` (or was not attempted) it logs `[DRY RUN] Automation grace (...) not started` at INFO and records nothing; an occupant change in the next minutes is handled as any observe-only change. With automation on it is `_start_grace_period("automation", trigger=...)` once the restore has landed (Issue #1308, below). Same "record only what was sent" family as #1293/#1295. The fan-off/drift-correction grace (`_clear_fan_flags_and_start_grace()`) follows an observed fan-off, not a restore command, and is unchanged.

**A restore that did not land starts no automation grace yet (Issue #1308, owner decision 2026-11-03 "retry, no grace").** Occupant: they close the last window and the AC/heat restore is refused or not confirmed; the grace used to start anyway and held the AC/heat off for 5 minutes. Now `_start_automation_grace_unless_restore_unsent()` judges the restore by its last command: `FAILED`, or `UNCONFIRMED` with the thermostat not reading the restored mode, keeps the restore pending (`_pending_restore`) and re-sends it at 15/60/300 s; the grace starts when the thermostat reads the restored mode (an `UNCONFIRMED` that already landed starts it at once). The pause flags are cleared as before (the window is closed); only the restore target and the grace trigger are held. The chain ends with no grace when the occupant's change is protected, a window opens again or a fan session takes the HVAC, and after three re-sends (the 30-minute cycle, no longer blocked by a grace, is the backstop). See [Computation Reference §10](08-COMPUTATION-REFERENCE.md#10-doorwindow-hvac-pause).

**Dashboard Resume and the occupant's own mode (Issues #1312, #1313).** A dashboard Resume whose restore did not land still clears the pause (the occupant asked for that), re-sends the restore on the same bounded chain and starts the MANUAL `dashboard_resume` grace only once it lands (an `UNCONFIRMED` that landed starts it at once; observe-only stays #1306's "clear, no grace"). While it is pending, `apply_classification()` skips its cycle and the live-sensor reconcile does not re-pause (the grace's job, done by the chain until it starts); the API reply reports the re-send instead of "Manual grace period started". When the window closes with the occupant's protected change standing, the mode-only restore of THEIR mode is re-sent the same way with no grace (their manual grace already runs); only a newer occupant change, the change ending, a window open again or a fan session stops it (G3: re-sending their own choice does not override them).

**A dashboard Resume holds the open window for its whole grace (Issue #1314).** Occupant: they pressed Resume to get the AC/heat back with the window open, and about 5 minutes later the next cycle paused it again inside the Resume grace. A Resume is a recognised manual action (rule 3), and the state table above says a manual / dashboard-resume grace is not re-paused. `_dashboard_resume_holds_open_window()` (true while the manual `dashboard_resume` grace runs, or while the Resume's restore is in flight / re-sent, #1312) is now honoured by both cycle re-pause paths: the live-sensor reconcile `_sync_paused_by_door_with_live_sensors()` (also used by bedtime, wake-up, pre-cool, TOU and the #1293 re-enable reconcile) and the comfort-band safety check in `_apply_comfort_band()` (which arms the band instead of pausing). A NEW window opening during the grace was already suppressed by `handle_door_window_open()` (a manual grace that is not fan-off / blower / automation holds the pause), per the table row. Grace expiry is unchanged: `_on_grace_expired()` -> `_re_pause_for_open_sensor()` re-pauses a window still open. Observe-only starts no Resume grace (#1306), so nothing is held there. Tests: `tests_ha/test_resume_grace_holds_open_window_1314.py`.

**What it suppresses (superseded by Issue #1142):** an automation grace no longer suppresses anything for an open window past the debounce: the handler starts nat-vent if the nat-vent FSM allows it, otherwise it pauses (rule 3).

**Notification:** On by default (`CONF_AUTOMATION_GRACE_NOTIFY` defaults to `True`). Configurable to off.

**End condition:** Same three-branch expiry callback as manual grace.

---

## State Machine

Production represents state as the raw `_paused_by_door`/`_grace_active`/`_grace_protects_override`/`_override_confirm_pending` flag combination. `override_grace_fsm.py` (Issue #639, now Phase 6 authority) names this state as two small composed pieces — `OverrideConfirmState` (IDLE/PENDING) x `GraceState` (NONE/ACTIVE_PROTECTING_OVERRIDE/ACTIVE_UNPROTECTED) — because grace routinely runs with no override behind it (fan-off, window-close, dashboard-resume grace) and a flat enum would misrepresent that. The FSM is the sole authority; all production decisions read the canonical flags, but the named state labels and emission of `GRACE_STARTED/ENDED`/`OVERRIDE_CONFIRMED/CLEARED` events now flow through the FSM.

| From State | Event | To State | Side Effect |
|---|---|---|---|
| NORMAL (`paused=F, grace=F`) | Door/window opens; stays open past debounce; HVAC was not off | PAUSED (`paused=T, grace=F`) | Store `_pre_pause_mode`; set HVAC off; send door-pause notification |
| NORMAL | Door/window opens; HVAC was already off | NORMAL (unchanged) | `_paused_by_door` set to True but no HVAC call needed; no grace started |
| NORMAL | Door/window opens during planned window period | NORMAL (unchanged) | No pause, no grace; sensor open is expected |
| PAUSED | All sensors close | GRACE (`paused=F, grace=T`) | Restore `_pre_pause_mode` via HVAC service call; restore comfort temp; start automation grace timer |
| PAUSED | User manually changes HVAC mode on thermostat (not automation-initiated) | GRACE (`paused=F, grace=T`) | Clear `_paused_by_door` and `_pre_pause_mode`; start manual grace; cancel all debounce timers |
| PAUSED | User presses "Resume" on dashboard | GRACE (`paused=F, grace=T`) | Clear pause; restore `_current_classification.hvac_mode` (not `_pre_pause_mode`); set `_resumed_from_pause=True`; start manual grace |
| GRACE (automation) | New door/window open (nat-vent refused: warm outdoor, veto or lockout) | PAUSED_DURING_GRACE (Issue #1142) | Paused now, grace untouched; notified |
| GRACE (manual / dashboard resume) | New door/window open (outdoor too warm for nat-vent) | GRACE (unchanged) | Suppressed — no re-pause, no new grace timer |
| GRACE (override, `_grace_protects_override=T`) | Scheduled routine (wake-up / bedtime / pre-cool / TOU pre-conditioning) or 5-minute revisit with a monitored sensor already open (Issue #1119) | GRACE (unchanged) | No re-check pause, no adoption, no HVAC call; the routine's own `DEFER_OVERRIDE` gate skips it. Re-pause waits for grace expiry |
| GRACE | New door/window open (outdoor cool enough for nat-vent) | NAT_VENT (special: `paused=F, grace=T`, `_natural_vent_active=T`) | Falls through grace guard to nat-vent path; HVAC off, fan on |
| GRACE | Grace timer fires; within planned window period | NORMAL (`paused=F, grace=F`) | Clear grace flags silently; call `clear_manual_override()` |
| GRACE | Grace timer fires; sensor open AND past its delay (`_any_monitored_sensor_settled_open()`, Issue #1227; a sensor still inside its delay is not open yet and the grace just ends) | PAUSED (`paused=T, grace=F`) | Clear grace flags; schedule `_re_pause_for_open_sensor()`; emit `grace_expired` event with `re_paused=True` |
| GRACE | Grace timer fires; all sensors closed | NORMAL (`paused=F, grace=F`) | Clear grace flags; call `clear_manual_override()`; emit `grace_expired` event with `re_paused=False`; send notification if enabled |
| GRACE | `_cancel_grace_timers()` called (e.g., new grace replaces old) | NORMAL (`paused=F, grace=F`) | Cancel active timer; clear `_grace_active`, `_last_resume_source` |
| NORMAL | Fan manual override detected | GRACE (`paused=F, grace=T`) | Start manual grace (fan override path — HVAC pause not involved) |

**Cross-reference (Issue #606):** the PAUSED state entered via `_exit_nat_vent()`'s sensor-still-open branch corresponds exactly to nat-vent's own `PAUSED_REACTIVATION_LOCKOUT` state when the exit reason was the outdoor-warm-rise exit (the only exit that arms the reactivation lockout) — see [nat-vent-lifecycle-spec.md § Handoff to Pause/Grace](nat-vent-lifecycle-spec.md#handoff-to-pausegrace). The GRACE row above ("New door/window open (outdoor cool enough for nat-vent) → NAT_VENT") is the reverse edge of that same handoff.

### Dispatcher-Based Event Sync (Issue #717)

As of Issue #717 (PR #720), `_resolve_override_grace_fsm_state()` — the same choke point that already covers every real grace transition regardless of which internal branch decides it (Issue #639's before/after `_grace_active` diff feeding `override_grace_fsm.py`'s own comparison) — additionally emits real `GRACE_STARTED`/`GRACE_ENDED` events through `lifecycle_dispatcher.py` whenever that diff shows a change. `_confirm_override_action()` and `_clear_manual_override_active()` (the single unconditional sites each already owns) similarly emit `OVERRIDE_CONFIRMED`/`OVERRIDE_CLEARED`. These are real events at real transition points, not a diagnostic simulation — but see the precision note immediately below before treating them as a new source of truth for grace/override state.

**Precision note — the dispatcher is an audit trail, not a data source.** `_paused_by_door`, `_grace_active`, and `_manual_override_active` remain sourced exactly as described throughout this spec: direct instance attributes on `AutomationEngine`, read and written by the methods and call sites documented above. The dispatcher does NOT supply these values to anything that decides HVAC behavior — `AutomationEngine` registers as both the sole emitter and the sole consumer of its own events today (`_on_lifecycle_event()` only updates a separate set of `_dispatched_*` mirror attributes, never the canonical flags), so this is a same-instance emit/consume round-trip, not a genuine second source. Its value is that every real transition now has a corresponding audited event — proven by `check_registry_completeness()` and the dispatcher's own `event_log` — which the diagnostic layer and any future genuinely-separate lifecycle controller can build on, not that today's grace/override reads changed in any way. **Full write-up:** [nat-vent-lifecycle-spec.md § Dispatcher wiring status](nat-vent-lifecycle-spec.md#dispatcher-wiring-status-issue-717) covers the same wiring in more detail (it's shared infrastructure across all three lifecycles, not grace/override-specific) — this section exists so a reader who only opens this spec isn't left wondering whether `lifecycle_dispatcher.py` touches grace/override at all. See also [02-ARCHITECTURE-REFERENCE.md § FSM Decision Layer](02-ARCHITECTURE-REFERENCE.md#fsm-decision-layer).

**Issues #721/#722 — investigated re-sourcing two more reads to the dispatcher, both stayed canonical.** #721 asked whether `handle_manual_override_during_pause()`/`resume_from_pause()`'s `if not self._paused_by_door: return` guard should read `_dispatched_paused_by_door` instead. Investigation confirmed the two values are always equal in production (every real writer of `_paused_by_door` already funnels through a before/after diff), but `tests/test_resume_from_pause.py` and 3 sibling test files set `engine._paused_by_door = True` directly, bypassing the dispatcher, then call these methods immediately — re-sourcing the guard would silently no-op every one of those tests' scenario, the identical regression the FSM-input-builder wiring above already hit and reverted once. The guard stays canonical; no code change. #722 (the sibling `whf_owns_hvac` cross-read in `door_window_fsm.py`, not owned by this spec — see `08-COMPUTATION-REFERENCE.md` §9) hit the same test-fixture conflict for the same reason and was resolved the same way: a new `_resolve_whf_hvac_suppression()` chokepoint + `_dispatched_whf_owns_hvac` mirror now exist (registered alongside the events above), giving that cross-read the same audit trail this section describes, but the FSM input itself still reads `_whf_owns_hvac()` directly.

**Note on concurrent manual and automation timers:** `_start_grace_period()` always calls `_cancel_grace_timers()` first (L1469). This means starting a new grace (of either type) unconditionally cancels any running timer. The engine cannot have both `_manual_grace_cancel` and `_automation_grace_cancel` active simultaneously — the second call to `_start_grace_period()` replaces the first. `_grace_active` therefore reflects the most recently started grace only.

---

## Pre-Pause Mode Storage

**What is stored:** `self._pre_pause_mode` stores the HVAC mode string (e.g., `"heat"`, `"cool"`) as read from `hass.states.get(self.climate_entity).state` at the moment the pause begins (L1164–L1166 in `handle_door_window_open()`).

**Pause guard (corrected — Issue #523):** If the current HVAC mode is `"off"`, `"unavailable"`, or `"unknown"`, no HVAC service call is made and `_pre_pause_mode` stays `None` — but `_paused_by_door` IS still set to `True`, and `_paused_with_hvac_already_off` is set to `True` alongside it (a companion flag that distinguishes this case from a genuine mode-change pause; see `check_natural_vent_conditions()`'s idle-open re-evaluation loop, which must keep running in this case since nothing else will ever re-trigger it). Before this fix, `_paused_by_door` was left `False` when HVAC was already off, which silently let the next `apply_classification()` cycle arm HVAC against an open window — the bug reported in #523, surfaced via `_do_startup_coalesce()` after an HA restart (HVAC is commonly already off at that point). Both `handle_door_window_open()` and `_re_pause_for_open_sensor()` now share this logic via a `_pause_for_door_window()` helper instead of hand-rolling it separately (they had already drifted out of sync once — see Invariant 2 below).

**Where stored:** In-memory on the `AutomationEngine` instance as `self._pre_pause_mode`. It is also included in `get_serializable_state()` (L2085) and therefore written to the persisted learning JSON on each state save.

**Restoration on door-close:** `handle_all_doors_windows_closed()` calls `_set_hvac_mode(self._pre_pause_mode, ...)` (L1222–L1224), then sets `_pre_pause_mode = None` (L1232). Comfort temperature is also restored via `_set_temperature_for_mode()`.

**Restoration on dashboard resume:** `resume_from_pause()` does NOT use `_pre_pause_mode`. It uses `_current_classification.hvac_mode` instead (L1449–L1456), because the classification may have changed since the pause was set. `_pre_pause_mode` is set to `None` at L1443.

**HA restart during PAUSED state (corrected — Issue #523):** `restore_state()` does NOT restore `paused_by_door` or `pre_pause_mode` — `__init__`'s clean defaults (`False` / `None`) apply on every restart, same as override/grace state. If the paused sensor is still open at restart, `_do_startup_coalesce()` (5 minutes after boot) re-derives the pause by delegating to `handle_door_window_open()` against the sensor's live state — this is what makes the off-mode pause guard above load-bearing at startup, not just during normal runtime.

**HA restart during GRACE or OVERRIDE state (Issue #282 — clean slate):** Override and grace state are NOT restored. `restore_state()` does not restore `_manual_override_active`, `_grace_active`, `_override_confirm_pending`, or their companion timestamps. CA always starts in clean automation mode after restart. The 5-minute startup coalescing window (`_startup_coalesce_active`, see §11 Startup Coalescing Window of `08-COMPUTATION-REFERENCE.md`) provides a debounce gap before any automation action is taken. A thermostat or fan change the occupant makes inside that window is not supported: it is not detected as an override and the plan is applied at coalesce end (owner decision, Issue #1235 — intended; do not track changes during the window). Previously (#227) the grace timer was restored on restart; Issue #282 reverts this in favor of the simpler clean-slate model.

---

## Invariants

Confirmed from code:

1. **Pre-pause mode is captured before any HVAC service call.** `handle_door_window_open()` reads `state.state` into `_pre_pause_mode` at L1165–L1166 before calling `_set_hvac_mode("off")` at L1172. The stored value reflects the mode that was active, not the mode after HVAC is turned off.

2. **`_paused_by_door` is set whenever a monitored sensor is open, regardless of HVAC mode — only the HVAC service call is conditional (Issue #523 correction).** `_pause_for_door_window()`'s mode check (`if mode not in ("off", "unavailable", "unknown")`) gates the `_set_hvac_mode("off")` call and `_pre_pause_mode` capture, not `_paused_by_door` itself — that flag is set in both branches. `handle_door_window_open()` and `_re_pause_for_open_sensor()` had drifted out of sync on this exact point before the fix: `_re_pause_for_open_sensor()` (added whole in #47) already set the flag in both branches; `handle_door_window_open()` (older, predates #47) did not, and was never brought in line — the class of defect this codebase has paid for repeatedly when the same branch is hand-copied at multiple call sites instead of shared.

3. **`_start_grace_period()` always replaces any running grace.** `_cancel_grace_timers()` is the first call inside `_start_grace_period()` (L1469). Starting grace twice in succession cancels the first timer and begins a fresh one. The two cancel handles (`_manual_grace_cancel`, `_automation_grace_cancel`) are mutually exclusive: each call to `_start_grace_period()` sets only one of them based on `source`.

4. **Grace suppresses new pauses but not natural ventilation.** The early-return in `handle_door_window_open()` at L1053–L1066 exits only when `outdoor >= nat_vent_threshold`. When outdoor is cool enough, execution falls through to the nat-vent evaluation path. Grace does not unconditionally suppress all sensor-open behavior.

5. **Override and grace state are NOT restored across HA restarts (Issue #282 — clean slate).** `restore_state()` does not read `manual_override_active`, `grace_active`, `override_confirm_pending`, or their companion timestamps from the persisted dict. CA always enters clean automation mode after restart. Pause state (`_paused_by_door`, `_pre_pause_mode`) is not restored either (Issue #523); `_do_startup_coalesce()` re-derives it from live sensors. Previously (#227) the grace timer was restored to prevent indefinite lock; Issue #282 supersedes this with the clean-slate model and the startup coalescing window instead (occupant changes inside that window are not supported — Issue #1235).

6. **Automation grace is always the source after a door-close resume; manual grace is always the source after a user action.** The `source` parameter passed to `_start_grace_period()` is hardcoded at each call site — `"automation"` in `handle_all_doors_windows_closed()` (L1214, L1231) and `"manual"` in `handle_manual_override_during_pause()` (L1426), `resume_from_pause()` (L1459), `handle_fan_manual_override()` (L1426), and `_confirm_override()` (L1651).

7. **Debounce timers are cancelled when a manual override is detected during pause.** `_cancel_all_debounce_timers()` is called in `_async_thermostat_changed()` at L1919, immediately after dispatching to `handle_manual_override_during_pause()`. This prevents orphaned debounce timers from re-triggering a pause after the user has manually resumed HVAC.

8. **`_resumed_from_pause` flag is set only by `resume_from_pause()` (not in observe-only, Issue #1306) and cleared only by `clear_manual_override()`.** It is not set by `handle_all_doors_windows_closed()` (automatic resume) or by `handle_manual_override_during_pause()`. The flag drives the dashboard status string: `"resumed — door/window override"` vs `"grace period (automation|manual)"`.

9. **Confirmed as of Issue #1007 — an automation-sourced grace never replaces an already-active manual grace with real time remaining.** Previously this was flagged as "NOT confirmed (no code evidence found)": the exclusive-at-a-time behavior fell out purely of the unconditional `_cancel_grace_timers()` call at the top of `_start_grace_period_action()` (the real side-effecting method behind the `_start_grace_period()` wrapper — note this doc's line numbers below predate the Issue #664 FSM-split refactor that introduced that split), with no priority check between the currently-active grace and the one being started. Issue #1007's live incident: a user turned the WHF off (manual grace, ~3h), and minutes later the physical-drift-correction backstop's `source="automation"` call silently replaced it with a 300s automation grace, letting nat-vent reactivate the fan far earlier than the user was told to expect. Fix: `_start_grace_period_action()` now checks, before its unconditional cancel, whether `source == "automation"` and a grace is currently active with `_last_resume_source == "manual"` and real time remains (`_grace_end_time` is still in the future) — if so, it returns `False` (the same contract used for "grace disabled") without touching any grace state, and logs a WARNING. A fresh `source == "manual"` request still always replaces whatever is running (Invariant 6's "latest user action wins" is unchanged), and automation-over-automation replacement (e.g. `nat_vent_exit_resume` replacing `sensor_closed_resume`) is unaffected — only automation-over-manual is now guarded.

10. **The hard AC/WHF mutex always overrides RF-remote-timer-absolute protection (Issue #748).** Issue #486 made an active QuietCool RF remote timer "absolute" against routine automation second-guessing — `_deactivate_fan()` declines any fan-off request while `_fan_override_active` and `_fan_remote_timer_hours is not None`, logging a WARNING instead. Issue #714 separately established that a manual HVAC-mode override physically conflicting with an active WHF session must end that session immediately ("last setting placed wins"). These two rules directly contradicted each other until Issue #748: a WHF session started via an RF remote timer was immune to the hard mutex for the remainder of the timer (the 2026-08-22 live incident — AC and WHF both ran for 5+ minutes). `_deactivate_fan()` now accepts a `bypass_absolute_override` parameter, set to `True` only by the single shared helper `_stand_down_whf_for_override_conflict()` (used by all 4 manual-override-conflict call sites). Every other `_deactivate_fan()` caller — nat-vent cycling, bedtime, economizer-off, comfort-floor breach, etc. — is unaffected and still respects the RF-timer-absolute guard exactly as before. **Precedence, in order:** (1) hard AC/WHF mutex (a newly detected manual override to an active mode) always wins; (2) RF-remote-timer-absolute protection wins against everything else, including routine automation shutoffs.

---

## Timer Lifecycle

**Start:** `async_call_later(self.hass, duration, _grace_expired)` at L1540. Returns a cancel callable stored in either `_manual_grace_cancel` (source=`"manual"`) or `_automation_grace_cancel` (source=`"automation"`). `_grace_active` is set to `True` before the timer starts (L1481). `_grace_end_time` is set to an ISO timestamp of `now + duration` (L1483).

**End instants are UTC (Issue #1199).** Every stored "now + duration" end (`_grace_end_time`, `_fan_off_hold_until`, `_fan_override_until`, `_timer_boundary_settle_until`, `_fan_rate_limited_until`, `_startup_coalesce_expiry`, `_door_open_timer_expiry`, the comfort-hold `until_ts`) is a timezone-aware **UTC** datetime built by `time_utils.instant_after(seconds, now=None)` (`dt_util.utcnow() + timedelta`, or `as_utc(base) + timedelta` for a stored base). Adding a `timedelta` to an aware *local* datetime is wall-clock arithmetic and is one hour wrong across a daylight-saving change (a 1 h hold ran 1 h long at fall-back and, started before the spring-forward jump, ended early). Rules: (1) never write `as_local(as_utc(now) + d)`; (2) every display (status text, `ends=` log, Next-Automation, event text) converts with `dt_util.as_local()` before `strftime`; (3) comparisons against `dt_util.now()` are between aware datetimes and so compare as true instants. A persisted `grace_end_time` written before the fix carries a local offset (`-07:00`); both forms parse with `fromisoformat` and restore the same way. Scope: END instants only; elapsed time measured as `dt_util.now() - self._stored_local_now` is a separate, wider class (tracked separately).

**Cancel:** `_cancel_grace_timers()` (L3781) invokes both cancel callables if present, then sets `_grace_active = False`, `_grace_end_time = None`, and `_last_resume_source = None`. Called in:
- `_start_grace_period()` — beginning of every new grace (replaces previous timer)
- `cleanup()` — coordinator/engine teardown
- `cancel_override()` (Issue #508) — the deliberate-cancellation entry point, see below
- `coordinator._check_orphaned_grace()` (Issue #508) — the self-heal watchdog, see below
- Each of `_on_grace_expired()`'s three branches (Issue #508 dedup — previously each branch
  hand-inlined the same 5-field reset instead of calling this shared helper)
- Implicitly: any new call to `_start_grace_period()` cancels the previous via `_cancel_grace_timers()`

**Extend:** There is no explicit "extend" operation. A new `_start_grace_period()` call cancels the previous and starts a fresh full-duration timer. This is the effective extension mechanism, but it is not named or documented as such in code.

**Expiry callback name:** `_grace_expired` (inner closure defined at L1486). The `@callback` decorator marks it as a synchronous HA callback. It executes one of three branches:

| Branch | Condition | Action |
|---|---|---|
| Planned window | `_is_within_planned_window_period()` returns True | Clear grace flags silently; call `clear_manual_override()`; return |
| Re-pause | `_sensor_check_callback()` returns True (sensor still open) | Clear grace flags; call `clear_manual_override()`; schedule `_re_pause_for_open_sensor()` via `async_create_task`; emit `grace_expired` event with `re_paused=True` |
| Normal expiry | All sensors closed or no callback set | Clear grace flags; call `clear_manual_override()`; emit `grace_expired` event with `re_paused=False`; send notification if `should_notify` is True |

**`_re_pause_for_open_sensor()`:** Async method scheduled via `async_create_task`. Re-checks planned window period first (if within window, skips re-pause). Otherwise evaluates nat-vent conditions — if outdoor is cool enough for natural ventilation, activates nat-vent mode instead of re-pausing. Falls through to `_pause_for_door_window()` (shared with `handle_door_window_open()`, Issue #523): sets `_paused_by_door = True` in all cases; if HVAC was active, also captures `_pre_pause_mode`, calls HVAC off, and sends the `grace_repause` notification; if HVAC was already off, only the flag is set (no notification, no mode call) — both branches now emit a `sensor_opened`/`paused` activity event, which the off-mode branch previously did not.

**`_sensor_check_callback`:** Set by the coordinator after engine construction. Points to `coordinator._any_sensor_open()` which reads live HA sensor states for all `_resolved_sensors`. If `None` (e.g., in unit tests), the re-pause branch is skipped and grace expires normally.

---

## Deliberate Cancellation — `cancel_override()` (Issue #508)

**Problem:** Both dashboard "Cancel..." buttons ("Cancel Override" for a thermostat-mode override,
"Cancel Fan Override" for a fan-only override) mean the same domain action — "stop protecting this
override, hand control back to automation now" — but each API view composed
`clear_manual_override()` + `_cancel_grace_timers()` by hand. `ClimateAdvisorCancelOverrideView`
(Issue #41) got the pairing right from its first commit; `ClimateAdvisorCancelFanOverrideView`
(Issue #79, added 11 days later) never called `_cancel_grace_timers()` at all, in any version —
confirmed via `git log -p` to be an original design gap, not a regression. A user cancelling a
fan-only override via the dashboard saw `_grace_active` stay `True` for the rest of the original
grace duration (up to 8 hours for a QuietCool RF-remote timer selection), even though the override
it protected was already gone.

**Fix:** `AutomationEngine.cancel_override(reason: str = "user_cancel") -> bool`
(`automation.py:799`) is now the single entry point for deliberate cancellation:

1. No-op guard: if none of `_manual_override_active`, `_fan_override_active`, `_grace_active` is
   set, returns `False` immediately — nothing to do.
2. Calls `clear_manual_override(reason=reason)` (which unconditionally clears the fan-override
   flag too via `clear_fan_override()`).
3. Calls `_cancel_grace_timers()`.
4. Activity-log parity: `clear_manual_override()` only emits `"override_cleared"` when
   `_manual_override_active` was `True` — a fan-only cancellation would otherwise leave zero trace
   in the Activity Report. `cancel_override()` emits `"override_cleared"` itself in that case
   (`was_mode=None`, gracefully rendered by the existing `_render_override_cleared()`).
5. Forces the same post-processing a *natural* grace expiry always performs (see the "Normal
   expiry" row in the Timer Lifecycle table above), instead of relying on an incidental unrelated
   event or the next 30-minute classification cycle: calls `_post_grace_fan_check_callback()` (→
   `reconcile_fan_on_startup()`, re-evaluating live indoor/outdoor/sensor state) and
   `_request_refresh_callback()` (immediate coordinator refresh).
6. Returns `True` if something was actually cancelled.

Both `ClimateAdvisorCancelOverrideView.post()` and `ClimateAdvisorCancelFanOverrideView.post()`
(`api.py`) call `cancel_override()` with a distinct `reason` string
(`"user_cancel_override"` / `"user_cancel_fan_override"`) and then call the shared
`_schedule_reclassify_after_cancel()` helper, which schedules `apply_classification()` after a 10s
settle delay — the HVAC-mode-level counterpart to `cancel_override()`'s fan-reconcile call. This
scheduling logic previously existed only on the thermostat-cancel view; the fan-cancel view had no
equivalent at all, so cancelling a fan override never forced reconciliation of anything.

The internal `adopted_matching_decision` path inside `_on_grace_expired()`'s normal-expiry branch
also now calls `cancel_override(reason="adopted_matching_decision")` instead of hand-composing
`_cancel_grace_timers()` + `clear_manual_override()` in that order — the two existing call sites
had drifted to opposite call orders, more evidence neither treated this as one canonical operation.

**Post-override rule (Issue #1073, invariant R5):** when an override or grace ends, the system does not undo the user's action because of the overshoot it caused. The user's mode/setpoint counts as "that side ran" for the comfort-edge settling guard, so a shallow breach (within `comfort_deadband_{day_type}_f`) inside `comfort_family_recency_window_min` is held (`comfort_switch_held`) rather than flipped; a deeper breach still switches at once. Example: on a mild day the user cools the house by hand to 66 F (2 F under a 68 F floor); when grace expires the guard holds (depth 2 F equals the 2 F tolerance), and heat arms only if the house falls below 66 F or the window lapses. See [comfort-edge-selection-spec.md](comfort-edge-selection-spec.md).

**Not folded into `clear_manual_override()` itself:** `clear_manual_override()` remains the
low-level primitive used by `handle_bedtime()`, `handle_morning_wakeup()`, and the away/vacation
occupancy handlers. Each of those three then ends the cleared override's now-orphaned grace itself, through
`_end_grace_left_protecting_nothing()` (Issues #874, #1222; see [§ Occupancy Interaction](#occupancy-interaction)
and the "What Clears a Manual Override" callsite table below), so the coordinator's orphaned-grace watchdog
never has to reap it with an ERROR. That is a caller step, not part of the primitive: a caller that wants the
grace to survive would simply not call the helper. `cancel_override()` sits one level above, composing the primitive plus grace
cancellation plus reconciliation, only for callers that mean deliberate, immediate cancellation.

---

## Grace Observability and Ending a Grace-Only State (Issue #1095)

**Occupant problem:** a 3-hour `fan_off` grace silently held nat-vent back while a window was open and outdoor air was cooler. Nothing in the log or on the Status card said why, and the only enabled dashboard action ("Resume from Pause") cancelled it and started a *new* manual grace.

**Log lines** (all in `automation.py`):
- `Started <source> grace period (N seconds) — trigger=… source=… ends=HH:MM` (`_start_grace_period_action()`), the mirror of `Grace cancelled — reason= trigger= source= remaining_min=` (`_cancel_grace_intentionally()`). Both fragments come from `_grace_log_fields()` / the same field names.
- `Nat-vent evaluation skipped: reason=… ` / `Nat-vent evaluation not engaged: reason=…` (`_log_nat_vent_eval()`), emitted from every entry-evaluation exit of `check_natural_vent_conditions()`. Skip reasons: `no_sensor_open`, `hvac_calling`, `sensor_debounce_pending`, `grace_active`, `reactivation_lockout`, `temperature_unavailable`. `not engaged` reasons are listed in the order the nat-vent FSM decides (`override_active`, `grace_active` — which, like the FSM, does not apply when indoor exceeds `comfort_cool` (Issue #134), except during the occupant's fan-off hold (Issue #1205) — `reactivation_lockout`, then gate rejections from `nat_vent_gate.explain_nat_vent_gate()`); `fsm_declined` means no listed cause applied. Only the full gate is itemised, not the soft-start sub-gate. The previous-outcome memory resets when a nat-vent session starts/ends or a new grace starts, so a new blocker always logs at INFO (see `08-COMPUTATION-REFERENCE.md` §nat-vent gate). INFO when the (outcome, reasons) pair changes, DEBUG when it repeats. Also `Nat-vent not re-entered: occupant's fan-off hold wins over comfort-ceiling bypass indoor=… comfort_cool=… grace_remaining_min=…` (Issue #1205), INFO once per fan-off hold, only when the house is above `comfort_cool` (the case the #134 bypass would have let through).

**Status card:** every Status string that mentions a grace carries the cause and `— <duration> (ends HH:MM)` via `ClimateAdvisorCoordinator._grace_status_detail()` — including the door/window-paused string (`paused — door/window open · grace (manual) — WHF turned off — 3h (ends 9:45 PM)`) and `resumed — door/window override — 3h (ends …)`. The Issue #860 rule still applies: when `_fan_remote_timer_hours` is set the duration is omitted and the Fan card owns it. `dashboard_resume` is a `_GRACE_TRIGGER_LABELS` entry.

**Next Automation:** while `_grace_active` is true the nat-vent start candidate is *deferred*, not dropped: the crossing scan starts at the grace end time (`max(now, _grace_end_time)`) because the engine's idle-open re-entry is blocked by grace (Issue #620) and the card must not promise a time it will not honour. If the end time is unknown the candidate is withheld.

**Ending a grace without resuming:** `cancel_override()` already ends a grace-only state (`had_grace`). The dashboard button (`index.html`, `cancel-override-btn`) is now enabled on `manual_override_active || grace_active` (status API field `grace_active`) and labelled **End Grace Period** when only a grace is active. `ClimateAdvisorCancelOverrideView` answers `Grace period ended…` instead of `Override cancelled…` in that case. The door/window pause is not affected; "Resume from Pause" still starts a manual grace by design (Issue #47).

## WHF Safety Stop Ends the Fan Override and Grace (Issue #1144)

Occupant impact: when the whole-house fan is stopped because the house is sealed (rule 1) or
because the occupant turned the heat or AC on after it (rule 2), the fan override that the
occupant's own fan-on created ends with it. Otherwise the stopped fan would leave a grace
period that suppresses automation for the rest of its timer (up to the RF remote's 12 h).

`_after_whf_safety_stop()` calls `_end_fan_override_and_grace(reason="whf_safety_stop",
keep_grace=not sealed)`: the fan override, the RF timer token and a **fan-owned** grace
(`manual`/`fan_manual_override` trigger) end. A sealed-house stop clears the grace; a rule 2
(mutex) stop keeps a grace that another mechanism owns (it is the occupant's HVAC change that
is protected there). The ending is recorded as the `whf_safety_stop` override ending, which
household promise P4 lists as an allowed ending for fan overrides
([household-promises-spec.md](household-promises-spec.md)). Nothing here lets the safety stop
be suppressed *by* a grace: a safety stop never consults `_grace_active` or
`_fan_override_active`.

**The rule-2 stand-down ends the older fan override too (Issue #1204, owner decision A3).** Occupant
impact: you press the whole-house fan on, then set Cool; Climate Advisor stops the fan for your Cool (rule 2).
The fan card used to keep saying "off (manual override)" afterwards: for 3 h or more with Manual Change Pause 0,
and about 25 min past the fan run's own deadline with a pause (the override then waited for the Cool's grace).
`_stand_down_whf_for_override_conflict()` (the one implementation for all four manual-override-conflict call sites)
now calls `_end_fan_override_for_rule2_stop(result)` once the stop has gone out: the fan override is cleared with
reason `whf_safety_stop`, keep-grace semantics (no grace is cancelled; it covers the occupant's HVAC change next, and
blocks a nat-vent restart over a still-pending change, since pending is not a nat-vent blocker). It deliberately does
not wait for `_manual_change_protected()` like `_end_fan_override_and_grace()` does. The override-detected caller is a
task scheduled from `start_override_confirmation()` and clears only after its `await _deactivate_fan()`, by which time the
change is usually already pending; the clear does not rely on that ordering. A rate-limited (not sent) stop keeps the
override (#931 mirror). Observe-only (`dry_run`) keeps it too: `_deactivate_fan()` sends nothing and returns `DRY_RUN` (Issue #1304), so
the fan is still running (the WHF safety enforcer likewise does nothing in observe-only). Only a whole-house-fan
override is ended (`_is_whf_press()`): the thermostat blower never conflicts with heating/cooling, so a newer blower
override is never ended by a stand-down.
**The same applies to the WHF safety enforcer's own rule-2 (mutex) stop** (`_after_whf_safety_stop()`, sealed=False).
`_end_fan_override_and_grace(keep_grace=True)` leaves the fan override armed when its grace protects it and no
thermostat change is pending or confirmed, which is the case for an occupant heat/cool made while start-up coalescing
suppresses override detection (the fan card then read "off (manual override)" until the fan's grace ended, hours with
an RF timer). After the mutex stop the enforcer now ends the fan override the same way (`whf_safety_stop`) and, if
nothing is then protected, ends the grace via `_end_grace_left_protecting_nothing("whf_safety_stop")`; a pending or
confirmed change keeps its grace. The sealed-house (rule 1) stop is unchanged.
Edge: if the pending change then self-resolves (Cool on a hot day, inside the sensor debounce), the kept grace
protects nothing; PATH B ends it on purpose (`_end_grace_left_protecting_nothing()`, `grace_cancelled` reason
`override_self_resolved`, the `decide_orphaned_grace` predicate) instead of the orphaned-grace watchdog's ERROR.
Tests: `tests/test_p4_fan_override_end_1204.py`, `tests_ha/test_p4_fan_override_end_1204.py`.

**The cards show the ending at once (Issue #1264).** Occupant impact: after the sealed-house stop the Status card
kept reading "grace period (manual) — WHF override — 3h (ends …)" for another 15-26 s, although the grace had ended.
The cards read `coordinator.data`. The stop runs from the enforcer's settle/tick/verify timers, and on a dual-entity
install the transmitter's off reading returns before the listener's refresh, so the card waited for the power entity's
lagging off reading. `_end_fan_override_and_grace()` (the sealed and rule-2 stops) and
`_end_grace_left_protecting_nothing()` (leaving home, bedtime, wake-up, PATH B) now push the cards
(`_refresh_cards_after_ending()` → the coordinator's `_push_override_card_status()`) once their ending is complete, after
every flag is cleared. The push is display only: it re-renders the Status, Fan, WHF and HVAC-fan card fields in
`coordinator.data` and notifies the entities, without a coordinator refresh. A refresh runs the automation cycle, and
pulling one forward here decided over an occupant heat/cool that start-up coalescing had left unprotected (it forced the
heat off for an open window seconds after the rule-2 stop). `cancel_override()` and the grace expiry keep their full
refresh.
**A sealed stop that finds the fan already off** (`_deactivate_fan()` returns `ALREADY_IN_STATE`: the snapshot saw the
fan on, the physical read is off by the command) ends the fan override and grace the same way
(`_end_fan_override_and_grace(reason="whf_safety_stop")`); before #1264 they ran on for the rest of the timer. Nothing
was commanded, so it is not counted as a stop for the anti-oscillation alert.
Tests: `tests/test_whf_stop_grace_cards_1264.py`, `tests_ha/test_whf_stop_grace_card_1264.py`.

---

## Orphaned Grace Self-Heal (Issue #508)

**Shape of the problem:** the opposite of the pre-existing Issue #321 stuck-grace check (below).
Issue #321 catches `_grace_active=False` with `_grace_end_time` in the past (a timer that should
have fired but didn't — override stuck active). This check catches `_grace_active=True` with
no override active at all — `_grace_end_time` is typically still validly in the future (the timer
will fire correctly on its own), but nothing is being protected. Defense-in-depth for any path that
clears an override without going through `cancel_override()` — an exception mid-cancel, or a future
third endpoint that composes the primitives by hand again.

**Corrected scope (Issue #530) — only grace that claims to protect an override can be "orphaned":**
The original implementation inferred "orphaned" purely from `_manual_override_active`/
`_fan_override_active` both being `False`. That is also the *normal, by-design* shape of every
grace type that was never about an override in the first place — fan-off cooldown (Issue #359),
physical-drift-correction, window-close resume, nat-vent-exit resume, and dashboard resume all
start grace without ever touching either flag. The watchdog could not tell "nothing to protect
because nothing is broken" from "nothing to protect because something clobbered the flag," and
killed the former just as eagerly as the latter — confirmed live: a whole-house-fan session's
fan-off grace was destroyed within roughly one event-loop tick of starting, because the same fan
transition that started it also requested an immediate coordinator refresh (Issue #510), and that
refresh's `_check_orphaned_grace()` call ran before anything else had a chance to protect it. This
defeated Issue #359's fan-off protection for essentially every whole-house-fan-off, not just the
RF-remote-timer scenario #530 was originally reported against.

**Fix:** `_start_grace_period(trigger=...)` now sets `self._grace_protects_override = trigger in
_GRACE_TRIGGERS_PROTECTING_OVERRIDE` (`automation.py`) — a 2-item frozenset containing only
`"fan_manual_override"` and `"override_confirmed"`, the two triggers that genuinely correspond to
a real override having just been set. This is a **centralized classification, not a parameter
threaded through every callsite**: every `_start_grace_period()` caller already passes a distinct
`trigger` string (pre-existing, used for logging/events), so a future 8th callsite is automatically
excluded unless its trigger is deliberately added to the set — there is no new per-callsite
judgment call to get wrong or forget. `_cancel_grace_timers()` resets `_grace_protects_override`
to `False` alongside `_grace_active` so a stale `True` can never leak into whatever starts next.

**Implementation:** `coordinator._check_orphaned_grace()` (`coordinator.py:1629`), called once per
coordinator update (`_async_update_data_impl`, under `if forecast:`) -- the 30-minute `update_interval` plus on-demand refreshes (5-minute revisit, sensor and fan events), not every ~30 s. The decision is `override_orphaned_grace.decide_orphaned_grace()`. Condition: `ae._grace_active and
ae._grace_protects_override and not ae._manual_override_active and not ae._fan_override_active and not ae._override_confirm_pending` (Issue #1216: a change still pending in its confirmation window keeps the grace; before this, Cool then Off inside Cool's grace lost the Off because the next update reaped the superseded Cool grace and the pending change went with it).
On match: logs an ERROR, calls `ae._cancel_grace_timers()`, and emits `"stuck_grace_recovered"`
with `{"grace_end_time": ..., "reason": "grace_without_override"}`.

**Renderer note:** `_render_stuck_grace_recovered()` (`ai_skills_context.py`) branches on the
`reason` field — the pre-existing Issue #321 call site never sets it and keeps its
`"Grace period recovered (expired {grace_end})"` label; this call site renders `"Grace period
recovered (no override was active)"` instead, since `grace_end` is misleadingly labeled
"expired" when it's actually still in the future. (Issue #913 renamed both labels from
"Stuck grace recovered..." to "Grace period recovered..." to drop the "stuck" jargon — the
branching logic described above is unchanged.)

**These two watchdog checks are mutually exclusive** — one requires `_grace_active=False`, the
other requires `_grace_active=True` — and run independently in the same update cycle.

---

## RF-Timer Boundary Settle Window (Issue #530)

**Problem:** A manual grace tied to an RF-remote timer (`_start_grace_period(duration_override=...)`
from `handle_fan_manual_override()`, e.g. an 8-hour QuietCool selection) tracks that timer in
software. The physical remote/fan hardware runs the *same* timer independently and completes it on
its own clock — confirmed live at an ~11-second gap, with follow-on RF chatter settling within
~60 seconds total. When CA's software-side grace expires first (the common case), its normal
expiry path (`clear_manual_override()` → `clear_fan_override()`) discards `_fan_remote_timer_hours`
immediately — so by the time the hardware's own fan-off report arrives seconds later,
`on_fan_turned_off()` has zero memory a timer was ever involved and treats it as a brand-new,
unrelated event: a fresh fan-off grace starts, gets killed by the (pre-#530) orphaned-grace
watchdog, the fan gets re-armed 5 seconds after the user/timer turned it off, and that re-arm can
itself trigger a further RF override detection — three independent grace/override cycles from one
physical action.

**Fix — treat it as the same transition, not a new event:** `_on_grace_expired()` snapshots, before
`clear_manual_override()` runs, whether the expiring grace's `_fan_remote_timer_hours` was set. If
so, it arms `self._timer_boundary_settle_until = now + TIMER_BOUNDARY_SETTLE_SECONDS` (`const.py`,
120s — generous headroom over the observed single-digit-second real gap). `on_fan_turned_off()`
checks this window first, before anything else: if the fan-off lands inside it, the window is
consumed (one-shot) and the event is **not** treated as fresh —
- If `_natural_vent_active` is `True` (the post-grace reconcile had already adopted the still-running
  fan as a nat-vent session), the fan-off is routed straight to `_exit_nat_vent()` — the single
  choke point for ending a session, which correctly checks live sensor state (pauses if a monitored
  sensor is still open; restores HVAC + starts an ordinary `"automation"`-sourced grace if closed).
- Otherwise, the fan flags are simply reconciled to "off" with no new grace at all.

Either way, **no second, independent fan-off grace is started** for a timer-boundary fan-off — this
is a stronger guarantee than "the fan-off grace now survives the orphaned-grace watchdog" (the
general fix above): it skips the redundant grace/session entirely, matching the fact that CA
already knew this fan-off was coming.

**Relationship to the Orphaned Grace Self-Heal fix above:** complementary, not overlapping. The
watchdog fix (Issue #530, previous section) restores fan-off grace surviving its full duration for
*any* fan-off, timer-linked or not. This settle window additionally recognizes the *specific*,
predictable case of a timer-linked fan-off and resolves it immediately instead of even starting a
grace — per the project's operating model, an automation outcome CA can already predict should be
handled as a known continuation, not re-discovered from scratch by a second mechanism.

**Location:** `automation.py` — `_on_grace_expired()` (arms the window),
`on_fan_turned_off()` (consumes it), `_timer_boundary_settle_until` (instance attribute,
reset alongside grace's other one-shot state). `const.py` — `TIMER_BOUNDARY_SETTLE_SECONDS`.

---

## Manual-Origin Unclosed Marker (Issue #829, Issue #1205)

### The rule (Issue #1205, owner decision 2026-11-01)

**What the occupant experiences:** when someone in the home switches off a fan (the whole-house fan in
`whole_house_fan` mode, the thermostat blower in `hvac_fan` mode), the fan stays off for the fan-off pause (the
fan-off grace, "hold"). That includes a fan Climate Advisor took over or restarted after an earlier manual fan-on.
Before #1205 an off of that kind started no hold, and with a window open and cool air outside CA switched the
fan back on within about 6-13 minutes.

**Every fan-off that Climate Advisor did not command and that falls outside the selected-timer settle window
starts the normal fan-off grace (hold), in both supported fan modes.** CA must not restart the fan during it: no nat-vent path, no economizer start, and not the Issue #134 comfort-ceiling bypass (a house above `comfort_cool` does not end the hold; that bypass is for CA's own graces only, see [§ Fan-Off Grace](#fan-off-grace-issue-359)). `_activate_fan()` refuses any CA start during the hold as the backstop. The hold runs to its OWN deadline: a thermostat override replacing the grace does not end it (see [§ Fan-Off Grace](#fan-off-grace-issue-359)).
`FAN_MODE_BOTH` is unsupported (#1175).

**Why: CA always knows how a run should end.**
- A remote-selected timer (1/2/4/8/12 h) is tracked by time. When its grace expires, the 120 s settle window
  (see [§ RF-Timer Boundary Settle Window](#rf-timer-boundary-settle-window-issue-530)) recognizes the hardware
  timer's own off. That is the only fan-off that starts no hold.
- With no timer selected the QuietCool would run indefinitely, so CA applies a default timer
  (`manual_grace_seconds`; the remote's `timer_none` token uses the same default, see `fan-remote-spec.md`). At its
  expiry the post-grace reconcile either turns the fan off itself (`decision="turn-off"`: CA's own command, which
  never reaches `on_fan_turned_off()`) or keeps it running as CA's own nat-vent run (`decision="adopt-on"`).
- All other CA-initiated stops (nat-vent exit, safety/mutex stop, cycling off, reconcile turn-off) are CA's own
  commands and start no hold either.

Therefore a fan-off CA did not command, outside the settle window, came from a PERSON. The premise of the
original #829 fix, that such an off is "the expected end" of the manual session, was wrong.

**The marker is label-only.** `_fan_manual_origin_unclosed` (with its run binding and pending fact, below) no
longer controls any behavior. It ONLY selects the Activity Record / `fan_cancel` / log wording of the occupant's
off:
- matched marker, `fan_cancel` trigger `manual_origin_carryforward`, rendered by
  `ai_skills_context.py::_render_fan_cancel()` from the payload's `manual_origin_kind` and
  `manual_origin_started`:
  - kind `takeover` (armed by `clear_fan_override()` or the reconcile `adopt-on`): "Fan off -- you turned off the
    fan Climate Advisor took over from your earlier manual fan-on (started HH:MM); holding the fan off"
  - kind `restart` (armed by the grace-expiry reactivation, applied or deferred-then-applied): "Fan off -- you
    turned off the fan Climate Advisor restarted after your grace ended (started HH:MM); holding the fan off"
  - no kind in the payload: "Fan off -- closing the session from your earlier manual fan-on; holding the fan off"
- stale or unmatched marker (or none), trigger `fan_off`: the plain "Fan cancel (user turned off)" wording.

`on_fan_turned_off()` runs the SAME path for both: `fan_cancel`, then `_clear_fan_flags_and_start_grace()` (flags
cleared, nat-vent session ended, the fan-off grace started, FSM kind `FLAGS_CLEARED_FOR_GRACE`), then
`_release_whf_and_reclassify()`. Only the settle-window branch returns early. A matched off never routes through
`_exit_nat_vent()` without a grace.

**Grace expiry with a window still open** is unchanged: when the hold ends and the window is still open, CA
restarts the fan (normal behavior). It is visible in the Activity Record as `nat_vent_reactivated_while_paused`
(and `fan_activated`). A person's off of that restarted run is again held, and labelled "restarted after your
grace ended".

### Incident narrative (Issue #829) and its correct reading

Confirmed live (2026-09-02/03): a WHF-on with no timer at 17:37 armed a 180-minute default override grace. That
grace expired in software at 20:37 while the fan was still physically running and nat-vent conditions favored
keeping it on, and `clear_fan_override()` discarded `_fan_override_active`, `_fan_override_time`,
`_fan_remote_timer_hours` and `_fan_remote_speed`, silently handing ownership to nat-vent with no record the run
had ever been user-initiated. The fan's real off, ~5 hours later (the 01:37 off), was read by
`on_fan_turned_off()` as a brand-new unexplained override ("another manual override") and started a fresh
180-minute grace. The same shape repeated one level down: that grace expired with the sensor still open,
`_re_pause_for_open_sensor()` reactivated nat-vent on CA's own initiative, and ~93 minutes later the fan's real off
was again read as fresh.

**Correct interpretation (Issue #1205):** the 01:37 off was a person, and a fan-off grace after it was the right
outcome. The real #829 complaint was the misleading LABEL ("another manual override"), not the grace. The #829 fix
removed the grace (it treated the off as the expected end of the manual session and skipped the hold) to fix a
label, which is what let CA switch the fan back on. #1205 restores the hold and keeps only the label fix.

The RF-timer settle window cannot cover the no-timer case: it works because a hardware timer's clock and CA's
software clock agree to within seconds, whereas a person's off of a run CA took over comes minutes to hours later.
That is not a reason to skip the hold; it is the reason the off needs the normal hold and its own wording.

### What arms the marker, and run binding

`self._fan_manual_origin_unclosed: bool` is set (not by elapsed time, but by the handoff event itself) where CA
hands a still-running or freshly-reactivated fan to itself without having seen the end of a manual session:
- `clear_fan_override()` (A1): the fan is still `_fan_active` and `_fan_remote_timer_hours is None` when the
  override flags are cleared. `_fan_active` implies a stamped CA-owned run; an occupant-started fan never sets
  `_fan_active` and never reaches this arm. Kind `takeover`.
- the reconcile adopt on `trigger == "post_grace_expiry"` (A2; `ha_restart` has its own RF-timer provenance
  handling per Issue #677, and `backstop_30min` / `thermostat_state_change` adopting a running fan is routine
  reconciliation). Kind `takeover`.
- `_re_pause_for_open_sensor()` when the grace-expiry reactivation is applied (A4), and the deferred-then-applied
  case (A3, below). Kind `restart`.

**Re-arm and clear (Issue #1205, label only).** A re-arm for the SAME run overwrites the kind and site (the newest
arm owns the wording; the same kind and site again is a no-op). The one exception is deliberate: the grace-expiry
reactivation site (A4) does not relabel a run that was already running and already marked (`ALREADY_IN_STATE`, for
example the post-grace reconcile adopted the person's run a moment earlier, kind `takeover`), because CA restarted
nothing. When the person turns the fan on again or selects a remote timer (`handle_fan_manual_override()`), an armed
marker is cleared (`_clear_fan_manual_origin()`, INFO `Fan manual-origin marker cleared: reason=occupant_fan_override
...`): the run is theirs now, and their later off carries the plain wording, not "restarted after your grace ended".
Either way the off starts the hold.

All arms go through ONE helper, `_arm_fan_manual_origin(site, reason, kind=..., started=...)`, which sets the bool,
`_fan_manual_origin_run` (the `_fan_on_since` of the run being handed to CA, stamped fresh on every successful
start), the kind and the start time together, and arms nothing when there is no live run to bind to. The consume,
`_consume_fan_manual_origin()`, returns the label provenance only when the marker's run equals the current
`_fan_on_since` (the run turning off now); a marker armed for another run, or without a run identity, is
discarded as stale (INFO with `armed_run` and `current_run`) and the plain wording is used. Either way the hold
starts. Binding keeps the label honest: CA stopping the marked run itself (its off never reaches
`on_fan_turned_off()`) cannot leave a marker that mislabels a later, unrelated run (R1/R2 below).

**A3 (rate-limited reactivation):** `_re_pause_for_open_sensor()` records a *pending* fact
`_fan_manual_origin_pending = (deferral applies_at, _fan_start_seq)` when a deferred "activate" is really recorded
(#1196 gate); no marker is armed, because no run exists yet and none is guaranteed. It is resolved in
`_activate_fan()` itself, on a successful start, right after the run is stamped: promoted to the run-bound marker
only when that start applied the recorded deferral (same `applies_at`, no other start counted since in
`_fan_start_seq`). Promotion is caller-independent (the paused-reactivation helper for PAUSED, the idle-open
re-evaluation in `check_natural_vent_conditions()` for PAUSED_IDLE). Any other successful start, the occupant's
fan action, or the door/window pause genuinely ending discards it. The pause ending is caught at one choke point,
the `_paused_by_door` property setter; a same-handler save-and-restore of the flag is not an end
(`_holding_door_pause()` wraps it at the two idle-open sites). Discarding also drops the abandoned
`_fan_rate_limited_*` activate record, and since #1212 that record is cleared whether or not a pending fact exists:
`_clear_fan_deferral_record()` is the one choke point (called by the pause-end setter, `handle_all_doors_windows_closed()`,
the occupant's fan on/off, and by `_activate_fan()`/`_deactivate_fan()` for the opposite direction, except the reconcile "no-fan" bookkeeping call, which
passes `abandons_deferral=False` because the paused-reactivation retry still applies that start); when it clears an
"activate" record a pending fact bound to it is dropped in the same step, so the two never disagree. A record more than 2100 s past its `applies_at` is expired the same way before the next `_activate_fan()`/`_deactivate_fan()` reads it (Issue #1247), so a start that lands that late does not promote the pending fact (label only). Pending fact, marker, run, kind and start time are not persisted.

Logging (INFO, outcome first): `marker armed (label only)`, `marker not armed, no live run to bind it to`,
`pending raised`, `pending promoted`, `pending discarded: reason=...`, `marker consumed, run matched (label
only)`, `marker discarded as stale (plain label): reason=... armed_run=... current_run=...`, and the off itself:
`Fan turned off by occupant — holding fan off (fan_off grace); closes session from earlier manual fan-on:
fan=... kind=... run=... started=... armed_site=...` (matched) or `Fan turned off by occupant — holding fan off
(fan_off grace): fan=...` (plain).

### Where the #1205 bug came from

Two routes left the marker set after the run it described was gone, so the NEXT, unrelated run's off was also
misread as "closing an earlier manual session" (no hold):
- **R1 (abandoned deferral):** the toggle-floor-deferred reactivation armed the marker, but nothing retries it
  except a later `check_natural_vent_conditions()` cycle while the pause lasts; if the window closed first the fan
  never ran and the marker outlived it.
- **R2 (CA stops the run itself):** the marker was armed for a running fan, then CA stopped that fan (nat-vent exit
  on window close, safety/mutex stop, cycling off); CA's own off never reaches `on_fan_turned_off()`.

Run binding and the pending fact fixed the marker's lifetime; the owner decision then made the hold independent of
the marker altogether (the matched case had the same symptom: a person's off of the marked run itself was ignored).

**Cycle-off/on inside a marked run:** CA stops the marked run and starts another (a nat-vent cycle). The new run
has a new `_fan_on_since`, so the person's off of it uses the plain wording (it is a nat-vent run CA started, not
the one it took over or restarted). The hold starts either way.

**Relationship to the RF-Timer Boundary Settle Window:** both are consumed at the top of `on_fan_turned_off()`.
They differ in effect: the settle window (a selected timer ending on schedule) returns early with no hold, trigger
`timer_boundary_settle`, FSM kind `TIMER_BOUNDARY_SETTLE` (unchanged by #1205); the marker never returns early.

**Location:** `automation.py` — `_fan_manual_origin_unclosed` / `_fan_manual_origin_run` / `_fan_manual_origin_kind` /
`_fan_manual_origin_started` / `_fan_manual_origin_site` / `_fan_manual_origin_pending` (instance attributes),
`_arm_fan_manual_origin()` (sole arm writer, called from `clear_fan_override()` / `_re_pause_for_open_sensor()` /
`_reconcile_fan_on_startup_locked()` and, on promotion, `_resolve_fan_manual_origin_pending_on_start()` called from
`_activate_fan()`), `_raise_fan_manual_origin_pending()` / `_discard_fan_manual_origin_pending()`, the
`_paused_by_door` setter and `_holding_door_pause()`, and `on_fan_turned_off()` → `_consume_fan_manual_origin()`
(label only). `ai_skills_context.py` — `_render_fan_cancel()` (renders `manual_origin_carryforward`).

---

## Fan-Entity Availability Blips Misread as Manual Action (Issue #787)

**Problem:** the settle window above only covers a fan-off that arrives within 120s of a
*previously software-tracked* remote-timer grace expiring. It does not cover a `fan_entity`
briefly reporting `unavailable` at an arbitrary moment with no preceding grace at all — e.g.
a transient network/API dropout between Home Assistant and the fan's own Wi-Fi module,
confirmed live (Issue #787) via `aioesphomeapi.reconnect_logic`'s `EncryptionHelloAPIError`.
`coordinator.py::_async_fan_entity_changed()` had no special case for `unavailable`: it
computed `is_on = new_state.state in {"on"}`, so `on->unavailable` dispatched as a manual
cancel (`on_fan_turned_off()`, starting a fresh `"fan_off"`-triggered "unprotected" grace) and
the recovery `unavailable->on` dispatched as a fresh manual override
(`handle_fan_manual_override()`, starting a "protected" 3-hour manual grace) — two bogus
grace periods from one non-event, and the fan was never actually off.

**Fix:** when either side of a `fan_entity` state transition is `unavailable`,
`_async_fan_entity_changed()` now cross-checks `_get_fan_physical_state()` (the same
ground-truth `fan_state_entity` reader built for Issue #363's status-sensor fallback) before
deciding what to dispatch. If ground truth is available and shows no real corresponding
change, the transition is treated purely as a connectivity blip — no override, no cancel, no
grace. If ground truth disagrees with what CA currently believes (a genuine state change
that happens to coincide with an availability blip), the existing dispatch logic still fires
correctly, now driven by the ground-truth value instead of the transient raw state. Type-1
installs with no `fan_state_entity` configured have no ground truth to check; an
`unavailable` transition there is currently skipped rather than guessed at (a
minimum-duration debounce is a tracked follow-up, not yet implemented).

**Relationship to the RF-Timer Boundary Settle Window above:** independent, not overlapping.
That fix resolves a *known, software-anticipated* timer-linked fan-off with no ground-truth
entity involved. This fix resolves an *unanticipated* connectivity blip on the fan-command
entity itself, using a *different* signal (a separate ground-truth sensor) that may not even
be configured on every install.

**Location:** `coordinator.py` — `_async_fan_entity_changed()` (the `unavailable` branch),
`_get_fan_physical_state()` (the ground-truth reader, pre-existing).

**Safety-rule stamp (Issue #1178):** the WHF safety enforcer's ground-truth "when did the occupant turn the fan on"
stamp (`_occupant_fan_on_at_from_ground_truth()`) must not count a fan entity that woke from unavailable/unknown
already on. `_async_fan_entity_changed()` records that wake-up time (`_whf_reconnect_changed_at`, using the same
`is_availability_recovery()` predicate the thermostat listener applies for Issue #1049) before any early return, and
the stamp ignores an on-time within `WHF_OWN_COMMAND_ECHO_S` of it.

---

## Pre-Pause Mode Storage

*(See also the invariants section above for the full storage contract.)*

**Serialized fields in `get_serializable_state()` (Issue #282, updated by Issue #1006):**

Override and confirmation-window fields are NOT included in the serialized state. `get_serializable_state()` omits `manual_override_active`, `grace_active`, `override_confirm_pending`, and their companion timestamps entirely — there is no point saving what is not restored. As of Issue #1006, grace *timing* fields — `grace_end_time`, `grace_duration_seconds`, `last_grace_trigger`, `last_resume_source` — ARE now serialized unconditionally (cheap, always-current); `restore_state()` alone decides whether to act on them (see below and [§ Fan-Off Grace Restart Persistence](#fan-off-grace-restart-persistence-issue-1006)). `grace_active` itself remains a derived FSM flag, not a timing field, and stays unserialized.

Pause state fields ARE serialized:
```
"paused_by_door": bool
"pre_pause_mode": str | None
```

**Fields restored in `restore_state()` (Issue #282 — clean slate, narrowed by Issue #1006):**
```
_paused_by_door         ← "paused_by_door"   (default: False)
_pre_pause_mode         ← "pre_pause_mode"   (default: None)
_manual_override_active = False              (always reset — NOT read from state)
_grace_active           = False              (reset here — MAY be re-armed True afterward, see below)
_override_confirm_pending = False            (always reset — NOT read from state)
_last_resume_source     = None               (reset here — MAY be re-armed to "manual" afterward)
_grace_end_time         = None               (reset here — MAY be re-armed afterward)
```
After the above clean-slate reset, `_maybe_restore_fan_off_grace(state)` runs and re-arms
`_grace_active`/`_grace_end_time`/`_last_resume_source` (and starts a real expiry timer) ONLY
when the persisted `last_resume_source == "manual"` AND `last_grace_trigger == "fan_off"` AND the
persisted `grace_end_time` is still in the future. See below.

---

## Occupancy Interaction

**Occupancy mode changes do not alter grace periods, with one exception: leaving home ends the grace of the override it clears (Issue #1222).** There are no calls to `_cancel_grace_timers()` or `_start_grace_period()` inside `handle_occupancy_away()`, `handle_occupancy_home()`, or `handle_occupancy_vacation()`.

The exception: when `_apply_occupancy_away_vacation_decision()` clears a manual override or a pending confirmation (`decision.clear_override`), the grace that was protecting that override has nothing left to protect. Before #1222 it kept running (dashboard "Grace period active" in an empty house already in setback) until the coordinator's `_check_orphaned_grace()` watchdog reaped it with an ERROR "Stuck grace detected" (live: 2026-11-01 13:07:41 to 13:12:41). The handler now snapshots `_fan_remote_timer_hours` BEFORE `clear_manual_override()` (the clear wipes it) and calls `_end_grace_left_protecting_nothing(f"occupancy_{mode}", remote_timer_hours=...)`, the same helper bedtime and wake-up use (#874). The helper's predicate is the watchdog's own `decide_orphaned_grace()`, so only an override-protecting grace (`fan_manual_override`, `override_confirmed`) ends: it cancels the timers (`grace_cancelled`, reason `occupancy_away` / `occupancy_vacation`, `source` automation) and dispatches `OVERRIDE_CANCELLED` to the override/grace FSM. A fan-off hold or grace, a window-close resume, a dashboard resume and a nat-vent-exit resume are never override-protecting and run on untouched; a fan-off hold survives away/vacation. Nothing is cleared (no override) means nothing is ended. The home path has no mirror: `handle_occupancy_home()` never clears the override, it defers its own restore while one is active (Issue #1050).

**Away/vacation mode and pause:** If occupancy transitions to away or vacation while `_paused_by_door = True`, the pause flag persists. The occupancy handlers apply temperature setbacks to the thermostat (via `_set_temperature()`) but do not call `_set_hvac_mode()`, so HVAC remains off as it was during the pause. When sensors close and `handle_all_doors_windows_closed()` fires, `_pre_pause_mode` is restored to whatever mode was active when the pause began — which may now be inconsistent with the setback temp applied by the occupancy handler. There is no reconciliation step between pause restoration and current occupancy mode at the `handle_all_doors_windows_closed()` level. However, `_set_temperature_for_mode()` (called during resume) internally routes through occupancy-aware logic via `_set_temperature_for_mode()` — which checks `_occupancy_mode` and applies setback if away/vacation.

**Away mode and door-open suppression:** Away/vacation mode does not skip the debounce or pause flow. If HVAC is running in heat/cool mode while occupancy is away (e.g., pre-conditioning), a door opening will still trigger the debounce and potentially pause HVAC.

**Grace expiry during occupancy-away:** The `_grace_expired` callback does not read `_occupancy_mode`. All three branches (planned window, re-pause, normal expiry) execute the same logic regardless of current occupancy state.

---

## Error Conditions

### Door/Window Sensor Goes Unavailable During Grace

**Owner rule 3: HVAC stays paused while a door/window is open, unless the occupant turned it on.** A sensor that goes dark (unavailable, unknown or missing) must not decide that for the house. Issue #1181 (v0.8.15) replaced the older behaviour, where a dark sensor read as **closed** and so *ended* a pause (the `open -> unavailable` event took the close branch of `_async_door_window_changed()`, `handle_all_doors_windows_closed()` ran, and HVAC was restored with the window still open). The earlier text of this section ("stays paused indefinitely") was wrong in the other direction.

The rule (decision D1), with one authoritative helper per question in `coordinator.py`:

| Question | Helper | A dark sensor counts as |
|---|---|---|
| Does a sensor *start* a pause / is it open right now? | `_is_sensor_open()` -> `_state_is_open()` | closed (never starts a pause; unchanged) |
| Does a sensor *keep or end* a pause / restore HVAC? | `_sensor_holds_open()` / `_any_sensor_holding_open()` (engine: `_any_monitored_sensor_holding_open()` via `AutomationEngineCallbacks.sensor_hold_check`) | open **only if its last REAL reading was open**; closed if it last read closed or never reported |

`_sensor_real_reading(state)` is the one definition of a real reading (`on`/`off` through polarity; anything else is dark). The last real reading per sensor is `_sensor_last_real_reading`, written by `_record_sensor_reading()` (every door/window event's old and new state, and `_sync_sensor_last_readings()` each update cycle), **persisted** as `sensor_last_real_reading` in the state file (`_build_state_dict()`; restored in `async_restore_state()` before the same-day check, because an outage can span midnight) and pruned when a sensor is no longer configured. There is **no time limit**: a pause held by a dark sensor ends only when that sensor reports a real reading (closed -> the normal close path) or the occupant changes the thermostat (the existing manual-override exception; grace expiry with the sensor still dark re-pauses exactly as with a real open sensor).

Sites that use the holding read (all the pause-ending / HVAC-restoring decisions): the all-closed test in `_async_door_window_changed()`; `_apply_comfort_band()`, `_sync_paused_by_door_with_live_sensors()`, `_override_decision_mode_and_target()` and `_occupancy_restore_hold_reason()` (via `decide_open_sensor_hold`, fed the per-sensor settled read `_any_monitored_sensor_settled_open()` since Issue #1229 — still holding-based, so a dark sensor that last read open counts); `_on_grace_expired()`'s re-pause test; `_exit_nat_vent()`'s pause-or-restore choice; `_override_decision_mode_and_target()`, `start_override_confirmation()`'s self-resolved window re-check, the door/window and override/grace FSM input builders (`_build_door_window_fsm_inputs()`, `_build_override_grace_fsm_inputs()`) and `_occupancy_restore_hold_reason()`; the sealed-house-stop release `_complete_whf_safety_release()` and the fan-off branch of `reconcile_fan_on_startup()` skip their pre-fan HVAC restore while it is True. Nat-vent and fan **start** decisions keep the plain open read (the idle-open restart gate asks it per sensor, past each sensor's own delay -- Issue #1289). `_on_grace_expired()` keeps the holding read for its re-pause, but its nat-vent start prediction and `_re_pause_for_open_sensor()`'s activation, like the paused-by-door reactivation, also require a sensor reading open past its delay (`_no_live_open_sensor_for_start()`, Issue #1289): a dark sensor holds the pause but never starts the fan. The quiet-pause prediction `_fan_session_about_to_take_over()` asks the same predicate, so a pause held only by a dark sensor is announced, never kept quiet for a fan that cannot start (Issue #1298).

A dark sensor is announced to the occupant through the entity-health path (see [entity-health-brief.md](entity-health-brief.md)) with a per-sensor key and a rule-aware message, and the dashboard's Doors/Windows tile lists it as "not responding" with its last reading instead of "all closed". When the sensor returns as `on`, `is_availability_recovery()` still skips the debounce (Issue #645). Tests: `tests/test_unreadable_sensors_1181.py`; pending scenario `issue_1181_dark_window_sensor_keeps_cooling_paused`.

`_is_sensor_open()` is a thin wrapper over `_state_is_open()`, which `_async_door_window_changed()` also applies to the event's `old_state`; an unavailable/unknown/missing state is never "open". An `open -> unavailable` event still fires the handler: it records the last real reading (open), sees that the sensor now holds the pause, and does nothing else.

**Re-reports are skipped, not processed (Issue #1050, #1048 change 19).** A `closed -> closed` or `unavailable -> closed` re-report (every contact sensor re-registers after an HA restart) carries no information. `_async_door_window_changed()` skips it with a DEBUG line (`Contact sensor closed re-report skipped`) **unless something could need clearing**: a door pause (`_paused_by_door`), an active nat-vent session (`_natural_vent_active`) or a live fan session (`_fan_active` / `_fan_override_active`) -- `handle_all_doors_windows_closed()` also ends those, so a persisted pause must still clear after a restart. `old_state=None` (no prior state at all) always proceeds, and `open -> unavailable` / `open -> closed` always proceed. Before this change each restart logged ~48 identical "all sensors closed" rows. Tests: `tests/test_door_window_restart_flood.py`.

There is still no polling path that periodically re-evaluates a stuck sensor: a sensor that stays `open`-but-silent (no event) leaves the pause in place until a manual resume, another sensor's close event, or an HA restart (which preserves the pause state). A sensor that is *dark* is different: it is held (above), announced, and its last reading is persisted.

### HA Restart During Grace

**Issue #282 — clean slate, narrowed by Issue #1006.** Override state is NOT restored after
restart. `_manual_override_active` and `_override_confirm_pending` are always reset to `False`.
`async_restore_state()` (the coordinator-level method) no longer calls `_reschedule_grace_timer()`
— see [§ Fan-Off Grace Restart Persistence](#fan-off-grace-restart-persistence-issue-1006) for the
one narrow exception, which lives entirely inside `AutomationEngine.restore_state()` and does not
change this coordinator-level behavior. CA otherwise resumes in clean automation mode after every
restart.

The 5-minute `_first_run` settling window prevents the system from taking any HVAC action
immediately after startup, allowing the thermostat to stabilize before CA evaluates the state.

**History:** v0.3.56 / #227 introduced grace timer restoration to prevent `_manual_override_active`
staying `True` indefinitely. Issue #282 superseded this with a clean-slate approach: override
state is simply discarded on restart, and the `_first_run` window provides the equivalent
protection period. Issue #1006 (2026-09-29) reintroduces a narrow, targeted piece of grace
persistence — NOT a reversal of #282 — following the same precedent already set for other fields
by Issue #835/#843: confirmed live that a restart landing minutes into an active 3-hour `fan_off`
grace silently discarded it, and 5 minutes later nat-vent reactivated the fan with zero memory of
the user's action. See the new subsection below for the full mechanism and its deliberately narrow
scope.

### Fan-Off Grace Restart Persistence (Issue #1006)

**Scope: `fan_off`-triggered manual grace only.** After `restore_state()`'s clean-slate reset
completes (above), `_maybe_restore_fan_off_grace(state)` re-arms the grace ONLY when the persisted
`last_resume_source == "manual"` AND `last_grace_trigger == "fan_off"` AND the persisted
`grace_end_time` is still in the future relative to restore time. If the grace already elapsed
while HA was down, it falls through to normal clean-slate — nothing to restore. When it re-arms,
it computes the *remaining* duration and calls `_start_grace_period("manual", trigger="fan_off",
duration_override=remaining_seconds)` — the same `duration_override` mechanism Issue #677 built
for the RF-remote-timer restart re-arm, sourced from persisted JSON instead of a live RF-remote
re-announcement.

**Issue #1205:** the hold's own deadline is persisted too (`fan_off_hold_until`) and restored even when a thermostat override's grace was the persisted one -- see the hold paragraph in [§ Fan-Off Grace (Issue #359)](#fan-off-grace-issue-359).

**Why `fan_off` only:** it is the one grace trigger confirmed to never set
`_manual_override_active`/`_fan_override_active` (`on_fan_turned_off()` →
`_clear_fan_flags_and_start_grace()`), so restoring just its grace timer — with no companion
override-flag restoration — cannot leave `_grace_active=True` with `_grace_protects_override=True`
and no override flag behind it, which `coordinator._check_orphaned_grace()` (the orphaned-grace
watchdog, no startup-coalescing gate, runs once per coordinator update) would force-cancel on
the next update.

**Deliberately NOT covered by this exception:** `dashboard_resume` (dispatches its own FSM event
kind directly at its real call site, bypassing the `_start_grace_period()` wrapper this fix reuses)
and `override_confirmed` (needs a live thermostat-mode check) — both recorded in Issue #1046, closed
as not planned. `fan_manual_override` is covered by Issue #1045 (next subsection). Automation-sourced
graces (`sensor_closed_resume`, `nat_vent_exit_resume`, 300 s) stay clean-slate **by decision**:
they are shorter than the 5-minute startup-coalescing window, so nothing is lost by dropping them.

### Fan Manual-Override Restart Survival (Issue #1045)

**Occupant outcome:** a fan the occupant turned on (physical switch, remote press with no timer, or the
HA fan entity) keeps running through an HA restart for the rest of its manual grace, instead of being
shut off by the startup reconcile a few minutes later. Confirmed live 2026-10-09: 17:32 turn-on ->
17:36 restart -> 17:41 `Fan reconcile ... decision=turn-off`; and 18:05 turn-on -> 18:20 restart ->
fan adopted as a CA nat-vent session at a 0.3°F margin and exited 16 minutes later.

**Mechanism.** `fan_manual_override` is override-protecting, so its grace cannot be restored alone —
`coordinator._check_orphaned_grace()` would force-cancel it (no override flag behind it). Instead:

1. `restore_state()` still clears every live override/grace flag. It additionally calls
   `_stash_fan_manual_override_rearm(state)`, which stores only the persisted `grace_end_time`
   in `_pending_override_rearm_end` when `last_resume_source == "manual"`,
   `last_grace_trigger == "fan_manual_override"` and the end time is in the future
   (`_persisted_manual_grace()` — the single helper shared with the Issue #1006 `fan_off` path).
2. The first `_reconcile_fan_on_startup_locked()` call consumes the stash (always cleared, whatever the
   outcome). Provenance precedence: a live RF-remote timer token (Issue #677) wins; otherwise the stash.
3. If the fan is physically running it re-arms through the **same** `handle_fan_manual_override(
   duration_override=remaining, restored_from=...)` the RF path uses, so override flag, grace timer,
   FSM state and WHF HVAC suppression are set together. Guards are shared with the RF path: fan must be
   physically running; the Issue #882 sealed-house guard (all monitored sensors closed -> deactivate
   instead of re-arm). Stash is dropped, with an INFO log, when the grace expired while HA was down
   (`expired_while_down`) or the fan was off at restart (`fan_off_while_down`).
   **Known ordering caveat:** `_do_startup_coalesce()` runs the door/window handler (which may start a
   free-cooling session on the already-running fan) *before* the fan reconcile. When that happens both the
   free-cooling session and the restored override reference the fan; the override wins — a CA stop request
   returns `OVERRIDDEN` and leaves the fan running — and the free-cooling session simply continues under
   normal management once the override ends. The same ordering already applied to the RF-timer re-arm.
4. `AutomationEngine._reconcile_rearmed_override` records that a re-arm happened; the coordinator's
   shadow override/grace FSM feed (#707) reads it instead of re-deriving the condition.

**Observability.** `Fan override restore pending: ...` (restore), `Fan override: set ... (restored after
restart: source=persisted|rf_timer, remaining=Ns)`, `Fan override restore dropped: reason=...`, and the
Fan reconcile turn-off line now carries `override_on_record=`. The `fan_manual_override` Activity event
gains `restored_from` and the Activity renderer appends "restored after restart". Status keeps its
existing grace text (end time from `grace_end_time`; duration is the remaining time).

**Issue #627 correction — the settling window did NOT cover every call site that reads the cleared override flags.** The claim above ("the `_first_run` window provides the equivalent protection period") was true for `coordinator.py`'s thermostat/fan override-detection listeners, but not for the periodic `backstop_30min` untracked-fan reconcile (`coordinator.py`, see `docs/08-COMPUTATION-REFERENCE.md` §9e) — that call site checked only `_fan_override_active` (cleared by the clean-slate above) and had no `_startup_coalesce_active` gate of its own. On a live install this let it fire within the first second after restart, misread a fan still legitimately running under a pre-restart RF-remote timer as unwarranted, turn it off, and release the WHF/HVAC mutex (`_pre_fan_hvac_mode`) — letting the AC be commanded on while the whole-house fan may still have been physically running. Fixed by gating that call site behind `not self._startup_coalesce_active` too, so it now waits for the same 300s window described here before making any decision. Grace/override state itself is still always discarded on restart exactly as described above; only the *timing* of one downstream consumer of that cleared state changed.

### HA Restart During PAUSED State

**Corrected — Issue #523.** Pause state is NOT persisted or restored — `restore_state()` leaves `_paused_by_door`/`_pre_pause_mode` at their clean `__init__` defaults on every restart, same as grace/override state above. If a monitored sensor is still open, `_do_startup_coalesce()` re-derives the pause 5 minutes after boot by delegating to `handle_door_window_open()`, which reads the sensor's live HA state directly (no transition event needed — the sensor was already "on" before CA's listener ever subscribed). Before this fix, `_do_startup_coalesce()` hand-rolled its own incomplete nat-vent pre-check and only called `handle_door_window_open()` when that pre-check favored nat-vent — when it didn't (e.g. outdoor warmer than indoor), the pause was never re-derived at all, and `apply_classification()` could arm a fresh comfort band against the still-open window. See `08-COMPUTATION-REFERENCE.md` for the full incident writeup.

**Regular-cycle race during the 5-minute window (Issue #978).** The 5-minute wait above is not a dead gap — the coordinator's regular (non-coalesce) refresh cycle keeps running during it, and `apply_classification()` had no gate against firing before `_do_startup_coalesce()`'s timer. Confirmed live: a restart cleared `_paused_by_door`; a regular cycle ran `apply_classification()` ~90s later while a monitored window was genuinely still open during a scheduled window-open period (so `_sync_paused_by_door_with_live_sensors()`'s planned-window exemption declined to re-latch the flag); `_apply_comfort_band()`'s choke-point guard (Issue #629) then caught it, correctly but noisily, 3+ minutes before `_do_startup_coalesce()` would have reached the same outcome quietly. Fixed by gating the regular-cycle `apply_classification()` call behind `_suppress_during_startup_coalescing()` — the same helper already used for two sibling regular-cycle checks (`_should_run_regular_cycle_nat_vent_check`, `_should_run_regular_cycle_window_cooling_check`, Issue #321/#670).

### Debounce-Timer Registration Race (Issue #623)

Bug: `_sensor_debounce_pending()` checked only event-listener registration, not HA-authoritative `last_changed` timestamp, allowing a concurrent coordinator refresh to read "open, not yet registered" and misfire a pause before debounce started. Fixed by also checking each sensor's HA `state.last_changed` against `CONF_SENSOR_DEBOUNCE` window.

### Comfort-Band Arm Had No Independent Window Check (Issue #629)

Bug: `_apply_comfort_band()` trusted a pre-computed `_paused_by_door` flag from earlier in the call stack without re-checking live window state, allowing a coordinator refresh (after nat-vent release) to commit an active mode through a still-open window. Fixed by adding a choke-point guard in `_apply_comfort_band()` that refuses to arm an active mode when a monitored sensor is genuinely open (mirroring the WHF/AC mutex pattern).

### Concurrent Door/Window Events (Multiple Sensors)

Each sensor gets its own debounce timer in `_door_open_timers` (keyed by `entity_id`). A second sensor opening while the first is in debounce starts a second independent timer. Both timers can fire and call `handle_door_window_open()` in sequence. The second call is a no-op because `_paused_by_door` is already `True` (early return at L1050–L1051).

On close: `handle_all_doors_windows_closed()` is only called when ALL monitored sensors are closed (L1871: `all_closed = all(not self._is_sensor_open(s) for s in self._resolved_sensors)`). Partial closes do not trigger resume.

### Pre-Pause Mode Is None When Sensor Opens

If `hass.states.get(self.climate_entity)` returns `None` (thermostat entity unavailable), `_pre_pause_mode` stays `None`. The guard at L1168 (`if self._pre_pause_mode and self._pre_pause_mode != "off"`) evaluates to `False`. `_paused_by_door` is NOT set and HVAC is not touched. No notification is sent. The sensor opening is effectively silently ignored in this case.


### A Door/Window Pause Is Always Recorded When HVAC Is Turned Off (Issue #1132)

**What the occupant experienced:** a window opened while a manual thermostat change was in effect (or while a forecast-peak / thermal-floor guard had just ruled out natural ventilation). Climate Advisor turned the HVAC off but never recorded the pause, so closing the window restored nothing and the house stayed without heating/cooling until the occupant touched the thermostat. Usually the next periodic refresh (`apply_classification()` -> `_sync_paused_by_door_with_live_sensors()`) repairs this within seconds; it does **not** while a manual override or pending confirmation is active (the #1119 early return), and never when Manual Change Pause is 0.

Three rules now hold:

1. **Pause recorded on every PAUSE decision.** `handle_door_window_open()` decides PAUSE on the legacy side (gate rejected, a Phase 2 guard skipped nat-vent, or a grace/override exemption fell through) *before* it dispatches the FSM event. It now dispatches `_resolve_door_window_pause_flags(kind=SENSOR_OPENED)`, exactly like `_pause_for_door_window()` (at the time via a `nat_vent_gate_ruled_out=True` flag; since Issue #1142 SENSOR_OPENED itself means "the caller chose to pause"). Without it the FSM re-derived its own simplified nat-vent gate (blind to the forecast-peak guard, the thermal-floor guard and a manual override) and answered NAT_VENT_ELIGIBLE (NORMAL origin, `door_window_fsm.py` `_transition_from_normal`) or GRACE_SUPPRESSED (GRACE origin, `_transition_from_grace`), recording no pause.
2. **Reactivation clears the pause only on fan success.** The paused-by-door nat-vent reactivation sites (`check_natural_vent_conditions()`, both the full-gate and soft-start branches, and `_re_pause_for_open_sensor()`) clear `_paused_by_door`, emit `nat_vent_reactivated_while_paused` and null `_paused_entity` only when `_nat_vent_activation_applied(result)` is true. That is the predicate `_apply_nat_vent_fsm_state_after_activation()` uses to decide whether a session is recorded (`OVERRIDDEN`, `RATE_LIMITED_NEW`, `RATE_LIMITED_DUP` are "not applied"), so "pause cleared" implies "nat-vent session recorded". Otherwise the pause is kept (`_keep_pause_after_refused_reactivation()` re-records `_paused_by_door`, which `_apply_nat_vent_fsm_state(INACTIVE)` would otherwise have cleared as its second writer, and logs an INFO line). `_re_pause_for_open_sensor()` then falls through to its normal re-pause. Mirror of #931 (exit side) / #935 (entry side).
3. **`_pre_pause_mode` lifecycle.** It is the restore target of a *recorded* pause and must not outlive one. Written only by `_pause_for_door_window_action()` (HVAC was active -> that mode; HVAC already off and no pause yet recorded -> `None`) and `_exit_nat_vent()`'s sensor-still-open branch. Cleared by: `handle_all_doors_windows_closed()` (after the restore, and also on its `not _paused_by_door` early return), `handle_manual_override_during_pause()`, `resume_from_pause()`, and the shared dispatcher `_resolve_door_window_pause_flags()` whenever a dispatch leaves no pause recorded (any kind except `ALL_SENSORS_CLOSED`, which consumes the value after the call) — this covers a pause that was never recorded and a pause that ended without a restore (nat-vent reactivation, grace hand-off). A later window opened while HVAC is already off therefore never restores a mode the occupant had off.

Tests: `tests/test_door_pause_recorded_1132.py`; real Home Assistant: `tests_ha/test_door_pause_recorded_1132.py`.

---

## Override Confirmation Delay

### Purpose

The override confirmation delay is a debounce window between detecting a thermostat HVAC mode change and formally accepting it as a manual override. Its purpose is to discard transient glitches — thermostat restarts, HA restart HVAC echoes, fan cycling — that look identical to intentional user overrides but resolve on their own within a few minutes. Without this window, each transient would trigger a 30-minute manual grace period and prevent the system from responding to weather changes.

### Configuration

| Constant | Config key | Default | Label |
|---|---|---|---|
| `CONF_OVERRIDE_CONFIRM_PERIOD` | `"override_confirm_seconds"` | `600` s (10 min) | "Override Confirmation Delay (minutes)" |

Set `override_confirm_seconds = 0` to confirm overrides immediately — the `_confirm_override()` path is taken synchronously with no pending window.

### Trigger

Detected in `coordinator.py` `_async_thermostat_changed()`. Conditions for triggering:
1. The HVAC mode changed to something other than `"off"`
2. The new mode differs from `classification.hvac_mode` (what the system expects)
3. The change is NOT flagged as automation-initiated (i.e., not a CA service call)

When all three hold, the coordinator dispatches to `automation_engine.handle_manual_override(mode)`, which calls `start_override_confirmation()`.

### State

Three instance variables track the pending confirmation window:

| Variable | Type | Meaning |
|---|---|---|
| `_override_confirm_pending` | `bool` | `True` while a detection is awaiting confirmation |
| `_override_confirm_mode` | `str \| None` | The detected HVAC mode string (e.g., `"cool"`) |
| `_override_confirm_time` | `datetime \| None` | UTC timestamp of when detection occurred |

A cancel callable (`_override_confirm_cancel`) holds the timer handle, like the grace cancel handles.

### Gate on `apply_classification()`

While `_override_confirm_pending` is `True`, `apply_classification()` returns early:

```python
if _override_confirm_pending:
    # "Override confirmation pending (detected=X at T) — skipping HVAC mode change"
    return
```

This blocks ALL downstream classification effects: HVAC mode changes, temperature setpoint commands, occupancy guards, fan logic, window open/close recommendations. The system is effectively paused at the classification boundary while the window runs.

The same wait applies to `apply_tou_precondition()` / `apply_tou_away_vacation_precondition()` (Issue #786 Fix 2) and, since the Issue #1119 follow-up, to `handle_morning_wakeup()`, `handle_bedtime()` and `handle_pre_cool()` (via `_scheduled_routine_waits_for_pending_override()`). It is deliberately not part of `decide_scheduled_band_gate()`: the gate's other callers already return on a pending confirmation before reaching it, and `classification_fsm` and the simulation harness consume its five-input shape. See [§ Manual Grace](#manual-grace).

### State Machine

```
[DETECTION] coordinator._async_thermostat_changed()
  Mode ≠ classification.hvac_mode AND not automation-initiated
  └─ handle_manual_override(mode)
       ↓
[PENDING] start_override_confirmation()
  _override_confirm_pending = True
  Event: "override_detected" (dupe-gated 5-min window)
  Timer: CONF_OVERRIDE_CONFIRM_PERIOD (default 600 s)
       ↓ timer fires → _confirm_override_expired()
       ├─ [PATH A: state still divergent from BOTH the mode CA commanded and the classification label, OR an "off" (always a manual change, Issue #1159, even when CA's last command was Off)]
       │   └─ _confirm_override(current_mode)
       │       _manual_override_active = True
       │       Event: "override_confirmed"
       │       _start_grace_period("manual")      ← see Grace Period Types § Manual Grace
       │       apply_classification() remains blocked for CONF_MANUAL_GRACE_PERIOD (30 min)
       │
       └─ [PATH B: state returned to the running (not "off") mode CA last commanded (Issue #1209), or to the classification label when that label is not "off"]
           _override_confirm_pending = False
           Event: "override_self_resolved"
           Notification sent (if notify service configured): "Brief thermostat adjustment
             detected — treated as transient. Climate Advisor continues normal operation."
           apply_classification() unblocked immediately
           No grace period started
```

**An occupant's Off on a mild day (Issue #1159).** On a mild or warm day the classification label is `hvac_mode == "off"`, but since #249 Climate Advisor does not write Off on those days: it arms the comfort band (heat or cool, `select_comfort_band()`). The raw comparison "Off == label `off`" therefore used to take PATH B (no override, no grace, no notice), and the next 30-minute refresh wrote heat/cool again, undoing the occupant's Off. Now an Off is a real manual change on every day type: PATH A, `override_confirmed`, the usual manual grace and notice, then CA resumes. There is no exception: an occupant Off is always a manual change, including while CA's own door/window pause (`_paused_by_door`) is in force (override detection compares against `_last_commanded_hvac_mode`, so an Off while CA has already written Off is never detected as an override at all). The confirm split (`decide_override_confirm_path`) always confirms an `off`, so the confirm split, adoption and the FSM cannot disagree. Heat/cool overrides and heat_cool/auto are unchanged.

**A glitch that returns to the mode CA set is not an override (Issue #1209).** On a warm or mild day the label is `hvac_mode == "off"` while CA runs cool (or heat) on the comfort band. Detection already judged a change against the commanded mode first (`ca_expected_mode(_last_commanded_hvac_mode, label)`, the one definition in `override_confirm_split.py`, used by both coordinator detection sites); the confirmation expiry compared only with the label, so a thermostat that blipped to heat and was back on cool before the window ended was "cool != off" and became a confirmed override with a full manual grace. Now `decide_override_confirm_path(commanded_mode=...)` also takes PATH B when the live mode equals the commanded mode. The commanded mode is read live at expiry (`AutomationEngine._ca_commanded_mode()`, by both the engine's expiry closure and the FSM input builder, `OverrideGraceFsmInputs.commanded_mode`), never snapshotted at detection, so a CA command issued inside the window is honoured. The rule, in order, for an origin that is not `setpoint`/`pause` (those always confirm): unavailable/unknown -> PATH B; live == commanded and live is not `off` -> PATH B; live != label -> PATH A; live == label == `off` -> PATH A (#1159); otherwise PATH B. An Off is never matched against the commanded mode: CA's last command is often Off (nat-vent exit Off-hold, door/window pause), and the occupant reaches Off again from their own heat/cool inside that pause (the coordinator then detects against their override mode), so that Off is confirmed and held like any other (`test_off_after_own_cool_in_a_ca_pause_is_kept_1159`). Consequences: an occupant Off while CA commanded heat/cool is still confirmed (#1159); a press of the mode CA already commanded is no change at all (detection never fires, and at expiry it self-resolves); a hot-day return to the label with a different commanded mode keeps self-resolving. Pinned by `tests/test_override_confirm_split.py`, `tests/test_glitch_returns_to_commanded_mode_1209.py`, `tests_ha/test_glitch_returns_to_commanded_mode_1209.py`.

**Adoption of an "off" override.** `_override_matches_current_decision()` (and the pure twin `decide_override_matches_decision`) never adopts an "off" override as matching: an occupant Off is always a manual change, kept for the whole grace (pre-expiry revisit and natural expiry). Otherwise the confirmed Off would be adopted and cleared at the next refresh and CA would write cool/heat again.

**Total blocking time if confirmed:** up to `CONF_OVERRIDE_CONFIRM_PERIOD + CONF_MANUAL_GRACE_PERIOD` = 10 + 30 = **40 minutes** (both configurable).

### Events Emitted

| Event | When | Key Payload Fields |
|---|---|---|
| `override_detected` | Detection occurs (dupe-gated 5-min) | `detected_mode`, `source` |
| `override_confirmed` | PATH A: timer expires, state still divergent | `mode`, `confirm_delay_seconds` |
| `override_self_resolved` | PATH B: timer expires, state resolved | `detected_mode`, `current_mode` |

### Interaction with `clear_manual_override()`

`clear_manual_override()` handles both the pending and active override states:

```python
if _override_confirm_pending:
    _override_confirm_cancel()  # cancels the pending timer
    _override_confirm_pending = False
    _override_confirm_time = None
    _override_confirm_mode = None
```

This means an occupancy transition, a fan override, or a dashboard cancel that calls `clear_manual_override()` will also cancel an in-progress confirmation window. The override is discarded without ever reaching PATH A or PATH B.

### Persistence

`_override_confirm_pending` and its companion variables are **not persisted** across HA restarts. `restore_state()` does not restore them. On restart, any in-flight confirmation window is silently abandoned and the flags reset to `False`. This is intentional — an HA restart is itself a transient event, and the mode state that triggered the confirmation may no longer reflect user intent after restart.

### Second Override During Active Grace (Issue #201)

If `_async_thermostat_changed()` detects that the thermostat mode has changed to a **different mode** while `_manual_override_active = True` (i.e., an active grace period is already running), a new detection branch fires:

1. The coordinator's "Fix D" (#282) calls `_replace_thermostat_override(..., OVERRIDE_SUPERSEDED, clear_fan=False)`, which clears the first override but deliberately keeps its grace timer running (log: "New mode change during active override grace").
2. `handle_manual_override(new_mode)` is called to start a fresh `CONF_OVERRIDE_CONFIRM_PERIOD` confirmation window for the new mode.
3. If the new mode is still divergent at window expiry (PATH A), a new full-duration grace period starts for the new mode.

**Invariant:** only one confirmation window is active at a time. Starting a new window (`start_override_confirmation()`) immediately after the first override is cleared ensures no confirmation timer is orphaned. The net effect is that the most recent user action always wins — the first override is gone and the system monitors the new mode with a fresh debounce.

**Issue #1216 -- the second change used to be dropped.** Occupant: Cool confirmed, then Off (or Heat) inside Cool's grace, and Climate Advisor put the house back as if the second change never happened. The change is PENDING (`_override_confirm_pending`) for the whole window, and two things used to drop it: (1) `_check_orphaned_grace()` (#508) reaped the superseded grace on the next update and the FSM resolve dropped the pending change -- the watchdog now skips while `_override_confirm_pending`; (2) the first grace expiring before the confirm -- `override_grace_fsm.unprotected_grace_expiry_keeps_pending(confirm_pending, manual_override_active)` now keeps a pending change whenever no confirmed override is behind it. With a window open, the superseded grace ending does not re-pause over the pending change (owner decision, 2026-11-01): the change is confirmed, gets its own grace, and Rule 3 re-pauses at that grace's end. If the second change self-resolves (PATH B), the old grace is reaped on the next update.

**This branch is distinct from the normal detection path.** The normal path in `_async_thermostat_changed()` checks `not _manual_override_active` before calling `handle_manual_override()`. This second-override branch explicitly checks `_manual_override_active = True AND new_mode ≠ _manual_override_mode`, which bypasses the normal gate.

---

## What Clears a Manual Override

`clear_manual_override()` is the single function that deactivates `_manual_override_active` and cancels any pending override confirmation window. Every callsite must be traceable in logs.

### `reason` parameter (post-Issue #204 fix)

`clear_manual_override()` now accepts a `reason: str` keyword argument. Every call logs at INFO level:

```
[climate_advisor] clear_manual_override called — reason=<reason>
```

This makes every clear event attributable in `python tools/ha_logs.py --full` without a lengthy investigation. Before this fix, all four callsites were indistinguishable in logs.

### Callsite inventory

| # | Callsite | Location | Condition | Behaviour after Issue #204 fix |
|---|---|---|---|---|
| 1 | `_grace_expired()` — planned window branch | `automation.py:~1500` | Grace timer fires while within a planned window period | Always clears — intended; override was established before a window period |
| 2 | `_grace_expired()` — re-pause branch | `automation.py:~1510` | Grace timer fires; sensor still open; re-pause scheduled | Always clears — the re-pause re-establishes HVAC-off state; prior override no longer meaningful |
| 3 | `_grace_expired()` — normal expiry branch | `automation.py:~1520` | Grace timer fires; all sensors closed | Always clears — intended; grace window has elapsed normally |
| 4 | `handle_bedtime_setback()` | `automation.py:1926` | Configured `sleep_time` fires | **Skips entirely if `_manual_override_active=True` or (Issue #1119 follow-up) `_override_confirm_pending=True`** — emits `bedtime_setback_skipped` event; logs skip at INFO. Clears only if override is not active.  **Fan override is different (Issue #1027, kept deliberately as of 2026-10-07):** bedtime still clears an active FAN override and cancels its grace (#874; since #1222 through the shared `_end_grace_left_protecting_nothing()` helper). Activity Log (Issue #1029): this now emits `fan_override_cleared` (reason `bedtime`) + `grace_cancelled` (reason `bedtime`). |
| 5 | `handle_morning_wakeup()` | `automation.py:2025` | Configured `wake_time` fires | **Skips entirely if `_manual_override_active=True` or (Issue #1119 follow-up) `_override_confirm_pending=True`** — emits `morning_wakeup_skipped` event; logs skip at INFO. Clears only if override is not active.  **Fan override is different (Issue #1027, kept deliberately as of 2026-10-07):** wake-up still clears an active FAN override and cancels its grace (#874; since #1222 through the shared `_end_grace_left_protecting_nothing()` helper). Activity Log (Issue #1029): this now emits `fan_override_cleared` (reason `morning_wakeup`) + `grace_cancelled` (reason `morning_wakeup`). |
| 6 | Dashboard "Cancel Override" / "Cancel Fan Override" buttons | `api.py` `ClimateAdvisorCancelOverrideView` / `ClimateAdvisorCancelFanOverrideView` | User explicitly presses either dashboard button | Always clears — explicit user intent. As of Issue #508 both views call `cancel_override(reason="user_cancel_override"\|"user_cancel_fan_override")`, not `clear_manual_override()` directly — see [§ Deliberate Cancellation](#deliberate-cancellation-cancel_override-issue-508) for why this additionally cancels grace and forces reconciliation, which bare `clear_manual_override()` does not. Activity Log (Issue #1029): emits `grace_cancelled` (reason passthrough); a fan-only cancel keeps its existing supplemental `override_cleared`. |
| 7 | `handle_occupancy_away()` / `handle_occupancy_vacation()` | `automation.py` occupancy handlers | Occupancy transitions to away/vacation while `_manual_override_active=True` | Calls `clear_manual_override(reason="occupancy_away")` / `clear_manual_override(reason="occupancy_vacation")` before applying setback, then ends the cleared override's grace with `_end_grace_left_protecting_nothing("occupancy_away" / "occupancy_vacation", remote_timer_hours=<pre-clear snapshot>)` (Issue #1222; `grace_cancelled`, reason `occupancy_away` / `occupancy_vacation`). Fixed in staging (issue #220). Note: `handle_occupancy_home()` does **NOT** call `clear_manual_override()` — returning home restores comfort via `_set_temperature_for_mode()` without touching the override flag. It still calls the bare primitive, not `cancel_override()`: the dashboard-cancel extras (fan reconcile, forced refresh) do not belong at this internal transition, so only the grace-ending half is done, via the shared helper (see [§ Occupancy Interaction](#occupancy-interaction)). |
| 8 | `adopted_matching_decision` path inside `_on_grace_expired()`'s normal-expiry branch | `automation.py` `_on_grace_expired()` | Grace timer fires naturally and automation's current decision already matches the override | As of Issue #508, calls `cancel_override(reason="adopted_matching_decision")` (previously hand-composed `_cancel_grace_timers()` + `clear_manual_override()` in that order — the two pre-508 callsites had drifted to opposite call orders). Activity Log (Issue #1029): none from this site beyond the existing `override_adopted` row — `_on_grace_expired()` calls `_clear_manual_override_active()` directly (not `cancel_override()`), and `clear_fan_override()` stays silent for `adopted_matching_decision`. The pre-expiry adoption in `apply_classification()` does go through `cancel_override()` and emits `grace_cancelled`. Neither adoption can happen while the open-window hold is live (Issue #1119): `_override_decision_mode_and_target()` returns `(None, None)` then. |

### Observability of early endings (Issue #1029)

Bedtime and wake-up clearing an active fan / RF-timer override (Issue #1027) is a documented, kept behavior. Issue #1029 only makes every intentional early ending visible in the Activity Log:

| Early ending | Site | Event(s) |
|---|---|---|
| Bedtime fan-override clear + grace cancel | `handle_bedtime` | `fan_override_cleared` + `grace_cancelled` (reason `bedtime`) |
| Wake-up fan-override clear + grace cancel | `handle_morning_wakeup` | `fan_override_cleared` + `grace_cancelled` (reason `morning_wakeup`) |
| Dashboard cancel / adopted matching decision | `cancel_override()` | `grace_cancelled` (reason passthrough); fan-only keeps `override_cleared` (no extra `fan_override_cleared`) |
| Grace replaced by a new grace | `_start_grace_period_action` | `grace_cancelled` (reason `superseded`) — only if the replaced grace was manual-sourced or override-protecting with >= 1 min left |
| Natural expiry | `_on_grace_expired` | `grace_expired` (unchanged; `fan_override_cleared` skipped for `grace_expired`) |
| Orphan / stuck-grace watchdogs | coordinator | `stuck_grace_recovered` (unchanged) |
| User turns fan off | `on_fan_turned_off` | `fan_cancel` (unchanged) |

`grace_cancelled` carries `reason`, `trigger`, `source`, `grace_source`, `remaining_minutes`, `protects_override`, `remote_timer_hours`, `grace_end_time`; `fan_override_cleared` carries `reason`, `source`, `remote_timer_hours`, `active_since`. See [activity-report-table.md](activity-report-table.md#event-catalog).

### Setpoint echo recognition by value (Issue #1232)

**Occupant outcome.** A cloud thermostat commonly reports a setpoint Climate Advisor wrote 30-120 s after the write. Read as the person turning the dial, that report confirmed a false manual override and paused automation for the manual grace period (1 h by default). It was rare while CA wrote a few times a day; the remote-sensor outer loop (#1194) writes stepped nudges overnight, so the sleeper would have woken to a house that had stopped following its plan. A person who really changes the setpoint must still be caught.

**Rule (one pure function, `own_write_echo.decide_setpoint_echo`, called through `AutomationEngine.recognise_own_setpoint_echo` by the single setpoint-detection site in `coordinator._async_thermostat_changed`).**

| Situation | Result |
|---|---|
| Change carries the Context id (or parent id) of a remembered CA write, inside 180 s | CA's own echo, whatever the value (a coarse thermostat may round it) |
| Change equals a remembered write after rounding to the thermostat's step, inside 180 s | CA's own echo |
| Different value | Evaluated as a possible occupant change (unchanged) |
| Same value, 180 s or more after the write | Evaluated as a possible occupant change |
| Same value inside 180 s, event Context has a `user_id` (a signed-in person) | Occupant: the value fallback is not used |
| Same value inside 180 s, no distinguishing Context | Treated as the echo. Indistinguishable by value; accepted because the person asked for the setpoint the plan already holds, so there is nothing to override |
| heat_cool: low and high changed in one event | Every changed attribute must be accounted for; one unrecognised edge makes it an occupant change |
| Two or more writes in flight, echoed in order | Each is remembered and recognised |
| Echo of a newer write recognised | Older remembered writes of the same entity and recorded attribute (sent before it) are forgotten too: the thermostat has moved past them, so an occupant who dials back to one of those older values is the occupant (owner decision 2026-11-02, D3). Other attributes keep their writes |
| Older write echoed AFTER a newer one (out of order) | Judged as a possible occupant change. Accepted cost of the row above, the owner's choice; pinned by `test_out_of_order_older_echo_is_judged_as_the_occupant` |

*Memory.* `AutomationEngine._own_setpoint_writes`: per `climate.set_temperature` call and attribute (`temperature` / `target_temp_low` / `target_temp_high`) an `OwnSetpointWrite(entity, attribute, value in the thermostat's unit, time, Context id)`. Recorded at the one send choke point (`_call_service_with_context`, kind `climate`) before the call; the window restarts when the awaited call returns (a slow thermostat holds the call ~25 s); a write that never reached the thermostat (failed or rejected) is forgotten; observe-only writes are never recorded. At most `SETPOINT_ECHO_MEMORY_MAX` (16) writes are kept. A recognised echo consumes the writes it answers (a second identical change later is then evaluated normally); an unrecognised change leaves the other writes in flight alone; entries are dropped when the 180 s window ends. Not persisted (see [state-persistence](state-persistence.md)).

*Rounding.* The thermostat's `target_temp_step` attribute, else 0.5 (Celsius) / 1.0 (Fahrenheit). A value already on the step grid must come back exactly (an occupant's one-step tweak is not the echo); an off-grid value (72 F = 22.22 C on a 0.5 C thermostat) matches either neighbouring grid value, because thermostats round up, down or to nearest.

*Aliasing.* CA writes a single `temperature` (with `hvac_mode`). A thermostat in a band mode may report that write on `target_temp_low` or `target_temp_high`, so a `temperature` write accounts for a band edge. A write recorded on a band edge (a future dual-setpoint writer) accounts only for that edge.

*Backstop and order.* The existing `_temp_command_pending` / `_is_recent_hvac_command(30)` / `_is_recent_temp_command(30)` guards are unchanged and still apply. The echo is also consumed when one of them would have suppressed the event anyway.

*Mode writes (Issue #1246; symmetry with the setpoint memory).* The person at home closes a window after a pause: Climate Advisor restores the pre-pause mode (heat), and because the house drifted past the margin the comfort selector switches straight to the other mode (cool). A cloud thermostat reports the first switch 30-120 s late; Climate Advisor used to remember only the LAST commanded mode (`_last_commanded_hvac_mode`), so the late "heat" looked like the person choosing heat: a false manual override (automation held for the confirm window, or paused for the 1 h manual grace). `_record_commanded_mode` (the single writer of `_last_commanded_hvac_mode`, whose meaning is unchanged for every other reader) now also appends an `OwnModeWrite(mode, written_at)` to `AutomationEngine._own_mode_writes`, pruned by the SAME `own_write_echo.prune_own_writes` (generic over any record with `written_at`), the same 180 s window (`OWN_WRITE_ECHO_WINDOW_S`) and the same 16-entry cap; the window restarts when the awaited call returns (`_restamp_climate_command_times`). The coordinator asks `AutomationEngine.recognise_own_mode_echo(new_mode)` (pure rule `own_write_echo.decide_mode_echo`) on every real mode change, and a mode that is ANY recent write is CA's echo and joins the `_is_expected_confirmation` guard used by the pause branch, the Fix-D branch and the plain mode-override branch. Same lifecycle as setpoints: a write whose service call FAILED or was REJECTED never reached the thermostat, so it is forgotten at once (`_forget_own_mode_write`, mirroring `_settle_own_setpoint_write(sent=False)`; a failed switch inside `_set_temperature` drops both entries that call recorded; a failed or rejected `set_temperature` call after a switch that landed drops only that call's own entry, keeping the switch's) and the person later choosing that mode is detected as theirs; a recognised echo consumes the matching writes, older writes it moved past are superseded (an older echo arriving after the newer one's is then judged as the occupant, the #1232 D3 rule), writes end with the window, nothing is persisted. A mode Climate Advisor did NOT write inside the window (the person switching to Off, or to the other mode after the earlier write's echo was consumed) is still detected as an occupant change. Accepted, with a known cost: a person who picks exactly a mode CA wrote inside the last 180 s whose echo has not arrived is read as the echo. When that mode is CA's LAST write there is nothing to override (the plan already wants it; the plain branch never flagged it anyway). When it is an EARLIER write that never showed up as a state change (the thermostat was already in the newer mode, so it reported nothing, or the earlier write failed), CA still wants the newer mode: the person's change is missed (no override, no grace), the read-back check about 130 s later logs "HVAC mode not applied" against the thermostat, and, with no override or grace protecting it, a later comfort write in CA's mode (for example on the next periodic cycle) can put that mode back over the person's choice. A change made after the 180 s window, or to a mode CA did not write, is detected as before. The same two-write shape arises after a fan session (`_deactivate_fan` blind-restores `_pre_fan_hvac_mode`, then the comfort selector writes the other family) and is covered by the same list for both fan modes. Tests: `tests/test_mode_echo_symmetry_1232.py`, `tests/test_own_write_echo.py`, real-HA `tests_ha/test_mode_echo_delayed_1246.py` (the dev thermostat sim's `set_echo_delay()` also delays `set_hvac_mode`).

Not changed: startup coalescing (changes in its first 5 minutes are not detected, Issue #1235). Tests: `tests/test_own_write_echo.py`, `tests/test_setpoint_echo_by_value_1232.py`, `tests/test_mode_echo_symmetry_1232.py`, real-HA `tests_ha/test_setpoint_echo_delayed_1232.py` (the dev thermostat sim gained `set_echo_delay()`).

*The read-back verify waits for the same window (Issue #1194 e F1).* The 10 s setpoint read-back
(`_check_single_setpoint_accepted` inside `_set_temperature_impl`) used to call a write "rejected" whenever the thermostat
still reported its old value at 10 s: on a cloud thermostat that is simply the echo not having arrived, so the person saw
"Setpoint validation FAILED", a `setpoint_rejected` entry, a 15-minute retry and, after a streak, a 1 F nudge, all for a
write that had worked. Now, when the read-back disagrees, the thermostat still reports the value it had BEFORE the write,
and that write is still outstanding in this memory (`own_write_echo.outstanding_own_write`: same entity and attribute,
same value after step rounding, inside `OWN_WRITE_ECHO_WINDOW_S`; an arrived echo is consumed, so it no longer counts), the
verify is not answered yet and re-checks when the window ends (`AutomationEngine._setpoint_echo_wait_s`). Only then -- no
echo inside the window -- is it a rejection, exactly as before. A thermostat that answered with a different value (clamped
to its own limit, refused) is a rejection at once, as before. A newer write supersedes the pending check as always (its
own check then waits for its own echo). Tests: `tests/test_setpoint_verify_late_echo_1194e.py` (echo at 45 s and 110 s, no
echo by 180 s, a different value, an instant echo; heat and cool, both fan modes).

### Note — Away/vacation setback and override detection (Issue #221)

Away/vacation setback setpoint changes are guarded by `_is_recent_temp_command(30s)` in the coordinator's setpoint-change detection block (fix #221). When CA issues a setback setpoint command via `handle_occupancy_away()` or `handle_occupancy_vacation()`, `_set_temperature()` records a `_temp_command_time` timestamp. Any thermostat echo arriving within 30 s is suppressed and does not trigger a manual override detection. This prevents the automation's own setback from opening a spurious grace period. Since Issue #1232 a setback echo that arrives later than 30 s is recognised by value too (see [Setpoint echo recognition by value](#setpoint-echo-recognition-by-value-issue-1232)); the 30 s guard remains the backstop.

### Invariant

After Issue #204: no scheduled timer (bedtime, wakeup) may call `clear_manual_override()` while `_manual_override_active=True`. Only grace expiry (callsites 1–3) and the explicit service call (callsite 6) may unconditionally clear an active override. All other callsites must check the flag first.

---

## Shared Scheduled-Band Gate (Issue #498)

**Problem:** `apply_classification()` (the real 30-min decision loop) has always had the
correct full gate stack — occupancy dispatch, manual-override adopt-or-skip, paused-by-door
suppression, and `_natural_vent_active`/`_whf_owns_hvac()` deferral. `handle_bedtime()`,
`handle_morning_wakeup()`, and `handle_pre_cool()` each hand-rolled their own partial,
independently-drifted copy of a subset of these checks instead of reusing it — the same
"duplicate parallel gate logic" failure class previously seen in nat-vent threshold bugs
(#400/#402). Two concrete production bugs came from this drift:

1. `handle_morning_wakeup()`'s copy never checked `_fan_override_active` at all (only
   `handle_bedtime()`'s did) — wake-up unconditionally deactivated a manually-overridden
   whole-house fan and armed AC, defeating the `_whf_owns_hvac()` choke-point guard that
   write is supposed to respect. Confirmed in production: 06:30 wake-up killed a manual WHF
   override and armed cool, self-correcting a cycle later only because an unrelated nat-vent
   re-evaluation happened to run right after.
2. None of the three scheduled handlers checked `_paused_by_door` at all — a door/window
   pause active at exactly sleep_time/wake_time/the pre-cool trigger was not protected; only
   `apply_classification()`, `handle_occupancy_away()`, and `handle_occupancy_vacation()` did.

**Fix:** one shared, pure function, `desired_state.decide_scheduled_band_gate()`, is now the
single place these four checks live (occupancy, manual override, paused-by-door, nat-vent/WHF
ownership — in that order, matching `apply_classification()`'s original inline order). All
four call sites — `apply_classification()`, `handle_bedtime()`, `handle_morning_wakeup()`, and
`handle_pre_cool()` — call this one function for their "may I proceed" decision, then map each
outcome (`PROCEED`, `DEFER_OCCUPANCY`, `DEFER_OVERRIDE`, `DEFER_PAUSED`, `DEFER_NAT_VENT`) to
their own existing telemetry/DailyRecord/skip-event behavior — no handler lost its distinct
event names or bookkeeping; only the boolean gate-checking is unified.

**Also fixed as part of the same change (bedtime's nat-vent-continuation logic):**
`handle_bedtime()` previously had its own inline check — `outdoor < sleep_band.ceiling` — to
decide whether an active nat-vent/WHF session should keep running through bedtime instead of
handing off to AC. This was a real, separate correctness bug, not just duplication: it could
hand off to AC prematurely even while outdoor was still well below indoor and the fan was
doing useful, cheaper cooling. Bedtime now just defers entirely whenever
`decide_scheduled_band_gate()` returns `DEFER_NAT_VENT` — no outdoor comparison of its own.
The engine's own per-tick `check_natural_vent_conditions()` (outdoor-reversal exit) and
`nat_vent_temperature_check()`'s sleep-window cycling target (`sleep_heat + hysteresis`, active
the instant `_in_sleep_window()` flips true) already manage the session's lifetime correctly
without any help from the scheduled handler.

**Capture-before-clear hazard (found while testing the wake-up fix):** `clear_manual_override()`
unconditionally calls `clear_fan_override()`, which resets `_fan_override_active = False` as a
side effect — regardless of whether a fan override was active moments before. Both
`handle_bedtime()` and `handle_morning_wakeup()` call `clear_manual_override()` *before* their
fan-deactivation check, so reading `self._fan_override_active` at that point always sees it
already cleared. This means the guard's *live-attribute* form is silently defeated even when
written correctly — confirmed by a test (`test_bedtime_clears_fan_override_then_deactivates`,
now corrected) whose own name documented the bug as intended behavior. Both handlers now
snapshot `_fan_was_overridden = self._fan_override_active` *before* calling
`clear_manual_override()`, and use that snapshot for the deactivation decision — the same
capture-before-clear pattern already used in `_confirm_override()` (automation.py:~3648) for
`_manual_override_mode`/`_manual_override_source`.

**A second, independent side effect of the same call (Issue #530):** the capture-before-clear fix
above only protects the *fan-deactivation* decision inside `handle_morning_wakeup()` itself.
`clear_manual_override()` → `clear_fan_override()` also unconditionally calls
`_release_whf_and_reclassify()`, which releases WHF HVAC suppression (`_pre_fan_hvac_mode = None`)
whenever the physical fan reads off — with **no awareness of `_natural_vent_active` at all**.
Confirmed live: `handle_morning_wakeup()` correctly computed `DEFER_NAT_VENT` ("leaving fan alone")
from the gate at the top of the handler, then two lines later `clear_manual_override()`'s side
effect released suppression anyway (physical fan was off; `_natural_vent_active` was still `True`
but never checked), so the `_apply_comfort_band()` write a few lines further down armed active
HVAC mode with windows still open. Fixed by adding a `_natural_vent_active` guard to
`_release_whf_and_reclassify()` itself (automation.py) — it now no-ops whenever a nat-vent session
is still considered active, deferring to `_exit_nat_vent()` as the only path that ends that
session's suppression, matching the pattern already established for the physical-fan-state guard
immediately above it.

---

## Thermostat Availability Recovery (Issue #1049)

**Occupant outcome:** after an HA or cloud-thermostat outage (e.g. Sensi), the thermostat reports `unavailable` and then reconnects, often as `off` or with stale/absent attributes. Previously that looked like the occupant turning the system off, so Climate Advisor started a manual-override grace period and stopped managing the house.

**Rule:** a state change whose `old_state` was in `UNAVAILABLE_STATES` (`{"unavailable", "unknown"}`, `const.py`) is an availability recovery (`is_availability_recovery(old_state)`; `old_state=None` is not). `coordinator._async_thermostat_changed()` computes this once and `and not _is_availability_recovery` gates:

1. the door/window-pause HVAC-mode override branch,
2. the "different mode while grace is active" re-override branch,
3. the plain HVAC-mode override branch,
4. the setpoint-only override branch (a reconnecting thermostat has no prior setpoint: `old_temp is None`).

No early return: the other bookkeeping in the handler still runs. The same helper is used by the contact-sensor reconnect-blip check. The `unavailable` -> `on` fan-entity case is covered separately earlier in this spec (the `fan_entity` unavailable-transition fix). Tests: `tests/test_availability_recovery_override.py`.
