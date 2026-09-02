# Changelog

All notable changes to Climate Advisor are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions.

## [0.7.6] — 2026-09-02

- Fix #817: the Status report's window/AC timing (Today's Strategy table, Next Automation card, and Next User Action card) was computed independently in three separate places and could disagree with itself — e.g. Next User Action still showing an old close time after the forecast changed. All three now read the exact same computed value, so they can no longer contradict each other, and the door/window pause behavior around window-close time now uses that same corrected time too.

## [0.7.5] — 2026-09-01

- Fix #818: the Status report no longer shows a nonsensical zero-width window recommendation like "Open 6:00 AM - 6:00 AM" — the close time now always reflects a moment after windows would actually open.

## [0.7.4] — 2026-09-01

- Fix #814: the Status report's Conditions card and Today's Strategy text now stay in sync with the live automation state, instead of sometimes showing a stale trend or forecast high left over from an earlier update cycle.
- Fix #815: Home Assistant's event loop no longer stalls briefly every time the AI subsystem initializes (on zone startup, or when testing/saving your Claude API key) — that work now happens off the main thread.
- Fix #816: the Debug tab now shows a plain-English description (e.g. "Thermostat's built-in sensor") for where indoor/outdoor temperature is read from, instead of the raw internal setting name.

## [0.7.3] — 2026-09-01

- Fix #813: the dashboard no longer guesses which zone to show on a brand-new browser or device's first visit to a multi-zone install, and the "Ambiguous zone selection" Repairs card no longer shows permanently on every multi-zone install — it only appears if an actual ambiguous zone resolution ever occurs.

## [0.7.2] — 2026-09-01

- Fix #812: the dashboard, weather/reload repair prompts, and AI Investigator reports now always act on the zone you're actually looking at instead of sometimes silently guessing which zone — and the dashboard no longer gets stuck if a saved zone selection becomes stale.

## [0.7.1] — 2026-09-01

- Fix #808: Adding a second zone that points at a thermostat you've already configured now shows a clear "already configured" message instead of silently creating a conflicting duplicate zone.

## [0.7.0] — 2026-09-01

- Feat #796: Climate Advisor now supports running two or more zones (thermostats) at once — each zone gets its own learning history, dashboard data, and settings, with a zone switcher on the dashboard when more than one zone is configured.

## [0.6.93] — 2026-09-01

- Fix #805: if your thermostat, weather source, a sensor, your whole-house fan, or another entity Climate Advisor depends on gets removed or stops responding, you'll now get a notification telling you which one and what to check — instead of Climate Advisor quietly running on stale data for hours with no warning.

## [0.6.92] — 2026-08-31

- Fix #802: the forecast chart's Target line and Vent/Windows activity bars no longer flicker or show near-total gaps into the future — they now forward-walk the same real nat-vent and ceiling-guard decision logic the live automation engine uses, including correctly predicting when an off-classified day would escalate to active cooling mid-day.

## [0.6.91] — 2026-08-31

- Fix #800: the forecast chart's Target line no longer draws a misleading flat line across stretches where nothing is actually setting a target (e.g. HVAC off, nat-vent not running) — it now shows a real gap instead.

## [0.6.90] — 2026-08-31

- Fix #790: the whole-house fan or HVAC fan-only mode could switch back on within seconds to minutes of being shut off for a legitimate reason (outdoor conditions changed while a window was open), because reconcile_fan_on_startup() — called by 2 event-driven triggers that can fire sub-minute — never checked or armed the 5-minute reactivation lockout every other fan-off path already respects.

## [0.6.89] — 2026-08-31

- Feat #797: TOU pre-conditioning's fallback lead time (used before your thermal model has learned your home's response rate) is now 45 minutes by default instead of 120, and configurable in Settings → Advanced.

## [0.6.88] — 2026-08-31

- Feat #794: the TOU Scheduler setup screen now shows one consistent "TOU Scheduler" title throughout, a compact collapsed day picker instead of an always-open checkbox list, and no longer wipes out what you typed in other fields when one field fails validation. The unused cost-tag choice is hidden. A savings schedule can now tell you when it's active even on a day it correctly decides not to act (e.g. windows already open, HVAC off) — the Status card and Activity Record now say so instead of showing nothing. The chart's overlay is now a single, bold "Target" line showing the real system target at any point in time — whatever the source (normal comfort-band operation, whole-house-fan/nat-vent cycling, or TOU pre-conditioning) — for both past and predicted-future hours, replacing two thinner, less accurate lines.
- Fix #514: the shaded Target Band on the chart now shows what your actual comfort target was at any past time, not today's live schedule re-applied backward over history.

## [0.6.87] — 2026-08-31

- Feat #786: Climate Advisor can now pre-cool or pre-heat automatically ahead of a scheduled high electricity-rate window, banking toward the comfort band's own floor or ceiling using the home's learned thermal response rate — no new temperature settings to configure. Up to 5 schedules (day-of-week and time-of-day, midnight-spanning windows handled correctly) are configurable from Settings → Options → Scheduler. Pre-conditioning never overrides an active or not-yet-confirmed manual thermostat change, and never runs the compressor while a monitored door/window is open or the whole-house fan is running — it defers automatically and resumes on the next cycle.

## [0.6.86] — 2026-08-31

- Fix #787: a brief network dropout on the whole-house fan's Wi-Fi module (confirmed via Home Assistant logs to be an ESPHome encryption-handshake error, not a real physical change) was being misread as someone manually turning the fan off and back on, starting a needless 3-hour "hands off" grace period each time. This produced a confusing overnight cascade of fan cycling and HVAC pausing/resuming. Climate Advisor now cross-checks the fan's own dedicated power-detection sensor (when configured) before treating an "unavailable" state as human action — a brief connectivity blip is now correctly ignored instead of triggering a false override.

## [0.6.85] — 2026-08-31

- Fix #788: warm-day briefings could tell you to reopen windows "when the evening air cools back down" at 8:00 AM, right after telling you to close them "to hold the heat in" at 7:00 AM — a regression from #535, which made the close sentence aware of why the cutoff happened (outdoor air rising vs. hitting your comfort floor) but never updated the reopen sentence to match. It now uses the same reason-aware wording. Also fixed: the grace-period heads-up after a manual HVAC override said "this morning" even when it could be regenerated and shown on the dashboard or API at any hour of the day.

## [0.6.84] — 2026-08-30

- No user-visible behavior change. Fix #585: automation.py logged routine, correctly-executed operations (setting the HVAC mode, writing a setpoint, accepting a manual thermostat override, activating/deactivating a fan) at WARNING — a holdover from Issue #37, which promoted them purely so they'd survive Home Assistant's default log level, not because they represented problems. These 19 log lines now log at INFO; WARNING is reserved for actual anomalies, guard-clamped/overridden writes, and safety-guard firings. Diagnostic visibility is unaffected: the dashboard's Activity Record tab and the AI Investigator's Activity Timeline read the automation event log directly, independent of Python log level.

## [0.6.83] — 2026-08-30

- Fix #586: the AI Investigator's thermal report could show two observation counts that look like they should match and don't (e.g. an all-time count of 24 next to a "0 committed" figure for the same category), with no explanation. The report now labels these explicitly as different scopes — an all-time cumulative counter vs. a 90-day-windowed, capped count — so a large gap reads as expected, not as lost data. A rejection count sitting exactly at its cap now displays as "100+ (capped)" instead of an exact "100".

## [0.6.82] — 2026-08-30

- Fix #583: The Manual Overrides panel no longer shows a contradictory count (e.g. "1 override" alongside "no overrides recorded"). The setpoint-override counter and its detail list are now updated together, so they can never disagree.

## [0.6.81] — 2026-08-30

- Fix #535: the briefing and dashboard's predicted window-close time for warm/mild days now also accounts for indoor reaching your comfort floor, not just outdoor air warming up — closing a latent gap where the predicted close time could show later than nat-vent would actually still be helping.

## [0.6.80] — 2026-08-29

- No user-visible behavior change. Documentation accuracy and readability pass covering `docs/`, `README.md`, and `CHANGELOG.md`: backfilled two missing changelog sections (0.5.48/0.5.49), corrected stale references to the shadow-engine/dual-engine migration infrastructure deleted in Phase 6 (issues #757–#770) across the Architecture Reference and all FSM lifecycle specs, fixed several numeric drifts (source file count, REST endpoint count, AI context-provider count) and two issue-number misattributions, converted the README's ASCII architecture diagram to a native Mermaid flowchart, and compacted dense prose throughout the FSM specs and computation reference for readability.

## [0.6.79] — 2026-08-29

- Fix #775: the whole-house fan could restart just 1°F above your comfort floor after already shutting off once — below the tighter comfort band it would have held if it had just kept running. Restarting now requires indoor to recover to the same point a continuously-running fan would already be cycling at, not the looser floor used for a brand-new activation. The fan's very first activation each day is unaffected.

## [0.6.78] — 2026-08-29

- Fix #774: turning off the whole-house fan while a manual override/grace period was protecting it could leave that override fully active for minutes afterward — suppressing natural ventilation and the AC/fan mutex exactly as if the fan were still running. Turning the fan off now ends the override immediately, matching what actually happened at the fan. Also fixed: the Activity Report could show a false "Fan stopped" line for a fan that never physically stopped, just got reclassified from untracked to override-tracked at restart.

## [0.6.77] — 2026-08-29

- Feat #702: no user-visible change. Shrinks const.py (was 710KB, mostly historical changelog data imported on every Home Assistant startup) by moving fix history to its own file, read only when actually needed. Reduces memory use on all installs, especially small hardware like a Raspberry Pi.

## [0.6.76] — 2026-08-29

- Fix #739: the whole-house fan could reactivate seconds after shutting off because indoor dropped below your comfort floor — pulling in cold outside air again and pushing the home right back under the floor it had just recovered from. The comfort-floor stop now waits out the same 5-minute cooldown every other stop reason already respects.

## [0.6.75] — 2026-08-29

- Fix #755: closes a gap where the whole-house fan (or HVAC fan) could briefly turn back on right after stopping because indoor cooled to your comfort floor. The fast, tick-level check that stops the fan at the floor now waits out the same 5-minute cooldown other stop reasons already respect, instead of letting the fan restart the moment indoor ticks up by even 1°F.

## [0.6.74] — 2026-08-27

- Feat #757: no user-visible change. Strangler-fig graduation Phase 6 Step 8 — collapses the dual-engine shadow-comparison shell. With every subsystem's legacy/FSM cutover flag already gone (Steps 1-7), the coordinator's two live AutomationEngine instances had become behaviorally identical, so the second (shadow) instance, its diagnostic sensor, its primary-switch entity, and the comparison plumbing between them are no longer needed. One internal safety detail: 8 call sites that fed the override/grace FSM as a side effect of the now-removed mirroring call were rewired to feed it directly, so that FSM keeps receiving the same events it always did. Also removed a dead historical-scenario tool mode whose only data source (the shadow-comparison diagnostic) no longer exists.

## [0.6.73] — 2026-08-23

- Feat #757: no user-visible change. Strangler-fig graduation Phase 6 Step 7 — removes the legacy (pre-FSM) classification code path, the final subsystem in the migration. The FSM-based ODE ceiling guard has been production-authoritative for weeks with zero corpus divergence, so the ~190-line legacy eligibility/dormancy/breach-scan/lead-time block and the differential-comparator scaffolding are no longer needed — no automation subsystem in Climate Advisor carries a legacy/FSM cutover flag anymore. No new production bugs were found.

## [0.6.72] — 2026-08-23

- Fix #764: at bedtime, if the whole-house fan was already running and doing useful free cooling (natural ventilation or the evening economizer), Climate Advisor could turn the fan off and start the AC compressor anyway — even right after its own activity log said the fan session would continue. Fixed: bedtime now leaves an active fan session alone in both places that could tear it down, matching what it already logs.

## [0.6.71] — 2026-08-23

- Feat #757: no user-visible change. Strangler-fig graduation Phase 6 Step 6 — removes the legacy (pre-FSM) occupancy dispatch code path. The FSM-based occupancy dispatch has been production-authoritative for weeks with zero corpus divergence, so the legacy inline branches in `handle_occupancy_away()`/`handle_occupancy_home()`/`handle_occupancy_vacation()` and the differential-comparator scaffolding are no longer needed. Unlike prior graduation steps, no new production bugs were found — the legacy and FSM paths were already behavior-identical.

## [0.6.70] — 2026-08-23

- Feat #757: no user-visible change. Strangler-fig graduation Phase 6 Step 5 — removes the legacy (pre-FSM) nat-vent code path. The FSM-based nat-vent dispatch has been production-authoritative for weeks with zero corpus divergence, so the 10 inline legacy call sites and the differential-comparator scaffolding are no longer needed.
- Fix #765: opening a door/window during an active manual-override grace period, with outdoor conditions favorable for free cooling, could stay suppressed by an unrelated overheat-exception rule instead of activating immediately as designed. Fixed.
- Fix #765: nat-vent exiting at the away-mode ceiling could immediately re-activate via the idle-open reactivation gate, flip-flopping in and out right at the ceiling boundary instead of staying settled and defeating the away-mode overheating guard the ceiling exists to enforce. Fixed — the away-ceiling exit now arms the same reactivation lockout every other exit reason already did. Both #765 fixes were latent design gaps in the FSM path since it was first built, invisible until this step made the path unconditional; no known live incident from either.

## [0.6.69] — 2026-08-23

- Feat #757: no user-visible change. Strangler-fig graduation Phase 6 Step 4 — removes the legacy (pre-FSM) door/window pause/grace code path. The FSM-based door/window dispatch has been production-authoritative for weeks with zero corpus divergence, so the old inline flag-write branch and its differential-comparator scaffolding are no longer needed.
- Fix #762: two FSM input builders defaulted an unconfigured fan_mode to "whole_house_fan" instead of the disabled default every other fan_mode read in the file uses, which could silently let a home with no whole-house fan configured skip a door/window pause it should have taken. Fixed.
- Fix #762: the choke-point guard that refuses to arm an active HVAC mode through an open window (`_apply_comfort_band()`) could, in a narrow timing window right after the whole-house fan was turned off manually, fail to mark the pause visible on the dashboard even though HVAC was correctly being held off. Fixed.
- Fix #762: after grace expired while paused mid-grace, a genuine nat-vent reactivation could be missed due to a stale pause flag. Fixed. All three #762 fixes were latent design gaps in the FSM path since it was first built, invisible until this step made the path unconditional; no known live incident from any of them.

## [0.6.68] — 2026-08-23

- Feat #757: no user-visible change. Strangler-fig graduation Phase 6 Step 3 — removes the legacy (pre-FSM) override/grace code path. The FSM-based override/grace dispatch has been production-authoritative for weeks with zero corpus divergence, so the old inline flag-write branch and its differential-comparator scaffolding are no longer needed.

## [0.6.67] — 2026-08-23

- Feat #757: Strangler-fig graduation Phase 6 Step 2 — removes the legacy (pre-FSM) fan/whole-house-fan code path. The FSM-based fan/WHF dispatch has been production-authoritative since Phase 5, so the 17 legacy closures and their differential-comparator scaffolding are no longer needed.
- Fix #759: the whole-house fan's fast, tick-level thermostatic stop check had been silently inert since Phase Fan/WHF went live — it never actually compared live indoor/outdoor temperatures, so the fan only stopped via the slower 30-minute cycle instead of immediately when conditions no longer favored it. Fixed; the fan now stops as promptly as originally intended.
- Fix #759: the whole-house fan's physical-drift self-correction was firing one backstop tick early (after ~5 minutes instead of the intended two consecutive ticks / ~10 minutes), reducing its protection against momentary sensor flaps. Fixed.
- Fix #759: restored a missing log line explaining why a too-soon fan on/off reversal was suppressed by the anti-flap rate limiter — the suppression itself was always working correctly, this only affects log visibility.

## [0.6.66] — 2026-08-23

- Feat #757: no user-visible change. Strangler-fig graduation Phase 6 Step 1 — removes the legacy (pre-FSM) economizer code path. The FSM-based economizer has been production-authoritative since Phase 5 with zero corpus divergence across weeks of live operation, so the old two-phase branch and its differential-comparator scaffolding are no longer needed.

## [0.6.65] — 2026-08-23

- Fix #696: closes a gap where the whole-house fan could briefly turn back on below your daytime comfort band shortly after shutting off at the comfort floor. The re-check that runs after a pause now properly waits out the same 5-minute cooldown other exit types already respect, instead of restarting the fan the moment indoor ticks up by even 1°F.

## [0.6.64] — 2026-08-22

- Feat #749: adds a hard-invariant watchdog that checks, every update cycle, whether the AC and the whole-house fan are ever physically running at the same time — a condition that should never happen. If it ever does, you'll see an immediate notification and a warning on the Status tab, even if the automation's own internal bookkeeping looks fine. Detects and alerts only — it never takes any action on its own.

## [0.6.63] — 2026-08-22

- Fix #748: the AC and the whole-house fan could end up running at the same time when a WHF session had been started via an RF remote timer and you then changed the thermostat mode by hand — the fan-off command silently never happened. Your most recent action now always wins: setting the thermostat while a remote-timer WHF session is active immediately turns the fan off, no matter how much time is left on the timer.

## [0.6.62] — 2026-08-22

- Feat #746: no user-visible change. Strangler-fig completion program Phase 5 (final subsystem extraction) — extracts the economizer's two-phase window-cooling logic into a differentially-validated lifecycle FSM. Zero divergence across the full 90-scenario test corpus and the 81-scenario golden suite. All 3 remaining monolithic subsystems named in the Strangler Fig Atlas are now extracted.

## [0.6.61] — 2026-08-22

- Feat #744: no user-visible change. Strangler-fig completion program Phase 4 — extracts occupancy dispatch (away/vacation/home transitions) into a differentially-validated FSM plus a new pure priority resolver for guest/vacation/home/away toggle logic. Zero divergence across the full 90-scenario test corpus and the 81-scenario golden suite.

## [0.6.60] — 2026-08-22

- Feat #742: no user-visible change. Strangler-fig completion program Phase 3 — extracts the daily classification decision pipeline (the hub every other automation subsystem's guard output flows through) into a differentially-validated FSM, including a ~190-line proactive-cooling escalation check that had never been pulled out of the main decision loop before. Zero divergence from current behavior across the full 90-scenario test corpus and the full 81-scenario golden suite.

## [0.6.59] — 2026-08-22

- Feat #738: no user-visible change. Strangler-fig completion program Phase 2 — a sustained live disagreement between the production automation engine and its shadow-engine comparison now automatically becomes a candidate regression-test scenario via the existing pending/golden review pipeline, instead of only a WARNING log line a human has to notice.

## [0.6.58] — 2026-08-22

- Feat #737: no user-visible change. Strangler-fig completion program Phase 1 — adds an automated static check catching two or more functions that independently reimplement the same decision gate before they drift apart. Validated against Issue #608: found the duplication claim was 2/3 already fixed and corrected the stale doc; found 2 additional real duplicate-gate instances and tracked all of them in the checker's enforced registry.

## [0.6.57] — 2026-08-22

- Fix #735: no user-visible change. Documentation and internal-consistency hygiene pass ahead of the strangler-fig completion program's next phases (classification/occupancy/economizer FSM extraction, then legacy-engine retirement) — corrected a stale fan_fsm.py docstring, fixed ~5,000-line-stale citations in docs/occupancy-dispatch-spec.md, and documented (via five-whys) why fan/WHF control is deliberately not yet registered with lifecycle_dispatcher.py.

## [0.6.56] — 2026-08-22

- Fix #733: after an HA restart, an already-favorable whole-house-fan natural-ventilation session could be silently cancelled a moment after Climate Advisor turned it on, leaving the fan running with no thermostatic oversight until the next scheduled check the following morning — the startup fan reconciliation now defers to a just-issued fan command instead of overriding it, and any orphaned backstop timer is cleaned up so oversight can never silently lapse.

## [0.6.55] — 2026-08-21

- Feat #731: no user-visible change. Continues the internal automation-engine refactor (fan/whole-house-fan control) with the same extract-and-shadow-validate pattern already applied to nat-vent, door/window, and override/grace — adds a shadow-diagnostic comparison axis so the fan/WHF FSM's agreement with production can be watched the same way the other three already are; see #594/#727/#729 for background.

## [0.6.54] — 2026-08-21

- Feat #729: simplifies the Shadow Engine Primary switch added in 0.6.53 down to the single control you actually use — the 3 separate nat-vent/door-window/override-grace FSM toggles are gone, replaced by one choice (legacy engine or FSM engine). Promoting now reloads the integration instead of swapping live, which closes a real gap where an in-progress timer (a grace period, a pending setpoint retry) could keep running against the wrong engine after a switch. Logs now record which engine issued each command, so it's provable after the fact.

## [0.6.53] — 2026-08-21

- Feat #727: the 3 nat-vent/door-window/override-grace FSM-authoritative switches now hold whatever state you last set them to across a Home Assistant restart, instead of always reverting to off. Also adds a new switch, Shadow Engine Primary, that lets you promote the diagnostic shadow engine to be the one actually operating your thermostat/fan — previously it could only compare its decisions against production, never act on them. Also persisted across restart, and instantly reversible.

## [0.6.52] — 2026-08-21

- Fix #724: no user-visible change. Closes a gap in the internal diagnostic that shadows automation decisions to verify safety-logic correctness — its copy of the whole-house-fan suppression state was never kept in sync, which could make the diagnostic falsely report a disagreement during completely normal overnight whole-house-fan use with a window open.

## [0.6.51] — 2026-08-21

- Fix #721/#722: no user-visible change. Closes the last two internal cross-checks left open by #717 — the door/window pause guard and the whole-house-fan/HVAC suppression tracker now both get the same audit trail as the rest of the safety logic. Also found and fixed two untracked fan-suppression release points that a prior investigation had missed.

## [0.6.50] — 2026-08-21

- Fix #717: no user-visible change. Wires the internal cross-check that lets the natural-ventilation, door/window, and manual-override safety logic confirm they're seeing the same events, into production for real — closes a piece of scaffolding that existed but was never connected. Every decision still comes from the same logic as before; this only makes the audit trail behind it real.

## [0.6.49] — 2026-08-21

- Fix #716: no user-visible change. The internal shadow-engine diagnostic that validates upcoming automation changes before they're allowed to affect real HVAC behavior wasn't tracking whether the whole-house/HVAC fan was on — so a related check could never meaningfully agree or disagree with production. It now does, closing a gap in the safety net that gates future automation changes; nothing about how the fan itself is controlled changed.

## [0.6.48] — 2026-08-21

- Fix #714: the whole-house fan and an active thermostat mode (cool/heat) can no longer run at the same time. If you manually change the thermostat mode while free cooling is running, the fan now stops immediately instead of continuing to cycle in the background, and it won't silently turn your thermostat back off anymore if it happens to reactivate while your manual change is still in effect.

## [0.6.47] — 2026-08-21

- Fix #711: closes a gap where an active whole-house-fan free-cooling session that was already running when you wake up wasn't re-checked against the daytime comfort band until whatever the next unrelated check happened to be — up to 5 minutes later. If indoor drifted below the graceful cycle-off point in that window, the fan could end up cycling off and back on again shortly after, instead of cycling off smoothly right at wake-up.

## [0.6.46] — 2026-08-20

- Fix #706: closes a gap where, if you opt into the nat-vent state-machine engine, it could lose track of an active manual override in two ways: not recognizing one was already in effect, and — in rare timing cases — briefly overwriting a fan override that started while a decision was in flight. Also teaches it the existing rule that free cooling should keep running during a protected period if the house is genuinely overheating. No change unless you've opted in.
- Fix #707: no user-visible change. After certain restarts with an active whole-house-fan remote timer, a diagnostic comparison (not any real fan/HVAC decision) could report a false disagreement for several minutes. Purely a live-verification signal fix.
- Fix #708: closes a gap where, if you opt into the nat-vent state-machine engine, one specific moment — deciding whether to resume free cooling right after a grace period ends — was still always decided by the old code regardless of that setting. No change unless you've opted in.
- Fix #709: closes a gap where, if you opt into the door/window state-machine engine, two of its eight decision points didn't actually change your grace-period status the way the setting implied, and a rare zero-length-grace configuration could leave a phantom grace period reported that never cleared on its own. No change unless you've opted in.

## [0.6.45] — 2026-08-20

- Fix #684: no user-visible change. A diagnostic-only comparison that checks whether the nat-vent state-machine engine (still not authoritative over any real decision unless you've opted in) agrees with production used a fixed 5-minute reactivation cooldown instead of your actually configured value, when they differ. Only affects installs that changed the reactivation lockout from its default — no change to any real fan/HVAC decision either way.

## [0.6.44] — 2026-08-20

- Feat #698: the whole-house fan can now briefly pause itself mid-session once the room hits your comfort target, then resume automatically if it drifts back — instead of running the whole time regardless. With the state-machine switch enabled, a running free-cooling session also now reacts immediately (instead of waiting up to 30 minutes) if conditions change enough to end it for any reason, not just if the house gets too cold. Also fixed a small pre-existing mismatch where the fan could stay on slightly too long after outdoor air warmed past indoor, by reusing the same shared check used elsewhere. No change for installs that haven't opted into the state-machine switch, aside from the outdoor-air mismatch fix, which applies to everyone.

## [0.6.43] — 2026-08-19

- Fix #694: fixed 3 defects introduced by the previous nat-vent state-machine wiring pass (still not authoritative over any real decision by default). With the state-machine switch enabled, an in-flight natural-ventilation session (free cooling already running) could be killed outright or silently downgraded from a stronger cooling mode to a weaker one whenever a second door or window was opened during that session — even though nothing about outdoor/indoor conditions had changed. Also fixed a case where reopening a window during an existing door/window pause could leave the automation's internal pause bookkeeping in an inconsistent state. No change for installs that haven't opted into the state-machine switch.

## [0.6.42] — 2026-08-19

- Fix #690: two separate places that decide when to end a natural-ventilation session (a fast check and a slower 30-minute check) used to disagree by one degree of temperature precision at the exact moment outdoor and indoor temperatures matched — the fast check would end the session, the slow one wouldn't, for up to 30 minutes. Both now agree and end the session at the same instant once free cooling is genuinely gone. Rare edge case; no change for the common case where temperatures aren't at exact equality.

## [0.6.41] — 2026-08-19

- Fix #691: no user-visible change. Adds a new internal method that will let the nat-vent state-machine engine (still not authoritative over any real decision today) eventually drive real fan state the same proven way the door/window engine already does. Not yet connected to anything — preparation work only.

## [0.6.40] — 2026-08-19

- Fix #687: no user-visible change. The nat-vent diagnostic engine (used to validate a future state-machine switchover, not authoritative over any real fan/HVAC decision today) couldn't see when a manual fan override or grace period was active, so it reported "would activate" for the full duration of any manual override — the single largest diagnostic-disagreement bucket found this session. It now correctly recognizes both.

## [0.6.39] — 2026-08-19

- Fix #685: no user-visible change. The shadow-diagnostic "disagreement" warning (used to validate the new state-machine engines against the existing production logic before any future switchover) used to fire the instant a real multi-step transition briefly looked different between the two computations, even when both settled on the same answer within seconds. It now only logs once a disagreement has genuinely persisted for 60 seconds, so the diagnostic signal reflects real problems instead of momentary timing noise.

## [0.6.38] — 2026-08-19

- Fix #680: no user-visible change. Closes a minor structural gap in the override/grace FSM dispatcher (Issue #664): the restart clean-slate reset directly assigned its 3 governed flags instead of routing through the single dispatch point every other real call site uses. Both paths already produced the same clean-slate result, so there was no behavioral bug — this closes the "exactly one writer" gap before it's relied upon.

## [0.6.37] — 2026-08-19

- Fix #679: no user-visible change. Closes another instance of the same shadow-diagnostic gap class as #676: the Issue #508 stuck-grace backstop correctly notified the override/grace diagnostic FSM when force-cancelling an orphaned grace, but never the door/window diagnostic FSM, which could show a stale "disagreement" for up to 10 minutes after a real recovery. Real HVAC/fan behavior was always correct throughout; only the diagnostic mirror could drift.

## [0.6.36] — 2026-08-19

- Fix #677: after a restart that lands in the middle of an active QuietCool RF remote timer, Climate Advisor now reads the remote's own live state to recognize the timer is still running and re-arms the correct remaining time, instead of forgetting about it. Previously, when the physical timer later shut the fan off naturally, CA misread it as a fresh manual power-off and started a fresh 3-hour lockout — blocking free cooling for hours even with ideal outdoor air.

## [0.6.35] — 2026-08-18

- Fix #676: no user-visible change. Closes a second, separate shadow-diagnostic gap found immediately after #672/#673 shipped: when a grace period expired with a door/window sensor still open and free-cooling conditions happened to be favorable, natural ventilation correctly resumed and the pause was correctly cleared, but the shadow diagnostic engine was never told about it and could show a stuck false "disagreement" for 20+ minutes. Real HVAC/fan behavior was always correct throughout; only the diagnostic mirror could drift.

## [0.6.34] — 2026-08-17

- Fix #673: no user-visible change. Closes a structural gap in the shadow-diagnostic safety net related to #672 — four nat-vent/door-window fields were never included in the periodic raw-copy step that keeps the shadow diagnostic engine in sync, so a single missed update anywhere in the code could cause a permanent false "disagreement" reading with no way to self-correct. Real HVAC/fan behavior was always correct throughout; only the diagnostic mirror could drift.

## [0.6.33] — 2026-08-17

- Fix #672: no user-visible change. Three shadow-diagnostic state machines (door/window, nat-vent, override/grace) each had their own reason for getting permanently stuck out of sync with real production state after a restart or a specific state transition — real HVAC/fan behavior was always correct throughout. Fixed all three.

## [0.6.32] — 2026-08-17

- Fix #670: right after an HA restart, if a door or window was already open, the whole-house fan could switch on before the startup-reconciliation logic had a chance to check the fan's actual state — occasionally causing a fan on/off flap in the minutes after restart. The regular-cycle nat-vent and window-cooling checks now wait for startup reconciliation to finish before acting, same fix already applied to a sibling check in #627.

## [0.6.31] — 2026-08-17

- Fix #668: no user-visible change. The shadow-diagnostic door/window FSM was being wrongly reset every automation cycle whenever a door/window was left open with no imminent free-cooling opportunity — a diagnostic-only bug (real HVAC pause behavior was always correct). The periodic nat-vent re-check was unconditionally signalling "nat-vent just reactivated while paused" on every call, regardless of whether that actually happened. Made the signal event-driven instead, so it only fires when nat-vent genuinely reactivates.

## [0.6.30] — 2026-08-17

- Fix #666: no user-visible change. The coordinator test harness silently dropped the shadow-diagnostic FSM feed for every nat-vent/door-window exit event — a test-infrastructure bug, not a production one (real HVAC pause behavior was always correct). Fixed the harness wiring, closed a matching coverage gap where one specific nat-vent exit reason never emitted its Activity Report event at all, and added a regression test proven load-bearing against a real revert.

## [0.6.29] — 2026-08-17

- Feat #664: the override/grace lifecycle FSM (whole-house-fan and thermostat manual overrides, and the grace period that protects them from being undone) can now optionally drive real production decisions instead of only observing them — a new, off-by-default, non-persisted switch, matching the same pattern nat-vent and door/window already have. Unlike door/window's staged rollout, this ships full authority for all 8 real trigger sites at once, since investigation proved the FSM and the existing logic always compute identical results, confirmed across the full scenario library with the switch turned on. Also fixes a config edge case found during this work: a manual grace period disabled via configuration (0 seconds) could have been reported as active with no way to ever clear it, had the switch been turned on before this fix. The switch defaults off — no occupant-visible behavior change from this release alone.

## [0.6.28] — 2026-08-16

- Fix #661: the override/grace shadow FSM's diagnostic accuracy for fan overrides (whole-house-fan remote timers, physical fan-on detection) is now correct — it previously modeled a confirmation delay that fan overrides never actually go through in production, causing a spurious disagreement reading on the most common override path. No occupant-visible change: override/grace has no authoritative switch and never drove real decisions — this only fixes what the diagnostic sensor reports.

## [0.6.27] — 2026-08-16

- Fix #660: the door/window pause/grace lifecycle FSM now has full, off-by-default authority for all 8 real trigger sites — completing the migration begun in #637. Also fixes a real gap found during that work: when a grace period was already running and a door/window pause independently became active too, the FSM's own tracked state could disagree with what production actually did, and a resume-after-close could restore the wrong prior HVAC mode in a specific reachable sequence. Both are fixed at the source for every caller, not patched per call site. The switch that lets this FSM actually drive decisions (instead of just tracking them for comparison) stays off by default — no occupant-visible behavior change from this release alone.

## [0.6.26] — 2026-08-16

- Fix #655: a door/window briefly reopened during an active grace period could still pause the AC/heat, even though the grace period exists specifically to avoid reacting to exactly that. The grace check now uses the same accurate indoor+outdoor reactivation check the automation already computes a moment later, instead of a coarser outdoor-only shortcut that could disagree with it — grace now reliably holds for its full duration.
- Fix #657: after a grace period ends with a door/window still open but conditions now favor natural ventilation, some pause-related dashboard and Activity Report fields (which door/window, how long it's been paused) could keep showing stale information from an earlier pause. These now clear correctly alongside the rest of the pause state.
- Fix (found during #637 Phase R Step 3 scoping, no user-facing symptom confirmed): a nat-vent-exit pause path wrote fewer pause-tracking fields than the equivalent door/window pause path, which could leave a dashboard field stale and — in one specific edge case — cause a later door-close to start an unwanted extra grace period. Both pause paths now share one definition of what a door/window pause writes.

## [0.6.25] — 2026-08-16

- Feat #637 (Phase R Step 2, partial): begins letting the door/window pause/grace lifecycle FSM actually drive production decisions — a new, off-by-default switch lets it take over 2 of the lifecycle's 7 actions (a manual thermostat override detected during a pause, and resuming from a dashboard pause) instead of the older logic. Both were proven behavior-identical to the existing logic before this shipped, across the full scenario library plus dedicated tests. The switch defaults off — no occupant-visible behavior change unless it is explicitly turned on, and even then only for those 2 actions; everything else about door/window pause/grace handling is unchanged.

## [0.6.24] — 2026-08-16

- Feat #637 (Phase R Step 1b): internal refactor only, no user-visible behavior change — closes the last coverage gap in the door/window pause/grace lifecycle's diagnostic-only shadow FSM (Block 5 series, epic #594). 3 of its 7 tracked event kinds (grace-timer expiry, dashboard resume, and a sensor-state reconcile check) were never fed to it, deferred as future work when the FSM was first built. All 7 are now fed. Purely observational — nothing it computes is ever acted on.

## [0.6.23] — 2026-08-16

- Fix #637: after a grace period expires with a door/window still open, if natural ventilation now takes over cooling, the system was still privately marking itself as "paused by door" — which could suppress the away/vacation energy setback later, and made the dashboard/API misreport the reason HVAC was off. Now clears correctly the moment nat-vent takes over, matching how every other nat-vent-activation path already behaves.

## [0.6.22] — 2026-08-16

- Feat #633 (Phase R prep): begins the cutover work for the nat-vent lifecycle FSM (Block 5 series, epic #594) — modeled the one remaining gap in its transition table (soft-start escalating to full free-cooling mid-session), and added an opt-in, off-by-default switch that lets the FSM's decision drive the real whole-house-fan/HVAC calls for nat-vent instead of the legacy inline computation. Proven behavior-identical to the legacy path across the full scenario library before this shipped. The switch defaults off and does not persist across a restart — no occupant-visible behavior change unless it is explicitly turned on.

## [0.6.21] — 2026-08-16

- Fix #651: internal refactor only, no user-visible behavior change — two more gaps in the diagnostic-only shadow/FSM comparisons (Block 5 series, 0.6.13–0.6.20). A manual override made directly at the thermostat wasn't registering with the diagnostic at all (fan overrides already worked, since #643). A fan-only override cleared by the bedtime or morning-wakeup schedule now reflects immediately instead of self-correcting a cycle later. Purely observational — nothing it computes is ever acted on.

## [0.6.20] — 2026-08-16

- Fix #649: follow-up to #641's whole-house-fan rapid-cycling protection. The 5-minute floor itself was already working correctly, but the Activity Report and HA logs made a blocked toggle look like it had actually happened, and repeated the same misleading row every time the system re-checked while still blocked. A blocked-then-later-applied fan toggle now shows as a single accurate "deferred" entry followed by one real "applied" entry once the floor clears, and is no longer mislabeled as an incident — it's the protection working as intended.

## [0.6.19] — 2026-08-16

- Fix #647: internal refactor only, no user-visible behavior change — the diagnostic-only shadow/FSM comparisons from the Block 5 series (0.6.13–0.6.17) were disagreeing with production on nearly every automation cycle, not just occasionally. A wiring gap left each FSM's tracked state stuck once a real manual override, grace period, or certain nat-vent exits occurred, instead of resetting once each finished. Fixed by feeding each FSM from every real production transition, not just the ones already replayed to the shadow engine. Purely observational — nothing it computes is ever acted on.

## [0.6.18] — 2026-08-15

- Fix #645: after a redeploy or restart, the dashboard could briefly show HVAC mode 'cool' next to 'windows open (as planned)' — a monitored window/door sensor blipping unavailable-then-on during startup reset its change timestamp, which made the automation's debounce check treat the window as still settling and skip the guard that normally refuses to command an active HVAC mode through an open window. The compressor never actually ran in the reported case (the target temperature was still above the indoor reading), but on a warmer morning this could have let real cooling run with windows open. The guard now always blocks arming an active mode while a monitored window is open, regardless of that startup timing race.

## [0.6.17] — 2026-08-15

- Fix #643: internal refactor only, no user-visible behavior change — the diagnostic-only shadow comparison from the Block 5 series (0.6.13–0.6.15) wasn't seeing manual fan overrides, the most common override trigger, so it couldn't confirm its own consistency after one occurred. Now mirrored like every other tracked automation decision. Purely observational — nothing it computes is ever acted on.

## [0.6.16] — 2026-08-15

- Fix #641: the whole-house fan could rapidly cycle on and off (roughly once a minute) when a predicted-floor or ceiling-threshold exit fired while a window was still open — the very next check immediately turned it back on, repeating indefinitely. Two nat-vent exit conditions now correctly hold off reactivation for 5 minutes after exiting, matching how the outdoor-air-reversal exit already behaved. As a second layer of protection, CA will never toggle the fan faster than once every 5 minutes going forward, regardless of cause — any future situation that would have caused rapid cycling is now blocked outright and logged as an incident instead of hitting the fan.

## [0.6.15] — 2026-08-14

- Feat #639: internal refactor only, no user-visible behavior change — Block 5 Phase 3 (the final phase) builds the unified override/grace transition table, completing the diagnostic-only shadow comparison series started by 0.6.13's nat-vent FSM and 0.6.14's door/window FSM. Confirmed via a new test scenario that a second, different override arriving while a prior override's grace period is still running is correctly treated as a fresh override rather than ignored. Purely observational — nothing it computes is ever acted on.

## [0.6.14] — 2026-08-14

- Feat #637: internal refactor only, no user-visible behavior change — Block 5 Phase 2 builds the unified door/window pause/grace transition table, the next diagnostic-only shadow comparison point after 0.6.13's nat-vent one. Confirmed (via new test scenarios, not just static analysis) that production can genuinely be paused-by-door and in-grace at the same time — the new state models that combination rather than assuming it can't happen. Purely observational — nothing it computes is ever acted on.

## [0.6.13] — 2026-08-13

- Feat #633: internal refactor only, no user-visible behavior change — the diagnostic-only decision table added in 0.6.12 now actually runs alongside production on every natural-ventilation check, compared against what production really did. Still purely observational — nothing it computes is ever acted on.

## [0.6.12] — 2026-08-13

- Feat #633: internal refactor only, no user-visible behavior change — assembles the natural-ventilation logic into one explicit, thoroughly-tested decision table and a small generic messaging mechanism for coordinating between the automation's different behaviors, laying the groundwork for the same treatment to extend to the rest of the automation logic over time. Not yet connected to anything the system does today.

## [0.6.11] — 2026-08-13

- Fix #631: the diagnostic-only shadow engine (used to validate an in-progress automation-logic refactor, never touches real hardware) could disagree with production for hours at a stretch whenever a manual override or a fan RF-remote override was active, because it never learned that a grace period was in effect. It now stays in sync with production's override/grace state on every check, closing a gap that could make its disagreement warnings unreliable during exactly the periods they'd matter most.

## [0.6.10] — 2026-08-13

- Fix #629: right after turning off the whole-house fan, the air conditioner could silently switch itself into Cool mode while a monitored window was still open — with no pause, no notification, and nothing in the logs even saying the mode had changed. A routine background check that keeps the thermostat's setpoint current was allowed to also change its mode, and nothing double-checked that a window wasn't open before it did. The AC now refuses to switch itself on while a monitored window is open, the same way it already refuses to fight the whole-house fan — and any time that check changes the mode, it's now spelled out in the log.

## [0.6.9] — 2026-08-11

- Fix #627: after a restart during an active whole-house-fan session (e.g. one started via RF remote), Climate Advisor could silently turn the fan off within the first second and then switch the air conditioner into Cool mode roughly 30 seconds later — running the AC and whole-house fan at the same time, which the automation is specifically designed to prevent. A periodic safety check meant to catch a truly stray fan was firing before the system had finished settling back in after the restart. It now waits for that settling window to close before acting, the same way every other restart-related check already does.

## [0.6.8] — 2026-08-11

- Fix #625: the Status card's grace-period text (added in 0.6.6, #620) had grown into a long, duplicated sentence — for a whole-house-fan override it repeated what the Fan (WHF) card already said, in different words. It now shows a short cause (e.g. "WHF override", "thermostat override") plus how long the grace period was set for and when it ends — the same compact style the Fan (WHF) card already uses for its remote timer. It also now shows a cause at all when you manually change the thermostat directly (mode or temperature) — previously that case showed no cause, or occasionally an unrelated leftover from an earlier event.

## [0.6.7] — 2026-08-12

- Fix #623: briefly opening a monitored door (e.g. walking outside) could trigger an instant "HVAC paused" notification, bypassing the debounce window you configured to ignore momentary opens. A timing race in the previous release's fix (0.6.6, #620) let this happen; the debounce check is now immune to that race, so a quick in-and-out through a door is correctly ignored.

## [0.6.6] — 2026-08-11

- Fix #620: if you turned the whole-house fan off manually while a window was open and the outdoor air was still favorable, the automation could turn it back on within seconds, undoing your action. Separately, once a fan session ended (for any reason) with a window still open, the AC or heat could get set active with that window open — even if the window had been open for a while and nothing had ever noticed. All three now correctly pause instead. Also: the Status card now shows how much longer an active grace period will last and why it started, information that was previously only visible on the Debug tab.

## [0.6.5] — 2026-08-10

- Fix #618: on a hot or cold day, if a whole-house-fan/natural-ventilation session ended while a window was still open, HVAC could stay silently un-managed for hours after the window closed — classification wanted the AC or heat on, but the mode never got applied and nothing indicated a problem. A related bug could also cancel AC that had just started cooling, moments after it began, if the thermostat reported a normal post-cycle fan phase. Both are fixed. Also: a specific corrective HVAC-mode restore now shows up in the Activity Record instead of being invisible.

## [0.6.4] — 2026-08-09

- Fix #615: internal fix only, no user-visible behavior change — the diagnostic shadow engine added in 0.6.3 was missing several real-world inputs (outdoor temperature, forecast, and 8 of 13 decision triggers), so it could never correctly agree with the real engine even when both were doing the right thing. Fixed with full coverage plus an automated check that keeps future changes from silently reintroducing the gap. The real engine's behavior is completely unchanged; only the diagnostic sensor's accuracy is affected.

## [0.6.3] — 2026-08-08

- Feat #613: internal refactor only, no user-visible behavior change — a second, permanently inert copy of the automation engine now runs live alongside the real one, fed the same nat-vent sensor/classification inputs, and can never issue a real command. A new diagnostic sensor shows whether it agrees with the real engine's conclusions. This is groundwork for a future safe-rollout mechanism and does not change today's HVAC behavior.

## [0.6.2] — 2026-08-08

- Feat #611: internal refactor only, no user-visible behavior change — added an offline test harness that proves a second, inert "shadow" copy of the automation engine can run alongside the real one without ever issuing a real command or changing what the real engine does. This is groundwork for a future safe-rollout mechanism (test new automation logic silently before it's ever allowed to control the thermostat) and does not change today's behavior.

## [0.6.1] — 2026-08-08

- Feat #608: internal refactor only, no user-visible behavior change — the natural-ventilation exit logic (why a free-cooling session ends: comfort reached, ceiling reached, prediction, outdoor warming) is now a single, tested, verified-behavior-preserving decision instead of inline logic. Along the way, this also surfaced (documented, not yet consolidated) that natural ventilation currently evaluates some of these same exit conditions in up to three separate places — a known duplication pattern in this area; consolidating them is flagged as follow-up work.

## [0.6.0] — 2026-08-08

- Feat #606: internal refactor only, no user-visible behavior change — the natural-ventilation on/off/purge-mode logic now has a single, named, automatically-verified description of its own state (checked against every regression-test scenario), laying groundwork for safer future automation-logic changes in this area.

## [0.5.67] — 2026-08-08

- Feat #604: internal refactor only, no user-visible behavior change — makes it safe to eventually build a second, non-acting engine instance for testing automation changes without risk to the live system, by giving it its own isolated set of callbacks instead of ones that could reach into the real thermostat.

## [0.5.66] — 2026-08-08

- Fix #602: the daily learning record (which gates manual-override detection for setpoint-only changes, HVAC runtime tracking, comfort-violation minutes, occupancy-away minutes, door/window pause counts, and the thermal-learning watchdog) was only ever created once a day by the morning briefing — if the weather integration happened to be unavailable at that one fixed moment, all of that silently stopped working for the rest of the day, with no warning. It now also gets created by the regular classification cycle, which already retries weather forever — the gap shrinks from up to 24 hours to about 30 minutes. Fix #598: a test scenario covering Issue #505's vacation-override-cleared fix was passing by coincidence rather than exercising the real behavior — this fix gives it real coverage.

## [0.5.65] — 2026-08-08

- Fix #600: after an HA restart or grace-period expiry with the whole-house fan already running for natural ventilation, the Activity Record no longer shows the same "Fan activated" adoption logged 2-3 times in the same minute — the fan itself only ever turned on once; only the redundant log/event entries are gone. Also fixes the displayed nat-vent session start time silently jumping forward on each redundant re-confirmation.

## [0.5.64] — 2026-08-08

- Feat #593: closed out the remaining Activity Record payload-completeness gaps from the #584 investigation — classification decisions now show the trend magnitude and the exact threshold/margin that produced the day type; setpoint retry/nudge events show the reject streak count; startup coalescing shows indoor/outdoor temps and fan archetype; the thermal-learning watchdog shows today's session count; the fan-stopped and incident-detected cards now use data they already had instead of a generic label; morning wake-up now reports an explicit skip reason when occupancy is away/vacation, matching its other skip reasons; and pre-cool deferring to an already-active nat-vent/WHF session now shows what indoor temp and target it's deferring to. Four renderer functions with no current emitter are now explicitly marked as legacy/historical-log-only.

## [0.5.63] — 2026-08-08

- Feat #592: the Activity Record now explains *why* several nat-vent, door/window pause, and grace-recovery decisions happened, not just that they happened — "Classification suppressed" and "Occupancy setback suppressed" rows now name which sensor is open and for how long; nat-vent fan-on/floor-skip/soft-start/ceiling-escalation rows show the actual outdoor/indoor temperatures and thresholds behind the decision instead of only a derived summary number; "Override cleared" (fan-only) and "Override confirmed" rows show the reason/trigger; and a stuck-grace recovery row now names which mode/time was stale. No automation behavior changed — same decisions, more visible reasoning.

## [0.5.62] — 2026-08-08

- Fix #591: fixed the Activity Record showing the same automation decision (comfort band, classification, occupancy setback skip, nat-vent AC assist, and several others) two or three times in a row after a restart or overlapping trigger — each real decision now appears once.

## [0.5.61] — 2026-08-07

- Fix #589: disabling automation (the "Automation Enabled" switch / observe-only mode) now also stops the whole-house-fan command-only reconciliation path. Previously, on installs where the fan entity only echoes commands (fan_state_feedback=False), this path kept issuing real fan on/off commands every ~30 minutes even with automation disabled — the only automated action that didn't respect the switch. It now honors dry_run like every other automated action.

## [0.5.60] — 2026-08-05

- Feat #580: the dashboard's Activity Record report now defaults to the "Last 12 hours" time window instead of 24, and lists events newest-first (most recent at the top, oldest at the bottom) instead of oldest-first — so the events you actually care about no longer require scrolling past a full day of history to find. The AI Investigative Analysis report type is unaffected and keeps its own separate time-window defaults.

## [0.5.59] — 2026-08-05

- Fix #578: several AI Investigative Analysis report-quality fixes from user feedback — the "Submit GitHub Issue" button now titles the issue "AI Investigative Analysis - <date>" instead of grabbing the first sentence of the report as the title; target_temp_low/high reading "unknown" while the HVAC is legitimately off (e.g. running whole-house-fan/nat-vent only) is now labeled as expected instead of flagged as a data-quality issue; the weather bias cap is now included in the report's context so the AI can actually check against it; "Manual Overrides Today" now shows a separate fan override count alongside the setpoint override count so the two no longer look contradictory; and "System Errors/Warnings" now reflects real captured WARNING/ERROR log records instead of a name-matching quirk that almost never caught anything. The AI Activity Report feature (separate from AI Investigative Analysis) has been retired entirely — it was superseded by the deterministic, non-AI Activity Record and had not written new data since the #563 skill merge. The Investigative Analysis report's default time window is now "Last 1 day" instead of 7 days, and new installs now default to Sonnet 5 at low reasoning effort instead of an outdated model at medium effort.

## [0.5.58] — 2026-08-05

- Feat #573 follow-up: replaced the menu-based "Save"/"Save and Reload" options added in 0.5.57 — Home Assistant's options-flow menu can't render an actual button (only a plain list row), so those looked identical to the settings sections instead of a real action. Each settings section now just has its normal Submit again; saving a section raises a repair notice (Settings -> System -> Repairs) telling you Climate Advisor has changes waiting, with a one-click Reload right from there.

## [0.5.57] — 2026-08-05

- Feat #573: editing several AI/comfort/schedule settings sections in one visit to Configure used to reload Climate Advisor after every single section's Submit, rebuilding the coordinator and AI client each time. Each section now just saves; applying pending changes is done via a repair notice guiding you to reload.

## [0.5.56] — 2026-08-05

- Fix #572: claude-sonnet-5's first request after being selected could silently hang for up to 90 seconds with no visible output at all before failing — a known model quirk that #565/#568/#569 tried to work around by learning it from a live failure and remembering that lesson, but a genuine Home Assistant restart could silently erase the lesson, so the failure kept coming back. Climate Advisor now ships pre-verified, correct settings for every supported Claude model instead of learning them from a failure — so a supported model's very first request already works correctly, no failed attempt required.

## [0.5.55] — 2026-08-05

- Fix #571: a legitimate whole-house-fan nat-vent exit was being misread as an externally-owned fan and force-corrected by an emergency reconcile — every single cycle, all morning. The Activity Report showed "Fan running (untracked)" and "fan found running without a CA-owned session" moments after Climate Advisor's own clean exit, instead of just the clean exit itself. Also fixed a related gap: the HVAC-fan dashboard status could get stuck showing "active" even after the fan genuinely stopped, on HVAC-integrated-fan configurations.

## [0.5.54] — 2026-08-05

- Fix #567: the whole-house fan's own automation-issued commands could get heard back on the QuietCool remote's RF channel and misread as a person pressing the physical remote — falsely handing fan control away from Climate Advisor for up to 3 hours and mislabeling the Activity Report as a manual action that never happened. Also fixed a related report-only issue: when Climate Advisor quietly corrects its own stale fan-tracking (no user involved at all), the Activity Report now says so instead of also claiming "user turned off".

## [0.5.53] — 2026-08-05

- Fix #568: the AI model-compatibility learning added in #565 (so Climate Advisor adapts automatically to a newer Claude model's quirks after the first request) was being silently wiped every time AI settings were saved or Home Assistant restarted — so it could never actually stick. It's now saved the same way as other AI usage stats, so it survives both. Also added clearer AI request logging so any future model-compatibility issue can be diagnosed directly from the logs.

## [0.5.52] — 2026-08-04

- Fix #565: the AI Investigator and AI Activity Report could silently burn their entire response budget with no visible answer at all on newer Claude models (confirmed with claude-sonnet-5) — the model was doing its own internal reasoning with no cap on it, and that reasoning alone could use up the whole response length before ever getting to write an actual answer. Climate Advisor now detects this and automatically applies a bounded-reasoning setting so the model always leaves room for a real answer; it also learns per-model going forward so this self-heals after the first occurrence instead of repeating on every request.

## [0.5.51] — 2026-08-03

- Fix #563: the AI Investigator was sending nearly the entire history of every fixed issue to Claude on every single run — a version-scoping check that was supposed to limit this to only recently-relevant fixes had a bug that let all 169 fixed-issue records through every time, and a separate rendering bug was expanding some of that text by roughly 15x on top of that. Investigations should now run noticeably faster and cheaper, with no loss of the "was this already fixed" cross-check the AI uses this data for.
- Fix #563: the scheduled "Generate with AI" activity narration was running the same full audit-depth analysis as an on-demand investigation (including a live GitHub fetch) — it now uses a lighter, current-activity-only context, which should make it noticeably faster.
- Fix #563: the Investigate report's progress display now shows real step-by-step status from the backend and fills in the report as sections complete, instead of a fake elapsed-seconds counter and raw unformatted text.
- Fix #563: fixed a bug where the "AI Activity Report" scheduled service call silently failed on every run after a recent internal rename.
- Fix #563: the AI model dropdown in settings now shows Anthropic's current available models automatically instead of a fixed list, and if a configured model is retired, Climate Advisor automatically switches to a comparable replacement instead of failing.
- Fix #563: fixed AI requests failing outright when a newer model no longer accepts a setting (e.g. temperature) that older models required — Climate Advisor now detects this and retries without it automatically.
- Fix #563: raised the maximum AI response length setting from 8192 to 16384 tokens, and added a clearer warning when a response uses its full budget but produces no visible output (rather than the generic "truncated" message, which incorrectly implied a bigger budget alone would fix it).

## [0.5.50] — 2026-08-03

- Fix #561: the whole-house fan could turn itself on with every door and window closed, briefly switching the thermostat off for no reason — and the log misleadingly claimed "whole-house fan manually turned on" even though nobody touched it. The fan-cycling logic now re-checks that a monitored sensor is actually open before ever turning the fan back on, instead of trusting an internal flag that could go stale for hours. Also fixed the underlying causes: a self-healing check that could keep a ventilation "session" alive after windows closed, and a rare timing gap that could start two duplicate internal timers, both of which could leave the system briefly confused about whether it or the user caused a fan change.

## [0.5.49] — 2026-08-02

- Fix #557: options dialog sections now save the instant you hit Submit — no more separate "Save & Close" step. Previously, submitting a section (e.g. Setpoints or Notifications) only staged the change in memory; re-opening that same section before hitting the separate Save button showed the old value, making it look like the change hadn't taken. Every section now writes and reloads immediately, so what you see after Submit is always what's actually saved.

## [0.5.48] — 2026-08-02

- Fix #558: the AC no longer chases a colder-than-comfort setpoint on hot days after you return from being away — it now simply restores your normal comfort setting. The overnight pre-cool banking feature (which quietly cools the house before a hot day, overnight, while it's cheap) now also runs on stretches of consecutive hot days that aren't getting hotter each day, not just the first day of a heat wave. The morning briefing no longer claims pre-cooling happened if it didn't.

## [0.5.47] — 2026-08-02

- Fix #555: Daily Briefing sensor no longer drops to "unknown" on days with a lot to say (away/vacation occupancy + dual window opportunities) — the TLDR summary is now shortened to reliably fit HA's 255-char sensor state limit, with a truncation safety net and full text still available in the sensor's attributes as a backstop.

## [0.5.46] — 2026-08-01

- Fix #553: `tools/deploy.py` now transfers files by piping a tar stream through the same SSH
  connection that extracts/restarts/verifies, instead of a separate `scp` — a full deploy is
  now capped at 3 connections total (down from ~8 after #551's partial batching), and
  `--rollback` at 1. Also fixes two real bugs caught during live validation against the
  production HA instance: a crash-safety gap where an interrupted connection mid-extraction
  could leave the live integration directory deleted or half-written (now extracts to a temp
  directory and swaps it into place as the final, near-instant step), and a backup/restore
  tar-format mismatch that could produce a broken, nested directory on rollback. As a side
  effect, deploys are now exact mirrors of the source tree (previously, files removed or
  renamed between versions could linger indefinitely). `docs/SSH-SETUP.md` documents the new
  connection budget and both superseded approaches (#549, #551). Developer/deployment tooling
  only — no change to the integration itself.

## [0.5.45] — 2026-08-01

- Fix #551: reverted #549's SSH connection multiplexing (`ControlMaster`) in
  `tools/deploy.py` — live testing showed it fails immediately against a real HAOS SSH
  add-on on this project's Windows/Git-for-Windows SSH client, making deploys worse (failing
  on the very first connection instead of getting partway through). Replaced with command
  batching — combining several remote commands into fewer SSH round trips — to reduce
  connection count and avoid tripping the add-on's rate-limit protection, without depending
  on client-side multiplexing support. `docs/SSH-SETUP.md` documents both the batching
  approach and the ControlMaster reversion. Developer/deployment tooling only — no change to
  the integration itself.

## [0.5.44] — 2026-08-01

- Fix #549: `tools/deploy.py` now multiplexes all of its SSH/SCP connections through one real
  connection per run (SSH `ControlMaster`/`ControlPath`/`ControlPersist`), instead of opening
  6-8 separate ones — avoids tripping the HA SSH add-on's rate-limit/brute-force protection,
  which was blocking deploys partway through with `Connection reset by peer` /
  `Connection timed out`. `docs/SSH-SETUP.md` documents the failure signature and the fix.
  Developer/deployment tooling only — no change to the integration itself.

## [0.5.43] — 2026-08-01

- Fix #547: `tools/deploy.py` now prints which SSH identity file it will use before
  connecting (`Using SSH key: ...`), resolved locally via `ssh -G` with no network I/O.
  `docs/SSH-SETUP.md` documents a Windows-specific gotcha where two different `ssh.exe`
  binaries commonly on `PATH` (Git's MSYS build and Windows' native OpenSSH client) can
  resolve default identity files differently, and recommends setting `HA_SSH_KEY` explicitly
  to remove the ambiguity. Deployment tooling and docs only — no change to the integration.

## [0.5.42] — 2026-08-01

- Fix #545: strengthened project guidance and automated checks to prevent a repeat of
  #543-style bugs (blocking file I/O called directly from async code, stalling Home
  Assistant's event loop). `claude.md`'s Thread-Safety Requirements section now explicitly
  covers blocking I/O, not just CPU-bound computation. Ruff's `ASYNC` lint category is now
  enabled (catches a blocking call written directly inline in an async function, going
  forward). `tests/test_executor_offload.py` gained a registry-driven check
  (`TestBlockingIOExecutorOffload`) that verifies known blocking sub-component methods are
  never called unwrapped from any async method in `coordinator.py` — the part that actually
  would have caught #543. No user-visible behavior change; contributor-facing tooling only.

## [0.5.41] — 2026-08-01

- Fix #543: `chart_log.py`'s `ChartStateLog.load()`/`save()` performed synchronous file I/O
  (tempfile write + `os.replace` + `os.chmod`, or a blocking `Path.read_text()`) directly on
  Home Assistant's event loop from three coordinator.py call sites — the initial load during
  coordinator startup, and two mid-run saves (30-min poll, and the event-driven
  hvac_action-transition write). All three now run via `hass.async_add_executor_job()`,
  matching the existing pattern already used for learning/state-persistence I/O.
- Fix #543: `manifest.json`'s `iot_class` corrected from `local_polling` to `cloud_polling` —
  Climate Advisor's AI features call the Anthropic cloud API (`anthropic>=0.49.0`), so
  `local_polling` was inaccurate. Both fixes were required by HACS's official
  default-repository review (hacs/default#8117).

## [0.5.40] — 2026-07-29

- Feat #540: new 'Nat-Vent Soft-Start (Purge Mode)' setting, on by default. The whole-house fan can now start moving air and purging attic/thermal-mass heat as soon as outdoor temperature reaches parity with indoor in the evening, once the day is confirmed past its peak — instead of waiting for outdoor to be measurably cooler. Disable it in settings if you prefer the old strict-delta-only behavior. See the Status card for a distinct 'soft-start (purge)' label while it's active.

## [0.5.39] — 2026-07-29

- Fix #538: the 'Next User Action' card said 'Free cooling is active.' while nat-vent or economizer cooling was already running — just repeating what the Status card already showed instead of telling you what to do. It now shows '-' when there's nothing for you to do.

## [0.5.38] — 2026-07-28

- Fix #534: the Next Automation card's 'outdoor no longer helping' message could read as a claim about right now even though it was always a forecast for a specific future time — the action text now says when that's expected to happen (e.g. 'Outdoor will stop helping around 9:00 AM — close windows') instead of only showing the time in a separate card. Also: mild-day briefings now use the same weather-forecast-based window close time warm days already got, instead of always showing a fixed 5:00 PM regardless of actual conditions.

## [0.5.37] — 2026-07-27

- Fix #530: turning off the whole-house fan didn't reliably stick — a watchdog meant to catch a completely different, rare bug was mistaking the normal 'no override in progress' state of an ordinary fan-off for a stuck automation, and killing its protection within about a second almost every time. Fixed at the root, so fan-off now stays off for its full protection window like it's always been supposed to. On top of that, an overnight session started via an 8-hour RF remote timer no longer produces a burst of contradictory decisions right when the timer runs out — a fan-off report in the couple of minutes after that timer's own grace period ends is now recognized as the tail of the same session instead of a brand-new event. Separately, a leftover fan override being cleared at the 6:30 AM wake-up could arm the AC with windows still open — wake-up no longer releases whole-house-fan HVAC suppression while a nat-vent session is still active.

## [0.5.36] — 2026-07-27

- Fix #528: on warm/mild days, the briefing's window-close and reopen times could be badly wrong — one real example told the user to close windows at 8 AM (outdoor was still cooler for hours after that) and reopen at 2 PM, before the day's actual heat peak, both computed from a data-alignment bug that's now fixed. Feat #528: the Next Automation card can now predict the whole-house fan/nat-vent starting (using the same real activation logic the automation itself uses), the warm-day window-close/AC-on/reopen events, and hot-day morning/evening window- cooling opportunities — previously only the daily briefing knew about any of this.

## [0.5.35] — 2026-07-26

- Fix #527: the dashboard's Status, Next User Action, and Next Automation cards could all say the same thing in different words whenever a door/window was open (or a grace period or thermostat-change confirmation was active) — the Next User Action card said 'Automation paused,' restating the Status card instead of telling you what to actually do (like closing the window), and the Next Automation card said 'Waiting' instead of showing the real next step (like tonight's bedtime setback) and when it'll happen. Each card now sticks to its own job, and away/vacation mode gets a bit of rotating personality in Next User Action instead of one flat line.

## [0.5.34] — 2026-07-25

- Fix #523: after an HA restart, if a window was already open, Climate Advisor could turn the AC on and cool against the open window instead of staying paused like it does at every other point in the day — most visible after an update. Startup handling now defers to the same door/window pause logic used the rest of the time, and that pause logic itself now correctly stays engaged even when the thermostat was already off when the window opened.

## [0.5.33] — 2026-07-25

- Fix #524: the dashboard's whole-house-fan status card never showed the QuietCool remote's reported speed, even though the underlying detection (#519) was working correctly — the value was computed but never reached the dashboard. It now shows promptly after any remote press. Also, the Activity Report's fan-override entries now note when a remote speed or timer selection armed the override, instead of looking identical to a generic detected toggle.

## [0.5.32] — 2026-07-25

- Fix #518: the warm/windows-day briefing could contradict itself — the header's window-close time didn't match the body's, an AC-start message ignored whether windows were actually open (and contradicted a correct warning elsewhere in the same briefing), a 'reopen windows' message could cancel an AC run that never started, and a bedtime-setback note could appear even when the header said 'No setback'. All four are now derived from a single computation so the header and body always agree, and the AC-safety-net wording is stated once, tied to windows being closed. Also dropped redundant 'no action needed' phrasing from the briefing and dashboard status text.
<!-- 0.5.31 folded in: exact commit ambiguous, see PR #543 discussion -->
- Feat #519: Climate Advisor now detects and respects QuietCool remote speed changes (low/medium/high), not just timer presses. If you adjust speed while the fan was already running, that's treated as a comfort preference — it's just recorded, not treated as taking manual control (no grace period or HVAC suppression armed). If you select a speed while the fan was off, or select a timer (with or without a speed), that's still treated as an override exactly like before. If your remote's firmware has been updated to the latest gunkl/quietcool-house-fan, the dashboard also shows the fan's current remote-reported speed. Fully auto-detected — no new setting to configure, and installs without the firmware update behave exactly as they do today.

## [0.5.30] — 2026-07-25

- Fix #510: the dashboard WHF status card could show 'nat-vent active, fan idle' for hours while the whole-house fan was genuinely, physically running — confirmed via live logs on an install with dedicated fan power detection, where a stale nat-vent session flag masked ground truth that was available the whole time. The display now refreshes immediately on every real physical fan transition (previously only when a manual override was already active) and always trusts confirmed physical state over CA's own internal session flags when it's available. The related 'active (unconfirmed)' status — which could also persist indefinitely once stale (observed 138 times over 24+ hours in the same incident) — now correctly settles to 'inactive' once enough time has passed for ground truth to be trusted, rather than leading with 'active' forever. Also fixes two related automation-bookkeeping gaps found during the same investigation: a whole-house-fan install's post-grace-period check was silently skipped because it consulted the thermostat's own fan attributes instead of the real fan entity, and the existing periodic untracked-fan reconciliation now also covers a stale nat-vent flag, not just a fully-untracked fan, closing the loop within ~30 minutes if it recurs.

## [0.5.29] — 2026-07-24

- Fix #511: for installs with no dedicated outdoor sensor (weather-service source only, e.g. Met.no), the dashboard's 'Actual Outdoor' reading and the automation decisions based on it could lag or lead true conditions by up to an hour during a temperature ramp — the weather integration's live reading only refreshes roughly hourly, so it was really a stale point-sample, not a live value. Outdoor temp is now estimated by interpolating between the two nearest hourly forecast points, refreshed every 5 minutes, feeding the dashboard, the windows-recommended flag, nat-vent/economizer gating, and thermal-learning model accuracy. Installs with a dedicated sensor or input_number are unaffected — they already had a true live reading.

## [0.5.28] — 2026-07-22

- Fix #508: pressing 'Cancel Fan Override' on the dashboard cleared the fan override but left the grace-period countdown running for its full original duration — up to 8 hours for a QuietCool RF remote timer — so the dashboard kept saying 'Grace period active' long after you'd already cancelled it. The fan/HVAC state also had no guaranteed way to re-sync immediately; it only worked today because an unrelated door/window event happened to fire a minute later. Both dashboard 'Cancel...' buttons now share one cancellation path that clears grace, re-checks the fan, and logs the cancellation to the Activity Report every time. A background safety check also self-heals any grace period left with no override behind it.

## [0.5.27] — 2026-07-20

- Fix #505: vacation mode's deep energy-saving setback was armed once when you turned vacation mode on, and never enforced again for the rest of the trip — confirmed against real logs from a 5-day vacation where the home ran at normal comfort temperature almost the entire time. A temporary override (e.g. for cleaners) that was later cancelled left the thermostat at the override's setpoint indefinitely instead of returning to the deep setback, the same way away mode already correctly does. Away mode itself, home mode, and guest mode were not affected. Also fixed the same gap for the bedtime and pre-cool triggers, which had the identical assumption.

## [0.5.26] — 2026-07-20

- Fix #504: a monitored door/window sensor bouncing open/closed rapidly (flaky contact hardware, or a quick open-close-open) could snap the whole-house fan on and back off within the same minute — an audible burst with no settle time, even though a sensor debounce period was configured. The debounce now also governs when free-cooling fan control reacts to a sensor change, not just HVAC pause/resume, so a bounce no longer instantly re-triggers the fan. The default debounce is also now 10 minutes (was 5) for new installs. Also fixed: the Activity Report row for nat-vent ending because a sensor closed now shows the fan's on->off transition, matching every other fan-transition row.

## [0.5.25] — 2026-07-13

- Fix #485: the Activity Report showed the same "Occupancy setback (away)" entry repeated every ~5 minutes for hours at a time while nobody was home, drowning out everything else in the log. The setpoint itself was never actually changing — Climate Advisor just wasn't collapsing the repeats the way it already does for other frequently-repeated entries. Now it shows one entry with a repeat count and time range, and still shows a new entry right away whenever something real changes (you come home, leave for vacation, etc.).

## [0.5.24] — 2026-07-13

- Fix #498: the Status dashboard showed "Grace period active" during an override but never said when it would end — now shows the end time and minutes remaining. Also fixed: the 6:30am wake-up could turn off a manually-overridden whole-house fan and arm the AC against open windows, self-correcting only by luck a cycle later. Bedtime, wake-up, and the overnight pre-cool trigger no longer each decide independently whether to touch the fan or arm HVAC — they now share one gate with the main 30-minute decision loop, closing two related gaps in the same pass: none of the three previously respected an open door/window pause, and bedtime's own free-cooling continuation check could hand off to the compressor prematurely even while the fan was still doing useful, cheaper work.

## [0.5.23] — 2026-07-12

- Fix #495: manually or remotely turning on the whole-house fan (WHF) — by hand, or via a QuietCool RF remote timer press — left the AC armed for the entire session, fighting the fan and wasting energy while windows were open. Only Climate Advisor's own fan activation suppressed HVAC; a user-initiated fan-on did not. Fixed: both paths now share one HVAC-suppression helper, and ending a manual session reclassifies (rather than blindly restoring a potentially hours-stale captured mode — an RF-remote-timer session can run up to 12 hours). Also fixes two QuietCool remote bugs found while investigating: (1) the remote's status entity can flap unavailable and re-announce a stale timer selection with no user action, which was previously processed as a fresh press — confirmed live as a phantom 2-hour override with zero button presses; (2) the dashboard's remote-timer display could go blank within seconds of a real press. And: the dashboard could show two contradicting status lines at once when a fan override and a pending thermostat-override confirmation overlapped — now reconciled.

## [0.5.22] — 2026-07-12

- Fix #493: found while verifying #491's restart fix on a real HA restart — learning.save_state() could occasionally log 'Failed to save learning state: No such file or directory' when two saves happened to run at the same moment (common at restart). It wrote to a shared, fixed staging filename, so one save could find the file already consumed by another. Non-fatal (the error was already caught and logged; nothing was corrupted), but one save's data could be silently skipped for that cycle. Each save now stages to its own uniquely-named temp file, the same pattern already used by CA's other state file — eliminating the collision entirely.

## [0.5.21] — 2026-07-12

- Fix #491: two restart-time bugs found immediately after the 0.5.20 deploy, both pre-existing and unrelated to #489. (1) The dashboard could show a false 'Fan manual override' and a bogus multi-hour manual grace period right after every HA restart — the whole-house fan never turned on and nobody touched the remote; the QuietCool RF remote's device entity can re-announce its last retained state while HA is still settling after restart, and neither fan listener had the same 5-minute startup-suppression guard the thermostat listener already had (Issue #321). Both fan listeners now share that guard. (2) A 'Climate Advisor unavailable' error banner could appear after routine restarts/deploys with nothing actually wrong — a plumbing bug in the thermal observation pipeline (present since April) crashed the coordinator update whenever a pending thermal observation was abandoned right as HVAC started, which is common at restart. Fixed; no HVAC or automation timing behavior changed by either fix.

## [0.5.20] — 2026-07-12

- Fix #489: the Doors/Windows status card could show a stale 'N open' reading for up to 30 minutes after a monitored door or window was actually closed again. Brief real door use (a few seconds) was always detected correctly, but closing it back up didn't force the dashboard to refresh — only opening did. Now every sensor transition, open or closed, refreshes the status display immediately. Automation timing is unaffected: the existing debounce still exclusively governs when HVAC actually pauses or resumes for a door/window event.

## [0.5.19] — 2026-07-12

- Feat #486: Climate Advisor can now hear the QuietCool whole-house fan's physical RF wall remote (via the gunkl/quietcool-house-fan ESPHome firmware's event entity) and honor a timer selection made at the remote. Previously, pressing '8 hours' on the remote had no effect on CA's own automatic fan-off timing — CA would still shut the fan off on its usual ~30-90 minute grace period, contradicting what the person just told the fan to do. Now, when an optional Fan RF Remote Event Entity is configured, a 1/2/4/8/12-hour remote timer selection sets the duration of CA's fan manual-override grace period, so CA backs off for exactly as long as the user asked. Fully optional and non-breaking: leave the field blank and nothing changes. See docs/fan-remote-spec.md for the firmware event contract and mapping.

## [0.5.18] — 2026-07-12

- Fix #434: optional entity settings can now actually be cleared. Previously, if you'd set a Home/Away toggle, Vacation toggle, Guest toggle, fan entity, fan-state entity, or a custom outdoor/indoor temperature-source entity and later wanted to stop using it, clearing the picker and hitting Save & Close did nothing — Climate Advisor kept reacting to the old entity even though the UI says 'leave blank if you don't use that feature'. The options flow now removes a field you've emptied, so leaving it blank truly unsets it (occupancy falls back to Home; vacation/guest default to off).

## [0.5.17] — 2026-07-11

- Fix #480: when Climate Advisor's coordinator update fails (the failure that took every climate_advisor_* entity unavailable simultaneously during the Issue #478 incident), the dashboard used to keep confidently showing the last-known automation/fan status with zero indication anything was wrong — you'd have no way to know CA had silently stopped working until you noticed the numbers looked stale. The Status card now shows ⚠ Climate Advisor unavailable since HH:MM — <error> the moment an update fails, and the underlying error/failure-count record is now written to disk, so it survives an HA restart and is still readable even after HA's own log retention has rotated past the event — the exact gap that made the original incident's root cause unrecoverable.
- Fix #481: fixes a false-positive comfort log entry that could make it look like the house was too cold overnight when it wasn't. The incident-detection subsystem that powers compliance/history review was comparing live indoor temperature against the flat daytime comfort band (e.g. 68°F) even during the overnight sleep window, where a lower sleep-band floor (e.g. 64°F) is the real, actively-applied target — so indoor temps that were genuinely comfortable within the sleep band (e.g. 66°F) could still log a 'comfort_undertemp' incident. Incident detection now resolves the same currently-active band (sleep/away/vacation-aware) that the dashboard's target-heat/cool fields and every setpoint-writing automation handler already use, so the incident log only reflects violations the occupant actually experienced.
- Fix #482: no user-visible change (latent-risk hardening). Closes two real gaps found during Issue #478's investigation in the fan-off manual-vs-automation classification path. (1) The fan physical-state drift-reconciliation self-correction (_reconcile_fan_physical_drift()'s off-command) now stamps the same _fan_command_pending/_fan_command_time bookkeeping every other WHF command site already sets, matching the existing pattern, so coordinator._async_fan_entity_changed() can suppress the resulting state-changed event as CA-caused instead of risking a misclassification as a manual fan-off (which would start a spurious grace period and temporarily block automated free cooling/HVAC control). (2) Every outgoing WHF fan/switch service call now carries a real HA Context (automation.py's new _call_fan_service_with_context()), and coordinator._async_fan_entity_changed() checks event.context.id/parent_id against it as an additional, authoritative CA-attribution signal alongside the existing _fan_command_pending/30-second timing heuristic (kept as-is, not replaced — context propagation through third-party fan/switch integrations, especially a one-way RF transmitter with no feedback of its own, is not guaranteed reliable by HA core). Every provenance decision (matched or not) is now logged at DEBUG so a future investigation has direct evidence instead of needing cross-source timestamp archaeology, and a genuinely external fan change's Context id is surfaced as diagnostic data in the Activity Report payload.
- Fix #483: if a manual thermostat override starts a grace period and Climate Advisor's own automation decision independently converges on the same HVAC mode (and, for heat/cool modes, the same effective setpoint) the override already produced, the override is now adopted instead of silently sitting out the rest of the grace window. Checked both pre-expiry (inside apply_classification(), so convergence is recognized as soon as the next classification cycle agrees — not just at the timer's natural expiry) and at natural grace expiry (skips the misleading 'your override has expired' notification when nothing was actually reverted). Deliberately conservative: only HVAC-mode overrides are eligible; setpoint-only overrides and fan/door-window grace types are unchanged (see KNOWN_FIXES[483] for the full scope boundary). New Activity Report event 'override_adopted'.

## [0.5.16] — 2026-07-11

- Fix #476: no user-visible change. Migrates all 10 remaining coordinator-dependent test scenarios (grace-period lifecycle, override detection/confirmation/self-resolve, bedtime+override interaction, cancel-override, restart behavior) to the coordinator-level Tier A harness built in #474 — closing out the full scope of #472's original investigation. Found and fixed 3 more real harness bugs along the way: a scheduler ordering bug where a coordinator listener's own state dispatch didn't settle before the next scenario event (silently misattributing timestamps to unrelated timers), an unpatched dt_util.parse_datetime() returning a MagicMock and crashing thermal-observation code, and async_track_time_change/interval callbacks (briefing/wakeup/bedtime) being constructed as coroutines but never awaited. Also fixed engine._sensor_check_callback being clobbered by an engine-only stub even in coordinator mode, breaking grace-expiry re-pause detection. Every migrated scenario was verified load-bearing via a real revert test (temporarily disabling the specific guard it protects, confirming failure, then restoring) — test-infrastructure only, no changes to coordinator.py/automation.py.

## [0.5.15] — 2026-07-10

- Fix #474: no user-visible change. Adds coordinator-level Tier A test harness coverage — a real ClimateAdvisorCoordinator can now be constructed headlessly over dispatching FakeHass/FakeScheduler fakes (real state-change events, real timers), closing a gap where 12 scenarios covering override detection, away-setback correctness, and grace-period behavior had no automated regression guard. Also deletes an 18-line hand-approximation of the coordinator's real override-detection state machine that had already drifted stale (test-infrastructure only — tools/sim_harness/, tools/simulate.py; no changes to the integration itself).

## [0.5.14] — 2026-07-10

- Fix #470: the chart's predicted-indoor curve could disagree with its own displayed target band overnight on nights where an adaptive sleep setpoint applied and sleep_heat/sleep_cool were left at their defaults (not explicitly configured) — the prediction curve silently used a flat default sleep floor while the band shown alongside it used the thermal-model-adjusted one. Also completes Phase B (coordinator single- source): the chart's target-band schedule is now computed once per request instead of twice.

## [0.5.13] — 2026-07-10

- Fix #468: the AI Activity Report and Investigator's thermal-model sections could show an empty learning-health summary and a blank thermal equilibrium temperature even when the dashboard's Comfort Score sensor showed real rejection/observation data for the same moment — three AI-context call sites queried the thermal model without the per-observation-type health data the dashboard already includes, producing a structurally incomplete result for no reason. One of the three had already computed that exact data a few lines above for its own display and simply never passed it along. Now all three match what the dashboard sees.

## [0.5.12] — 2026-07-10

- Fix #466: no user-visible change. Continues Phase B (coordinator single- source): added target_temp/target_temp_low/target_temp_high to coordinator.data so ai_skills_activity.py and ai_skills_context.py stop independently re-fetching the thermostat entity to derive the same values. api.py's dashboard status endpoint deliberately keeps its own live read — it powers the ca_target_heat/cool divergence check (#402/ #462), whose entire purpose is comparing CA's computed target against the real thermostat right now, not a snapshot that can be up to 30 min old.

## [0.5.11] — 2026-07-10

- Fix #464: no user-visible change. Starts Phase B of the architecture- consolidation direction (coordinator single-source) by adding coordinator.get_hvac_runtime_today() as the one place today's live HVAC runtime is computed, replacing an identical formula that was copy-pasted byte-for-byte in coordinator.py, ai_skills_context.py, and ai_skills_activity.py. No drift had occurred yet, but any future change to the formula (e.g. excluding paused/away time) would have needed to be applied in 3 places to avoid the AI Activity Report and Investigator silently diverging from the dashboard.

## [0.5.10] — 2026-07-10

- Fix #462: the dashboard's setpoint-divergence indicator (ca_target_heat/cool) could show the wrong intended target while the home was in away or vacation mode — it never accounted for occupancy at all, so it displayed the comfort or sleep band even though the thermostat was actually being held at the (wider) setback band. Routed through the same select_comfort_band() function every real setpoint-writing code path already uses, so this indicator can no longer silently drift from what the thermostat is actually doing. Also corrected the fallback used when sleep_heat/sleep_cool aren't explicitly configured, from the flat daytime comfort temps to the documented sleep defaults (64/72°F), matching what the thermostat is actually set to overnight in that configuration.

## [0.5.9] — 2026-07-10

- Fix #460: no user-visible change (confirmed via unit tests and a positive control). Consolidated the 'should this comfort/setback code path defer because occupancy is away/vacation' gate — previously phrased 3 different (but logically equivalent) ways across automation.py's setpoint paths (_set_temperature_for_mode, handle_bedtime, handle_pre_cool, handle_morning_wakeup) — into a single should_defer_to_occupancy_setback() function. No drift had occurred yet, but the risk was live: a future change to which occupancy modes should defer could easily be applied to 3 of the 4 sites and miss the 4th, the same class of bug already found once in #458.

## [0.5.8] — 2026-07-10

- Fix #458: the AI Activity Report could misreport the whole-house fan as a contradiction ('hvac_mode=off but hvac_action=fan') during the brief window where CA detects and self-corrects a stale WHF on/off flag (Issue #423's 'active (unconfirmed)' state) — that specific fan state was missing from this report's allow-list of expected fan activity, even though the dashboard status card already handled it correctly. Consolidated the two independently-written checks (coordinator.py, ai_skills_activity.py) onto one shared predicate so this class of drift can't recur; also fixed a second latent gap the consolidation surfaced: a confirmed-running manual fan override wasn't suppressing the coordinator's own internal contradiction-warning event either.

## [0.5.7] — 2026-07-10

- Fix #456: no user-visible change (confirmed via differential testing and a positive control). Consolidated the nat-vent 'hard exit floor' formula — the sleep-aware threshold below which an active free-cooling session ends outright — from 3 independent implementations down to 1. Two automation.py call sites (check_natural_vent_conditions, nat_vent_temperature_check) previously recomputed this formula inline instead of using the already-pure, already-tested fan_thermostat_decision.py version — the same 'sibling function silently drifts' bug class behind issues #400/#402/#417. No drift had occurred yet here, but the risk was live: a future fix to one copy could easily miss the other two.

## [0.5.6] — 2026-07-10

- Fix #454: no user-visible change. Extracted the shared shape behind the nat-vent gate's old-vs-new differential comparator (shadow-mode instrumentation, the Call/ComparisonRun result shape, substitution mode) into a reusable base so each upcoming pure decide_*() extraction gets a comparator by supplying only which production method and pure function to wire together, instead of a new copy-pasted comparator file. A first cut of the refactor introduced an import-order bug that broke the CLI comparator tool (resolving the production class before the module that installs test HA stubs) — caught by running the tool directly, not just the test suite, and fixed before merge.

## [0.5.5] — 2026-07-10

- Fix #452: no user-visible change. Continues the nat-vent architecture-reset direction (v0.5.1) into the test suite — 14 test helpers that hand-copied production logic (API view dispatch, sensor attributes, coordinator status strings) because HomeAssistantView couldn't be instantiated in tests now exercise the real classes directly. Along the way this caught and fixed a stale test assertion that had silently drifted from production: the bedtime status line's expected setpoint used an old comfort-temp-plus-delta formula that stopped matching the real sleep_heat/sleep_cool config keys, so the old test was passing against logic that no longer runs.

## [0.5.4] — 2026-07-10

- Fix #449: found the real reason a whole-house fan could stay off for hours overnight after being turned off outside of Climate Advisor (e.g. a wall switch or the device's own remote) — in dual-entity setups (a control switch plus a separate power-detection sensor), the control entity's Home Assistant state can silently keep saying 'on' even though the fan is truly off, since it's a one-way command with no feedback of its own. A plain 'turn on' command sent to an entity Home Assistant already believes is on can be silently dropped before it ever reaches the device. Climate Advisor now checks the power-detection sensor before every command: if the control entity and the sensor already agree, nothing is touched; if they disagree, it forces a real transition (off, briefly, then on — or the reverse) so the command actually reaches the fan. Confirmed against real device history from an actual overnight incident. Only affects dual-entity whole-house-fan setups — single-entity setups and HVAC-fan-mode ventilation are unchanged.

## [0.5.3] — 2026-07-10

- Fix #446: an automated self-correction (Issue #423's fan physical-drift check fixing its own stale belief about whether the fan was on) was reported in the Activity Report as 'Grace period started (manual)' — telling you that you turned the fan off when nobody did. It's now correctly labeled as an automation-triggered grace period.
- Fix #446: after a restart, if a fan kept appearing as 'running without CA warrant' (e.g. a thermostat's own circulation schedule CA can't durably override with a single command), CA re-issued the same correction attempt every few minutes for up to 45 minutes. It now waits 5 minutes between correction attempts for the same condition, while still keeping a persistently-stray fan visible in the logs.

## [0.5.2] — 2026-07-10

- Fix #444: the Activity Report could show the same 'Comfort band applied' line 2-3 times in a row for the exact same setpoint — most visibly right after an HA restart, when the startup sequence and the regular classification cycle both independently re-announced the identical band within the same minute. The underlying thermostat command was always correct; only the notification was duplicated. A short-window dedup now suppresses a redundant announcement of an unchanged band, without ever skipping the actual setpoint command.

## [0.5.1] — 2026-07-10

- Fix #439: the initial setup wizard could write stale sleep-temperature defaults into a brand-new install — Fahrenheit sleep fields, and all six Celsius setpoints, were hardcoded and never picked up the household-matched defaults shipped in 0.5.0. Every unit now derives its default directly from the same shared constants, so new installs get the intended values.
- Fix #440: on a warming-trend night, if natural ventilation ended earlier than its originally scheduled close time — for any reason, including the window simply being closed — the overnight pre-cool AC trigger stayed on the old schedule instead of stepping in right away. It now reacts to nat-vent actually ending and moves the AC trigger earlier when that saves time, never later.
- Feat #438: the default comfort/setback/sleep temperatures shipped for fresh installs (and any config relying on an unconfigured fallback) now match a real, tuned household configuration instead of arbitrary round numbers — comfort 68°F/74°F, setback 63°F/79°F, and a flat sleep target of 64°F/72°F that's cooler than daytime comfort, not warmer. Fixed 3 latent bugs found along the way where a hardcoded fallback had silently drifted from the value it was supposed to mirror (a setpoint-inconsistency check, the chart's fan-activity prediction, and the away/vacation display in the daily briefing).
- Fix #437: on a warming-trend night, the overnight pre-cool phase (which lowers the AC ceiling to bank cold thermal mass before the next hot day) could silently become a no-op — it computed a target but immediately clamped it back up near daytime comfort, so no extra cooling ever happened even though the system reported pre-cool as active. The clamp now anchors to the sleep temperature range instead of the daytime one, so pre-cool can use its full intended range. This also closes #436: the chart's target-band display and the real overnight setpoint can no longer show different pre-cool numbers, since both now compute the target the same single way.
- Fix #435: if you run natural ventilation with no whole-house fan or HVAC-fan device configured (relying on manually-opened windows instead), the activity report could show a confusing 'Nat-vent fan on/off' entry claiming device "none" turned on or off — even though nothing happened, since there's no fan to control in that setup. The cycling check now only reports a fan transition when one actually occurred.

## [0.4.74] — 2026-07-08

- Fix #427: overnight whole-house-fan nat-vent sessions were being torn down and re-adopted every 5-15 minutes for hours, showing repeated 'fan running (untracked)' and 'startup reconcile' notifications even though the window never closed. The proactive floor-exit check (which predicts an imminent floor crossing from the thermal model) was comparing indoor temperature against the flat daytime comfort floor instead of the lower overnight sleep floor, so during the sleep window it believed the floor was already breached hours before it actually was and kept ending the session for no reason. It now uses the same sleep-aware floor as every other nat-vent exit/reactivation check, so sessions persist correctly through the night and the fan only cycles the way it's supposed to.

## [0.4.73] — 2026-07-08

- Fix #428: 'Your Next Action' could tell you to open a window or turn on a fan to cool down even when it was hotter outside than inside — advice that would have made things worse. It now checks live outdoor temperature (the same free-cooling direction guard already used by the economizer/nat-vent logic) before ever suggesting a window or fan, covers the mirrored heating-direction case, and won't repeat advice that's redundant with what you've already done or what automation is already doing.

## [0.4.72] — 2026-07-06

- Fix #424: fan mode 'Both' (whole house fan + HVAC fan simultaneously) is no longer selectable during setup or in options — a proper per-device redesign for two independently-tracked physical fans was judged too risky to build on top of the already-fragile fan-reconcile logic (site of the recent #423 incident), so the option is removed instead. Existing installs configured with 'Both' are automatically migrated to 'Whole house fan' the next time the config entry loads.

## [0.4.71] — 2026-07-06

- Fix #423: a whole-house fan could get stuck showing 'active (unconfirmed)' for hours after physically turning off, with nat-vent never resuming even though conditions clearly favored free cooling. Root cause: the fan-reconcile logic that runs after a thermostat-internal fan blip always trusted the thermostat's own fan attributes as "the fan is running" — correct for a furnace/AC blower, but wrong for a physically separate whole-house fan switch, which could get silently "adopted" as running when it was actually off. It now checks the real configured fan's own reported state for whole-house-fan setups. Also added a background check that self-corrects a stuck fan-status flag within about 10 minutes if it ever disagrees with the real device, instead of only showing 'unconfirmed' in the UI.

## [0.4.70] — 2026-07-05

- Fix #418: two remaining nat-vent exit paths (closing the last open window, and the fast free-cooling-reversal check that runs on every temperature update) now go through the same unified exit handling the other paths already used. The fast-loop path had a real bug — it could mark the session as 'paused, waiting for the window to close' while still turning the HVAC back on into that open window. Closing the last window now restores HVAC and lets it settle into the right mode within a few minutes (previously instant) — a deliberate tradeoff for consistency.

## [0.4.69] — 2026-07-05

- Fix #420: AI Investigation reports now flag when a report was cut off before Claude finished writing it (hit the configured max response length), instead of silently showing an incomplete report as if it were 'Completed'. The dashboard now shows a clear truncation warning and a log WARNING is emitted so you know to raise 'Investigator Max Response Length' in AI settings and re-run.

## [0.4.68] — 2026-07-05

- Fix #417: overnight nat-vent no longer flickers between 'nat-vent' and 'paused — door/window open' every few minutes while the window stays open the whole time. The reactivation gate that decides whether nat-vent can resume was using the flat daytime comfort floor even during the sleep window, so indoor temperatures that were perfectly fine relative to the (lower) sleep floor kept reading as 'too cold' and repeatedly shutting the session down. It now uses the same sleep-aware floor the fan-cycling logic already used.

## [0.4.67] — 2026-07-04

- Fix #415: the Status card no longer shows a stale nat-vent target temperature (e.g. 'nat-vent (target 71°F)') that could disagree with the correct cycling band shown right below it (e.g. '64°F–66°F'). The status string is cached for up to 30 minutes while the cycling band is recomputed live on every dashboard load, so the two could drift apart across a sleep-window transition. The status string now just says 'nat-vent' — the live cycling band is the only place the temperature is shown.

## [0.4.66] — 2026-07-04

- Fix #413: restart-cause diagnostics (added in #403) now correctly classify real HA restarts and deploys as 'version_changed' or 'user_restart' instead of always showing 'unknown'. The persistence step was wired to async_shutdown(), which only runs on config-entry unload/reload — not on a normal Home Assistant restart. A new EVENT_HOMEASSISTANT_STOP listener now persists the same shutdown diagnostics on the restart path that actually happens in practice.

## [0.4.65] — 2026-07-04

- Fix #411: nat-vent floor-exit decisions and false comfort-violation alarms during correct WHF cycling are now consistent; a stuck thermostat setpoint disagreement self-corrects instead of retrying forever.

## [0.4.64] — 2026-07-03

- Fix #409: streamlined the Status card's nat-vent display — removed the duplicate target temperature (previously shown twice), removed the redundant 'Natural ventilation'/'nat-vent' double-naming, and dropped the unverified 'windows open' prefix (nat-vent can be active without any window physically open; real window state is already shown by the dedicated Doors/Windows card).

## [0.4.63] — 2026-07-03

- Fix #407 follow-up: removed the standalone 'Natural Vent' dashboard card — its cycling-band and AC-assist info is now shown as a supplemental line on the main Status card instead of a separate card, per the project's 'no new cards, extend existing ones' dashboard convention.
- Fix #407: the dashboard Status card no longer shows a stale daytime nat-vent target (e.g. 71°F) overnight during the sleep window — it now matches the Natural Vent card's correct sleep-window target (e.g. 65°F).

## [0.4.61] — 2026-07-03

- Fix #405: HVAC writes no longer stay permanently blocked after a whole-house-fan nat-vent session ends with the fan already off at a restart/coalesce boundary. reconcile_fan_on_startup()'s 'no-fan' decision now releases any stranded HVAC suppression flag (_pre_fan_hvac_mode) the same way a normal fan deactivation does, instead of only clearing the fan-tracking flags — previously the home could be left with no automated cooling response for the rest of the day.

## [0.4.60] — 2026-07-03

- Fix #402: whole-house-fan nat-vent could silently stop controlling the home for hours overnight instead of cycling through the sleep window. Two causes: (1) `fan_thermostat_check()` — the tick-level safety check that runs far more often than the 30-minute classification cycle — still used the flat daytime `comfort_heat` floor even during the sleep window, so it always ended the nat-vent session prematurely before the correct sleep-window cycling (fixed in #374) ever got a chance to run. (2) Once that premature exit fired, `apply_classification()` legitimately arms `cool` mode as a compressor backstop — but that permanently blocked the fan's own re-activation check, which required the thermostat's armed mode to be literally "off" even though the compressor was never actually running. Both fixed: the tick-level floor check is now sleep-aware, and re-activation now checks whether the compressor is actively calling instead of the armed mode string.
- Fix #402: root-caused and fixed a related ceiling-guard/nat-vent oscillation — whole-house-fan archetypes have no compressor-assist model by design, but the ODE ceiling guard's escalation branch didn't check that before arming AC, causing repeated escalate/reactivate bursts with redundant thermostat writes whenever nat-vent briefly paused.
- Fix #402: bedtime setback tracking — a manual-override skip now correctly records the reason, and (a deeper related bug found while fixing it) days classified `hvac_mode="off"` — the majority case in mild climates — now correctly record that the sleep-band setback was applied; previously neither "applied" nor "skipped" was ever recorded for those nights.
- Fix #402: nat-vent exit/assist events now all carry a `fan_device` field identifying which physical fan mechanism was involved; the single-setpoint dashboard card now shows a "(CA: X)" divergence annotation when the real thermostat setpoint diverges from CA's intended target, matching the indicator the dual-setpoint card already had.
- Fix #403: CA now logs its own version at startup and shutdown and classifies why it restarted — a routine version-change deploy, a user-initiated Home Assistant restart/stop, or an unexplained (crash-like) restart — and shows that cause on the restart boundary marker in the AI activity report, instead of leaving restarts unexplained.

## [0.4.59] — 2026-07-02

- Fix #400: nat-vent dashboard/status showed the daytime comfort-band target (e.g. 71°F) even during the overnight sleep window, after Issue #374 already fixed the fan's actual cycling target to follow sleep_heat + hysteresis (e.g. 66°F) overnight. The fan was behaving correctly, but coordinator.py's get_debug_state() independently recomputed the target with a hardcoded daytime-only formula, so the status page never reflected the #374 fix. The dashboard now mirrors the same sleep-vs-daytime logic used by the fan itself.

## [0.4.58] — 2026-07-02

- Fix #396: The status card could show "waiting for coalescing" indefinitely after an HA restart with no clue why. Live diagnostics confirmed the #392 decision lock was never the cause (nothing was holding it) — the real blocker is that the coalesce check only runs once weather data is available, and the weather entity can stay unavailable for a long time after restart. The status card now says "starting — waiting for weather data" in that specific case instead of the misleading generic "waiting for coalescing".

## [0.4.57] — 2026-07-02

- Fix #396: Added diagnostics to pinpoint a startup-coalescing regression introduced by #392's automation decision lock — after that fix, the status card could show "waiting for coalescing" indefinitely after a restart, with no way to tell what was stuck. The decision lock now tracks and logs which method holds it and for how long, with checkpoint logging through the coalesce call chain and a new `decision_lock_holder` / `decision_lock_held_seconds` status field. This is diagnostics only — the underlying hang itself is not yet confirmed fixed; the next occurrence will name the exact stuck step.

## [0.4.56] — 2026-07-02

- Fix #392: Whole-house fan (WHF) and AC could fight each other in a repeating off→cool→off→cool loop roughly every 5 minutes — the ODE ceiling guard applied the same "switch to AC once indoor crosses the ceiling" rule to both fan archetypes, but a WHF is mutually exclusive with AC and physically guaranteed to keep cooling the house as long as outdoor air is cooler than indoor, so the ceiling number never applied to it. The ceiling check is now archetype-aware, HVAC writes are structurally blocked while a WHF session owns the thermostat (previously only enforced by convention), fan activation/deactivation are now idempotent, and automation decisions are serialized so independently-triggered handlers can no longer race on shared state.
- Fix #392: Activity Log lines for fan events now show which fan (hvac_fan/whf/both) actually fired instead of a generic "fan" label.

## [0.4.55] — 2026-07-02

- Fix #390: Whole-house fan status could show "off (manual override)" for up to 30 minutes after the fan was actually confirmed running — the coordinator listener that detects the fan_state_entity confirming physical on/off silently dropped the event once a manual override was already active, so the displayed status only caught up at the next scheduled poll. Now a coordinator refresh is requested immediately so the status reflects reality within one cycle.

## [0.4.54] — 2026-07-02

- Fix #388: Climate Advisor was missing from the Integrations page in Settings → Devices & Services — v0.4.53 set manifest.json integration_type to 'helper', which Home Assistant's frontend excludes from the Integrations dashboard and routes to the Helpers tab instead. Corrected to 'service', the accurate HA taxonomy value for a full custom integration.

## [0.4.53] — 2026-07-02

- Feat #384: HACS compliance — integration_type field added to manifest, dynamic README version badge replaces hardcoded string, state file permissions hardened (0o600), HACS knowledge base added to docs.

## [0.4.52] — 2026-07-02

- Fix #382: AI investigator streaming now shows live text as the LLM responds — chunks are flushed to the browser immediately via aiohttp drain(). Previously all chunks buffered until EOF, so the user saw no progress until the full report arrived at once.

## [0.4.51] — 2026-07-02

- Fix #380: AI investigator streaming — 'Generating…' loading overlay now hides when the first chunk arrives so live text is visible. Button and spinner restore immediately on completion instead of waiting for TCP close.

## [0.4.50] — 2026-07-02

- Feat #376: Day-type classification thresholds (Hot/Warm/Mild/Cool) are now configurable in Settings → Day-Type Thresholds. Defaults remain 85/75/60/45°F so existing users see no change until they opt to adjust.
- Feat #376: Thresholds display in the user's chosen temperature unit (°F or °C) with slider inputs and ascending-order validation.
- Feat #376: Config entry migrated from version 15 → 16; existing installations receive the default threshold values automatically on upgrade.

## [0.4.49] — 2026-07-02

- Fix #376: ODE/OLS prediction math (_build_predicted_indoor_future) now runs in a thread-pool executor instead of directly on the HA event loop — eliminates periodic event-loop blocking on every coordinator refresh cycle and morning briefing.
- Fix #376: Chart data API endpoint (get_chart_data) also offloaded to executor — same ODE computation ran inline on every chart panel load.
- Fix #376: HACS compliance — official Anthropic SDK usage documented in ClaudeAPIClient docstring; bundled JS libraries (Chart.js, Hammer.js, chartjs-plugin-zoom) attributed with upstream URLs in index.html.

## [0.4.48] — 2026-07-02

- Feat #377: AI investigator context is now built from 11 independently-testable provider functions in a new ai_skills_context module — replaces the 773-line monolith with a thin orchestrator.
- Feat #377: Focus-aware provider selection — specifying a focus keyword (thermal, fan, nat-vent, etc.) skips irrelevant providers, reducing token usage ~40% on focused runs.
- Feat #377: KNOWN_FIXES injected into AI context are now version-scoped — only entries that are partially unfixed, just deployed, or not yet deployed are included, eliminating stale bug history from mature installations.
- Feat #377: GitHub issues are now cached (24h open, 30d closed) — no live API fetch on every investigation; stale cache returned on network error.
- Feat #377: AI investigator now streams — first content visible in ~3–5 seconds via SSE; structured sections rendered on completion. Non-streaming callers unchanged.

## [0.4.47] — 2026-07-02

- Feat #374: Nat-vent nighttime cycling now targets sleep_heat (the sleep floor) instead of stopping at sleep_cool. Fan cycles off at sleep_heat, back on at sleep_heat + 2×hysteresis, keeping the home just above the sleep floor without over-cooling.
- Feat #374: Fan events now carry a fan_device field (whf/hvac_fan/both) so logs and the activity report distinguish WHF from HVAC fan blower activity.
- Feat #374: Status card now shows separate Fan (WHF) and Fan (HVAC) rows. WHF status cross-checks physical state and warns when CA's internal flag disagrees with the device.

## [0.4.46] — 2026-07-01

- Feat #370: Nat-vent (WHF/HVAC fan) now continues past bedtime when outdoor air is below the sleep target — free cooling closes the gap before handing off to the compressor. Fan stops automatically when indoor reaches sleep_cool. Fixes stale _natural_vent_active flag after bedtime fan deactivation.

## [0.4.45] — 2026-07-01

- Fix #369: add diagnostic logging to nat-vent paused-by-door reactivation gate.

## [0.4.44] — 2026-07-01

- Feat #367: Status pane Conditions card combines day type badge, trend direction/magnitude, and current outdoor temperature into a single card. HVAC Mode card now shows indoor temperature inline. Standalone Day Type, Trend, and Indoor cards removed.

## [0.4.43] — 2026-07-01

- Fix #365: Fan status now correctly shows 'running (manual override)' when the user manually turns on a WHF and CA records it as an override (not adopted as nat-vent). Previously showed 'off (manual override)' even though the fan was physically running.

## [0.4.42] — 2026-07-01

- Fix #363: WHF fan status sensor now shows 'running (untracked)' when the whole-house fan is physically on but CA's flags are clear — reads fan_state_entity (Type 2) or fan_entity (Type 1) via _get_fan_physical_state().

## [0.4.41] — 2026-07-01

- Feat #361: Added fan_state_feedback config flag. When OFF (default), CA operates in command-only mode — asserting desired fan state idempotently without reading back entity state. Prevents false override detection from command-echo entities. When ON, enables physical state feedback for WHF installations with a dedicated state sensor.

## [0.4.40] — 2026-07-01

- Fix #359: Fan cancel now correctly re-asserts setpoint after ecobee comfort-program echo.
- Fix #359: Fan running untracked after grace expires now reconciled via post-grace callback and periodic backstop.
- Fix #359: User turning fan ON under nat-vent-eligible conditions now triggers nat-vent adoption (not override).
- Fix #359: AI activity investigator now tracks fan ownership across timeline, annotating nat-vent events when user controls the fan.
- Feat #359: Whole-house fan dual-entity support — optional separate state sensor (fan_state_entity) for Type 2 WHF installations.

## [0.4.39] — 2026-06-23

- Fix #354: Activity Record now shows indoor/outdoor temp at thermostat decision events.

## [0.4.38] — 2026-06-23

- Feat #352: Analysis tab — single dropdown card replaces three-section layout; report type selector (Activity Record / AI Activity Report / AI Investigative Analysis) with adaptive time window and controls. Download .md and Submit GitHub Issue available for all three types. Debug and Analysis tabs swapped in tab bar order.

## [0.4.37] — 2026-06-23

- Feat #352: Activity Record — new deterministic event timeline (no AI required) with indoor/outdoor temperature columns. Available on the Analysis tab with Copy, Download .md, and Submit GitHub Issue actions. AI Activity Report and AI Investigative Analysis now have their own dedicated sections with separate generate buttons; AI sections show a disabled notice when AI is not configured. Tab renamed from 'AI' to 'Analysis'.

## [0.4.36] — 2026-06-21

- Fix #347: Fan no longer stays running (untracked) indefinitely after thermostat starts it autonomously between AC cycles. CA now reconciles on every hvac_action transition to 'fan' — adopts as nat-vent if conditions allow, or turns it off.

## [0.4.35] — 2026-06-20

- Fix #345: Prediction Engines debug panel now shows correct confidence for k_solar (was always 'none' regardless of observation count) and k_active_hvac (confidence was previously absent from the panel entirely).

## [0.4.34] — 2026-06-20

- Fix #343: Prediction Engines debug panel now shows only confidence level per parameter — stale 'since' dates (which were frozen at first observation and never updated on EWMA changes) and redundant observation counts have been removed.

## [0.4.33] — 2026-06-20

- Fix #341: nat-vent active during sleep window no longer sets two conflicting thermostat setpoints every 30 minutes all night — one write per cycle (sleep band) instead of two.
- Fix #341: 'Grace started' activity report entry now shows what triggered it (e.g. 'fan override (manual fan change)') in the Settings column instead of a blank.
- Fix #341: fan manual override now emits its own timeline event showing the fan state change (e.g. 'fan: on->auto') so the reason for the 90-min grace period is visible without reading the Decisions section.

## [0.4.32] — 2026-06-19

- Fix #339: Occupancy→away/vacation no longer arms HVAC setback while windows/doors are open. HVAC stays off; occupancy mode is recorded for correct setback on resume. Status now shows 'paused — away (setback deferred: windows open)' when both conditions are active.

## [0.4.31] — 2026-06-19

- Fix #338: nat-vent + AC assist — band re-armed when nat-vent activates from pause; aggressive_savings gate prevents compressor through open windows; comfort band re-armed immediately when windows close on warm/mild days.

## [0.4.30] — 2026-06-19

- Fix #337: HVAC no longer runs with windows/doors open — apply_classification now enforces HVAC off whenever paused, on both hot and cold days.

## [0.4.29] — 2026-06-18

- Fix #335: Sleep setback was overridden every 30 minutes after bedtime on installations configured via the HA UI (time selector). The HA time selector stores times as 'HH:MM:SS' but _in_sleep_window() only handled 'HH:MM', causing a silent parse failure and falling back to the daytime comfort band on every 30-min cycle.

## [0.4.28] — 2026-06-17

- Fix #333: Bedtime 'Next Automation' label and chart sleep band now show the configured sleep temp (e.g. 73°F), not the trend-adjusted value. The warming-trend modifier was never applied to the thermostat at bedtime — only the mid-night pre-cool event uses it. Cool + cooling-trend and heat + warming-trend users no longer see a phantom ±2°F offset.

## [0.4.27] — 2026-06-17

- Fan activity now appears in the Activity Report with its trigger source. CA-commanded fan changes (min-runtime, economizer, whole-house, reconcile, thermostatic, nat-vent) emit fan_activated/fan_deactivated, and the thermostat's own blower running uncommanded (e.g. between AC cooling cycles) now logs a deduped 'Fan running (untracked)' event with the inferred source — so fan activity is no longer invisible in the report.

## [0.4.26] — 2026-06-17

- Chart Vent bar: the forecast (right of 'Now') now renders green-only (ventilation armed/planned) — blue is reserved for live/historical fan that is physically running, removing the confusing green→blue flip at 'Now'. Removed the two Vent legend keys.

## [0.4.25] — 2026-06-17

- Fix #330: The Activity Report's per-event table is now built deterministically in Python (no longer LLM-generated). The Settings column is always populated on band/setback rows (e.g. 'setpoint: 72°F Cool (64°F Heat)') and on deduplicated ×N rows — ending the recurring empty-Settings defect. A renderer registry covers every event type, with a safe default for any new type and a coverage test that flags unhandled events.
- Fix #331: The chart's Fan and Win Rec bars are merged into one Vent bar (blue = fan physically running, green = nat-vent armed or windows recommended); the HVAC bar now shows compressor-only states (heating/cooling). Fixes the fan appearing ON while thermostatically off.

## [0.4.24] — 2026-06-17

- Fix #327: The HVAC/whole-house fan can no longer run indefinitely. A thermostatic fast loop now re-checks on every indoor OR outdoor temperature change and stops the fan the moment outdoor ≥ indoor (free cooling gone) or the home has cooled to the comfort floor — no more waiting up to 30 minutes. On restart, startup coalescing reconciles a running fan (adopt as nat-vent if eligible, otherwise turn it off), and a manual fan change is treated as a timed override that is reclaimed on expiry or restart. The economizer also no longer starts the fan when it is warmer outside than inside.

## [0.4.23] — 2026-06-16

- Fix #326: Pre-cool now surfaces in the Next Automation card (next to bedtime setback, morning wake-up, etc.) instead of as a footnote under Status. Removed the hardcoded 'tonight' label — the trigger time itself conveys when. 'Next Action' renamed to 'Next User Action' to distinguish occupant advice from scheduled automations.

## [0.4.22] — 2026-06-15

- Fix #325: Four async_call_later callbacks in automation.py were missing the @callback decorator — HA emitted a thread-safety WARNING on every setpoint verify and fan verify event. The two lambda shortcuts (setpoint retry + setpoint verify) are now named @callback functions; the two fan-verify undecorated defs also get the decorator. No behavior change; eliminates the runtime warning.

## [0.4.21] — 2026-06-15

- Fix #323: Automation Time card now shows local HH:MM instead of the raw ISO timestamp.

## [0.4.20] — 2026-06-15

- Fix #258 CI: test infrastructure patches for pre-cool feature — isinstance guard in _build_predicted_indoor_future prevents MagicMock comparison errors; pre-cool stub attributes added to coordinator factory in test_hvac_session_detection and test_temperature_sensors; test_target_band updated to document correct warming-trend sign convention (modifier=-2.0 lowers cool ceiling, not raises it). All 50 golden scenarios pass.

## [0.4.19] — 2026-06-15

- Feat #258: Trend-aware overnight pre-cool — on warming-trend nights CA now banks cold thermal mass by lowering the AC ceiling mid-night (after nat-vent window closes or 4h before wake, whichever is later). Nat-vent suppresses AC pre-cool when it already achieved the target. A morning guard prevents the pre-cool target from dropping below comfort_heat + 2°F. Status card and chart target band both show the pre-cool dip. Sign-convention bug fixed: warm-trend modifier now correctly lowers the sleep ceiling (pre-cool) instead of raising it (energy setback).

## [0.4.18] — 2026-06-15

- Fix #321: HA restart no longer causes spurious manual overrides. A 5-minute startup coalescing window suppresses override detection; at the 5-minute mark CA evaluates sensor states and nat-vent conditions, then applies the correct operating mode with full INFO logging of every command issued.
- Fix #321: Grace period stuck-at-0 now self-heals. If the grace expiry callback is ever lost, the next 30-minute evaluation cycle detects the stale grace_end_time, logs an ERROR, and force-clears the override so automation resumes.
- Feat #321: Natural ventilation now acts as an active thermostat targeting the midpoint of the comfort band. The fan cycles on when indoor reaches midpoint+1°F and off at midpoint-1°F, re-evaluated on every thermostat temperature tick. Fan status surfaced as 'nat-vent (session active, fan idle)' when session is active but fan is idling between cycles.

## [0.4.17] — 2026-06-14

- Feat #320: Add step-by-step logging for contact sensor debounce and nat vent gate evaluation. When a window opens, logs now show: sensor detected, debounce timer start/expiry time, gate check values (outdoor/indoor temps, thresholds), and which specific guard (forecast or thermal floor) blocked activation. The next_automation sensor now shows 'Evaluating door/window sensors' with the expiry time during the debounce window.

## [0.4.16] — 2026-06-14

- Docs #261: Documented that heat-only and cool-only HVAC systems are unsupported. CA requires a system with both heating and cooling capability. Single-mode systems will not receive commands for their unsupported mode — this is expected behavior. See docs/02-ARCHITECTURE-REFERENCE.md.

## [0.4.15] — 2026-06-14

- Fix #318: Sleep setpoint config no longer blocks users from setting sleep temperatures cooler or warmer than daytime comfort bounds

## [0.4.14] — 2026-06-14

- Fix #313: Fan commands no longer trigger false manual-override detection. When Ecobee reverts its setpoint after a fan mode change, the coordinator now suppresses the setpoint-change override check for 30s after any fan command (matching the existing guard on hvac and temp commands).
- Fix #313: After every fan activation or deactivation, CA schedules a 30-second verify-and-repair callback. If the thermostat's setpoint has drifted more than 0.6°F from what CA commanded, CA re-asserts the correct setpoint — so any delayed Ecobee state report arrives within the temp-command recency window and is not misread as an override.
- Fix #313: Natural ventilation no longer exits when outdoor and indoor temperatures are equal. Equal temps mean neutral airflow (no benefit but no harm); only when outdoor is strictly warmer than indoor does nat-vent exit due to airflow reversal.

## [0.4.13] — 2026-06-14

- Fix #185/#310: solar_phase_offset_h now re-fits daily from the chart_log passive-daytime windows (incremental 2-day lookback). Previously, the one-shot startup backfill flag was persisted, so the fit ran exactly once and then never again — solar phase estimation was frozen from the first time the dashboard was opened. Now _maybe_run_periodic_solar_phase_fit() fires once per calendar day after the backfill completes.
- Feat #312: CA now estimates solar phase offset from AC duty cycle patterns when passive-window observations are unavailable (common in summer when AC runs during peak solar hours). A secondary EWMA (α=0.07, min 3 qualifying days) accumulates AC-based estimates without contaminating the primary passive EWMA. A 5-tier resolver picks the freshest available estimate; a 90-day staleness gate ensures stale home-specific data is still preferred over the generic prior.

## [0.4.12] — 2026-06-13

- Fix #184/#308: k_solar confidence is now graded (none/low/medium/high) based on committed solar_gain observation count — thresholds: low ≥20, medium ≥50, high ≥100. Previously hardcoded to 'none' permanently regardless of how many observations had been collected.
- Fix #185/#308: _run_solar_phase_chart_log_fit() now emits structured INFO log lines at entry, window filtering, EWMA update, and no-qualifying-windows exit — making it possible to diagnose why solar_phase_offset_h is or isn't learning from chart_log passive windows.
- Fix #308: tools/learning_db.py --model now includes a Solar Model section showing solar_phase_offset_h, observation_count_solar, confidence_k_solar, and a rejection summary.

## [0.4.11] — 2026-06-13

- Fix #290: Grace expiry UI refresh, bedtime recovery on HA restart, setpoint validation, and AI report Settings column display.
- Fix #263: After an HA restart with a door or window open, automation no longer stays paused indefinitely. Pause state is no longer persisted across restarts; the door/window state-change listener re-detects open sensors within ~5 minutes and re-pauses cleanly — eliminating the race where slow cloud reconnect left the home with HVAC off and no nat-vent for up to 30 minutes after restart.

## [0.4.10] — 2026-06-13

- Fix #295: On hot days, CA no longer holds the pre-cool temperature offset (−2°F) after the home reaches the comfort ceiling. Once the pre-cool target is met, a _pre_condition_achieved flag is set and the ceiling reverts to the configured comfort setpoint for the rest of the day — preventing unnecessary overcooling.
- Fix #301: CA no longer uses heat_cool dual-setpoint mode. Every thermostat command is now a single climate.set_temperature call containing both the mode (cool or heat) and the single relevant setpoint — CA sets the bound that matters and lets the thermostat manage its own band internally.
- Fix #301: If the thermostat does not accept a commanded setpoint within 10 seconds, CA automatically retries the same command 15 minutes later. The retry is cancelled if a newer command has been issued in the meantime.
- Fix #301: README now documents that thermostats must have their built-in schedules and comfort programs disabled, and their hold type set to 'hold until I change', for CA to operate correctly.

## [0.4.9] — 2026-06-13

- Fix #299: CA setpoint writes to the Ecobee thermostat now bypass HA's deduplication filter. Every setpoint command sends an intentionally-offset pre-write followed by the exact target, guaranteeing the command reaches the physical thermostat even when HA's optimistic state already matches the target.
- Fix #299: Dual-setpoint (heat_cool) writes no longer include hvac_mode in every call. The mode switch is sent only when the thermostat is not already in heat_cool mode, preventing the Ecobee from applying its comfort-program setpoints (65/75) instead of CA's commanded values (e.g. 68/74).
- Fix #299: CA now verifies that reported thermostat setpoints match its commanded values within 1°F before treating a state change as a confirmation. When setpoints differ by more than 1°F in heat_cool mode the event is treated as an Ecobee comfort-program reassertion, not a confirmation, preventing false-positive override suppression.
- Fix #299: handle_bedtime() now skips the setpoint write if another setpoint command was issued within the last 30 seconds, eliminating a startup race where the coordinator's initial classification cycle and the sleep-window bedtime handler both fired and produced a double-write that triggered the Ecobee comfort-program reversion.
- Fix #299: Fallback default temperatures in _set_temperature_for_mode() corrected from 68°F/76°F to 70°F/75°F, matching the documented comfort defaults.

## [0.4.8] — 2026-06-13

- Fix #293: After every HA restart, CA no longer treats a heat_cool thermostat state as a manual override. The startup check now recognises heat_cool as CA-compatible with cool/heat classifier outputs, preventing a spurious 30-min grace period that blocked automation each morning.
- Fix #293: When natural ventilation ends (door/window sensors close), CA now uses the dual-setpoint heat_cool command for capable thermostats instead of reverting to single-setpoint cool mode. Ecobee users no longer see the band drop from [68/74] to a single 72°F setpoint after every ventilation cycle.
- Fix #293: AI activity investigator now includes active thermostat setpoints (single-setpoint temperature and dual-setpoint low/high) in its context block so the AI can explain pre-cool offsets and band boundaries in morning summaries.
- Fix #293: GitHub issue titles generated from the dashboard no longer include a redundant 'Climate Advisor: ' prefix; the full AI-generated summary is used up to 100 characters.

## [0.4.7] — 2026-06-13

- Fix #290: Grace period expiry now immediately triggers a coordinator refresh so sensor entities reflect cleared override state without waiting up to 30 minutes.
- Fix #290: On HA restart, if the system is in the sleep window and no manual override is active, bedtime setback is re-applied on the first classification cycle (prevents sleeping at daytime comfort temps after a restart mid-night).
- Fix #290: After every climate.set_temperature or _set_temperature_dual() call, a 10-second validation callback checks whether the thermostat accepted the commanded setpoints; mismatches are logged as ERROR with commanded vs reported values.
- Fix #290: AI activity report Settings column now correctly shows setpoint changes: override_detected event payload includes old_setpoint_f and new_setpoint_f fields that the annotation code uses to build the [settings: setpoint: X°F→Y°F] string.

## [0.4.6] — 2026-06-12

- Fix #286: climate.set_temperature for dual-setpoint (heat_cool) thermostats now includes hvac_mode='heat_cool' in the service payload. Without this key the Ecobee integration silently ignored the setpoints and reverted to its internal hold values within 1 second. Log now shows actual service values (post-unit-conversion) so unit-mismatch issues are diagnosable from logs alone.

## [0.4.5] — 2026-06-12

- Fix #284: Door/window close and dashboard Resume now correctly restore both heat and cool setpoints in heat_cool (dual-setpoint) mode. Previously, _set_temperature_for_mode() silently returned without writing when the classification used heat_cool — leaving the thermostat at whatever the Ecobee's own schedule had set until the next 30-min coordinator cycle.
- Fix #284: AI investigator context now includes target_temp_low and target_temp_high from the live thermostat entity — absence of these fields made Issue #281 root cause analysis inconclusive.
- Fix #284: CA dashboard now shows a (CA: X/Y) indicator when live thermostat setpoints diverge from CA's configured comfort band by more than 1°F.

## [0.4.4] — 2026-06-12

- Fix #282: HA restart now clears all override and grace state (clean slate). CA starts in fresh automation mode after every restart. Override state and grace timers are no longer carried over. The 5-minute startup settling window remains.
- Fix #282: Manual grace expiry now notifies the user by default. Message updated to: 'Your manual thermostat override has expired. Climate Advisor has resumed automated control.'
- Fix #282: Brief thermostat adjustments that self-revert within the confirmation window now send a notification: 'treated as transient, CA continues normal operation.'
- Fix #282: Changing thermostat mode while an override grace is active now restarts the confirmation window for the new mode, rather than being silently ignored.

## [0.4.3] — 2026-06-12

- Fix #277: Whole-house fan now suppresses HVAC while active (sets thermostat off; restores prior mode when fan stops). Running AC while exhausting conditioned air is no longer possible.
- Fix #277: All sensors closing now stops the whole-house fan even when natural ventilation was not the trigger — the whole-house fan serves no purpose with windows sealed.
- Fix #277: CA's own HVAC-off command (which asserts fan_mode=auto as a side effect) no longer triggers a spurious fan manual-override grace period. Cloud thermostat echoes arriving after the 30s guard window are now suppressed.
- Fix #277: A single thermostat event that includes both a setpoint change and a fan_mode change now triggers at most one override response — setpoint wins. Previously, CA's coordinator re-application produced both a setpoint override and a fan grace period simultaneously.
- Fix #277: Activity report event log now places setpoint values in the Settings column for override_detected entries. AI investigator flags events that occur at exact automation intervals as timing-coincident (may be automation-caused).

## [0.4.2] — 2026-06-11

- Fix #239: CA's own fan activation no longer triggers a spurious manual-override grace period. When CA calls climate.set_fan_mode for natural ventilation, the fan_mode echo from a cloud thermostat can arrive after _fan_command_pending has already cleared. A new _fan_command_time timestamp guard (_is_recent_fan_command, 30 s) mirrors the existing _is_recent_temp_command pattern and suppresses false override detection. Parallel fix to #221/#225.

## [0.4.1] — 2026-06-11

- Fix #269: Manual overrides now correctly detected in heat_cool (dual-setpoint) mode. Four bugs fixed: CA's own mode command no longer triggers a false fan override grace period (cloud-thermostat echo arrives after the 30s guard); heat_cool → cool mode switch is now detected as a manual override; dual setpoint changes (target_temp_high/target_temp_low) are now visible and trigger a grace period; hvac_mode now captured in incident records.
- Fix #264: Economizer (comfort-band fan assist) no longer re-applies the full classification setpoint when it exits, overriding a user's manual adjustment during the fan-only period.
- Fix #266: Dashboard Status tab now shows the actual band setpoints [heat_floor/cool_ceiling] for heat_cool thermostats rather than a single target_temperature.
- Fix #190: Forecast pipeline — tomorrow's high no longer shows as day-after-tomorrow in negative-UTC-offset timezones after 5 pm (evening UTC rollover). Reference date is now local calendar date; forecast entries are matched by raw API date.
- Feat #193: Activity report now includes a full event log (last 12 h, chronological) and a per-override detail section showing each manual setpoint change with time, direction, and duration. The Timeline section reflects the complete sequence, including automation re-assertions after an override cleared.

## [0.4.0] — 2026-06-10

- Feat #249: Thermostat-is-the-controller — Climate Advisor now programs a comfort band [comfort_heat, comfort_cool] and lets the thermostat's own deadband hold it, instead of switching HVAC off and running a 30-minute supervisory loop. The home pre-heats cold mornings up to comfort and cools warm afternoons by itself; natural ventilation keeps the band armed (free cooling stays free while the heat floor stays defended); aggressive_savings widens the band. away/vacation/sleep use setback bands. Single-mode thermostats arm the threatened edge; dual heat_cool thermostats hold both edges with one command.
- Fix #247: The ODE ceiling guard now escalates to AC when outdoor stays below indoor but ventilation can't hold the comfort ceiling (re-occurrence of #218's incomplete fix). Under the #249 band model this is the misprogramming backstop; the comfort band is the primary defense.

## [0.3.56] — 2026-06-08

- Fix #220: Manual override now cleared when occupancy transitions to away or vacation — automation resumes correctly after user leaves home; override no longer silently persists
- Fix #221: Away-mode setback no longer falsely detected as manual override — automation-issued setpoint change on occupancy transition correctly attributed to automation
- Fix #222: Away/vacation setback now uses correct mode-aware setpoint — cool-mode thermostat correctly receives setback_cool (79°F), not setback_heat (61°F) (critical bug: wrong setpoint caused AC to run to 61°F all day while away)
- Feat #223: Closed-loop simulation feedback system — production incidents auto-generate pending BSpec scenarios; simulation_loop.py validates them; Tests dashboard tab surfaces results; approve_pending_test API promotes to golden
- Fix #227/#199: Grace period timer restored after HA restart — timer re-scheduled on startup if grace was active; override auto-clears if timer already expired (previously: restart destroyed timer; system stuck with 0 min remaining until user clicked Resume)
- Fix #229: Simulator alignment overhaul — six simulator divergences from production fixed; three-way audit protocol added; occupant-first framing and simulator mirror rules encoded in process policy
- Fix #230: Grace period expiry now converges to scheduled automation state — bedtime setback suppressed during grace is applied when grace expires (previously: grace expiry resumed from daytime classification; occupant slept at wrong temperature)
- Fix #231: Nat-vent exits at home comfort ceiling when occupancy is away — nat_vent_away_ceiling_exit fires when indoor >= comfort_cool while away; free cooling within home band; HVAC setback handles the rest

## [0.3.55] — 2026-06-03

- Fix #190: _get_forecast() switches to local date + raw forecast date — tomorrow's forecast no longer shows day-after-tomorrow in evening hours (UTC rollover bug in negative UTC offset timezones)
- Feat #193: AI activity report gains event log section and override detail section — recent events and manual override history visible in generated reports
- Fix #197: Setpoint-only thermostat change now enters manual grace period — user adjusting target temperature without changing mode correctly detected as override
- Fix #203: Sensor health comprehension guarded against int instrumentation keys — integration no longer raises TypeError on health data with numeric keys
- Fix #204: Bedtime setback and morning wakeup respect active manual override — automation defers scheduled setpoint changes when user has active override in effect
- Fix #205/#206: Three activity report and override detection fixes: false override_detected events from automation fan actions eliminated (compound command-pending guard); timeline now renders as markdown table with Time|Event|Source columns; markdown tables render correctly in the dashboard panel (frontend renderer added)
- Fix #208: Activity report time window now respected — event log filters to requested hours (was hardcoded 24h); reports >36h include HISTORICAL DAILY SUMMARIES per-day table from learning records

## [0.3.54] — 2026-05-30

- Fix #172: Predicted indoor temperature no longer drops suddenly at sleep time — ODE uses classification.hvac_mode for today's mode (prevents evening forecast-high flip); hvac_mode passed explicitly to both ODE functions (prevents wrong Q branch on sleep setback)
- Fix #174: chart_log time sourcing unified — dt_util.now() replaces datetime.now(UTC) in get_entries() and _maybe_prune() for consistent behavior across production and tests
- Fix #176: DailyRecord accumulated counters survive HA restart mid-day — _async_send_briefing() preserves hvac_runtime_minutes, manual_overrides, and 6 other fields when replacing _today_record on same calendar day; state saved on HVAC off
- Feat #177: AI Investigator noise reduction — abandonment reasons pre-classified (operational vs quality-failure), count discrepancy ≤1 suppressed as flush lag, pending observations removed from context; new investigate-ca-report Claude Code skill with 5-phase triage taxonomy
- Feat #180: GitHub issue submission modal restored — Submit GitHub Issue button in investigation panel, config flow GitHub Integration step, default title 'Climate Advisor: Investigative Analysis'
- Feat #186: window_compliance denominator in AI investigator context — shows '0.6667 (2 of 3 windows-recommended days)' to prevent AI misinterpretation

## [0.3.53] — 2026-05-20

- Fix #170: Setpoint-only overrides now enter manual grace period immediately — CA no longer resets thermostat after user adjusts target temperature without changing mode (handle_setpoint_override() bypasses confirmation window; CONFIG_METADATA description corrected)

## [0.3.52] — 2026-05-20

- Feat #166: AI Investigation Analysis — feedback loop (helpful/not helpful/wrong), unified investigation view with history tab, GitHub issue submission from the dashboard
- Feat #164: Chart forward navigation into predicted future — '>' button advances beyond current time using physics-simulated indoor ODE results
- Fix #162: Chart forward navigation after historical re-fetch — advances from the retrieved anchor timestamp instead of jumping to current time

## [0.3.51] — 2026-05-19

- Fix #158: Investigation history panel shows full report text — AI no longer duplicates findings across sections in multi-section reports

## [0.3.50] — 2026-05-18

### Fixed

- **Thermal: `"samples": []` key removed from HVAC obs dict** (#156): `_start_hvac_observation`
  created the observation dict with both `"samples": []` and `"active_samples": []`. Because
  Python dicts return the first matching key, `obs.get("samples", ...)` always returned `[]`
  regardless of how many samples had accumulated in `active_samples`. All HVAC observations
  were silently discarded at commit time — `k_active_cool` and `k_active_heat` could never be
  learned despite AC or heat cycling normally. `"samples"` key removed; all HVAC commit paths
  now read `active_samples` and `post_heat_samples` explicitly.

- **Thermal: Startup recovery now correctly handles HVAC pending observations** (#156):
  The startup recovery loop (run on HA restart to continue or abandon in-flight observations)
  used `obs.get("samples", [])` for all types. For HVAC types, this always returned `[]` due
  to the key-shadow bug, so every pending HVAC observation was abandoned with `n=0` on every
  HA restart. Recovery is now phase-aware: `post_heat` phase reads `post_heat_samples`
  (min_s = `THERMAL_MIN_POST_HEAT_SAMPLES`); `active` phase reads `active_samples`
  (min_s = 1 — any sample worth recovering). Backward-compat fallback retained for
  pre-fix persisted observations.

- **Thermal: `_abandon_observation` now reports real sample count in rejection log** (#156):
  Rejection log `n` field was always computed from `obs.get("samples", [])` — the shadowed
  empty list — so all HVAC rejection entries showed `n=0` regardless of actual sample count.
  Fixed to read the correct key per type (`active_samples` for HVAC active-phase,
  `post_heat_samples` for post-heat, `samples` for rolling-window types).

### Added

- **Thermal: Event-driven sampling during active HVAC phase** (#156): `_async_thermostat_changed`
  now appends a sample to `active_samples` whenever a thermostat state change occurs while HVAC
  action is active. A 60-second decimation gate prevents duplicate samples. Short HVAC cycles
  (1–4 min) that complete between 5-min polling ticks previously accumulated only 1 sample
  (0 OLS pairs); they now accumulate 3–10 event-driven samples, making `compute_k_active_single_point`
  much more likely to succeed on short-cycling thermostats.

- **`learning_db.py --pending` flag** (#156): Shows in-flight observations from the
  `pending_observations` dict — type, phase (`active`/`post_heat`), elapsed time, sample
  counts, and peak indoor temperature. Run during a live HVAC cycle to confirm samples are
  accumulating correctly.

- **`learning_db.py --rejections` enhancements** (#156): The rejection log output now includes
  a top-reason summary table at the bottom (reason code, count, percentage). New `--type TYPE`
  filter narrows output to a specific obs_type (e.g., `--rejections --type hvac_cool`).

- **AI investigator: Thermal pipeline health coverage** (#156): A new
  `=== THERMAL OBSERVATION PIPELINE ===` context section is added to the investigator's
  context. Per-type rows show committed/rejected counts, top rejection reason codes, and
  `NEVER LEARNED` flags when `k_active_cool` or `k_active_heat` is `None`. Pending in-flight
  observations are listed with phase and sample count. `THERMAL PIPELINE HEALTH rules` in the
  system prompt instruct the AI to flag 0-committed HVAC types and repeated `new_session_started`
  abandonments as pipeline failures rather than leaving them implicit in null model fields.

## [0.3.49] — 2026-05-18

### Added

- **Chart: Automation Setpoints overlay** (#153): Replaces the "Thermostat Setpoint"
  overlay (which was empty all warm season because it read the hardware `target_temperature`
  attribute, null when HVAC is off). The new overlay reads two always-present defense lines
  derived from the target band schedule: a heat threshold (amber, lower bound) and a cool
  threshold (blue, upper bound). Both are on by default. The setback step at bedtime is now
  clearly visible as the heat line drops from `comfort_heat` to the configured sleep setpoint
  at `sleep_time` and rises again at `wake_time`.

- **Chart: Future activity bars** (#153): HVAC, Fan, and Windows Recommended activity bars
  now extend into the future with predicted state shown at 40% opacity. Predictions derive
  from today's classification (`hvac_mode` intent), natural ventilation conditions computed
  from the hourly forecast, and windows-recommended logic applied to forecast outdoor vs.
  predicted indoor temperatures. A vertical separator marks the now boundary between solid
  historical bars and faint future bars.

## [0.3.48] — 2026-05-17

### Added

- **Bedtime setback visibility** (#151): `handle_bedtime()` now emits `bedtime_setback` and
  `bedtime_setback_skipped` events to the structured event log, making all skip/fire paths
  observable by the AI investigator. `DailyRecord` gains five new fields:
  `setback_heat_applied_f`, `setback_cool_applied_f`, `setback_depth_f`,
  `setback_was_adaptive`, and `setback_skipped_reason`. Previously, the on-mode warm/mild
  nights took a silent pass (correct behavior); that pass is now logged as `reason="hvac_off"`.
  Doc error in §6a: Away row now correctly says "Skip" rather than "Apply bedtime setback".

- **`learning_db.py --daily [N]`** (#151): New `--daily` flag prints the last N nightly
  setback records (date, day type, mode, applied temp, depth, adaptive flag, skip reason).
  Default: 30 nights. Useful for diagnosing whether setback has been firing on heat/cool
  nights or silently skipping all warm-season nights.

- **Chart: Thermostat Setpoint overlay** (#151): The chart now captures the thermostat's
  `target_temperature` at every 30-min poll and exposes two new API fields:
  `historical_setpoint` (actual past setpoints) and `predicted_setpoint` (derived from
  the target band — lower bound in heat mode, upper in cool mode, null in off mode). The
  dashboard renders these as a stepped purple/magenta line with solid past, dashed future,
  and faint-dotted forward-fill during off-mode periods. Toggle via the Thermostat Setpoint
  overlay checkbox.

## [0.3.47] — 2026-05-17

### Fixed

- **AI activity report: k_active_hvac shows None** (#149): `_format_engine_status_for_ai`
  read `hvac_info.get("k_active_heat")` directly — always None. The real shape nests
  these values under `hvac_info["value"]["heat"]` and `hvac_info["value"]["cool"]`. Fixed
  to read nested keys; added chain tests covering the full `get_engine_status()` →
  formatter path.

- **AI activity report: comfort band false positives** (#149): The cross-validation check
  flagged any indoor temp below `comfort_heat` with zero tolerance. Thermostat deadband
  (±0.5–1.5°F) made these false alarms routine. The check now acquires
  `swing_heat_f_display` / `swing_cool_f_display` from the thermal model (default
  `THERMAL_SWING_DEFAULT_F` = 1.5°F) and only flags when the shortfall strictly exceeds
  the learned swing.

- **AI activity report: section repetition** (#149): Added `DEDUPLICATION RULE` to
  `_SYSTEM_PROMPT` with exclusive section role definitions. SUMMARY / TIMELINE /
  DECISIONS / ANOMALIES / DIAGNOSTICS each have a non-overlapping scope; one-line
  cross-references are allowed, verbatim restatement is not.

- **Thermal: HVAC swing peak capture at HVAC-off** (#149): `_end_hvac_active_phase`
  previously did not sample indoor temperature at the HVAC-off moment. `peak_indoor_f`
  was updated only at 30-min poll cycles, making swing measurements based on stale data.
  The method now appends a final active sample at HVAC-off and updates `peak_indoor_f`
  if the shutoff temperature exceeds the prior peak.

## [0.3.26] — 2026-04-22

### Added

- **Sleep temperatures** (#101): New `sleep_heat` and `sleep_cool` config fields give
  users independent overnight setpoints separate from the away setback. Config entry
  migrates from v14 to v15 automatically; defaults preserve prior adaptive setback
  depth.
- **AI Investigator: version context and GitHub issue awareness** (#105): The investigator
  now reads the running integration version at startup and has access to the project's
  open and closed GitHub issues, enabling it to correlate symptoms with known fixes.
  Live rolling status updates during investigation replace the static progress message.
- **Thermal modeling v2: physics-based prediction** (#114): OLS regression over the full
  post-heat decay curve replaces the broken single-point model. Parameters `k_passive`,
  `k_active_heat`, and `k_active_cool` are learned from observed data; a
  `PendingThermalEvent` state machine tracks observation windows across HA restarts.
  Legacy field aliases preserved for backward compatibility.
- **Natural ventilation directional guard** (#115): Activation now requires
  `outdoor < indoor` (directionally beneficial airflow). A symmetric exit condition
  (`outdoor ≥ indoor`) was added to all three activation sites and the continuous
  condition checker. `natural_vent_delta` is now solely a ceiling tolerance above
  `comfort_cool` when indoor is hot.
- **Temperature Setpoints settings section** (#112): New `"setpoints"` category in
  `CONFIG_METADATA` and a dedicated options wizard step group all six temperature targets
  (comfort, setback, sleep) together. Category order in the settings tab:
  Core → Temperature Setpoints → Sensors → Fan → Schedule → Advanced → AI Settings.

### Fixed

- **Predicted indoor spike at bucket boundary** (#106): Thermal lag treated as an index
  offset (wrong physics) combined with hard bucket boundaries at 60°F/70°F caused a
  7.6°F instant jump in predicted temps at 11 PM on cool nights. Fixed with first-order
  exponential smoothing (α = 1/lag_h) and linear interpolation over ±2°F transition
  zones in `_outdoor_conditional_diff`.
- **Wildly incorrect predicted indoor temperatures** (#104): `compute_predicted_temps`
  used `setback_cool = 80°F` for overnight hours on warm/mild days and re-anchored
  daytime drift to `comfort_cool` every hour instead of accumulating. Corrected setpoint
  logic and accumulation model.
- **Win Rec / Windows bars drop to zero on HVAC events** (#117): Three event-driven
  `_chart_log.append()` call sites omitted `windows_open` and `windows_recommended`,
  defaulting to `False` on every HVAC state change. All three now read current sensor
  and classification state.
- **Outdoor temperature spikes in chart** (#110): Short HVAC cycles under 30 minutes
  were missing from chart data, and override events were reading the climate entity's
  indoor sensor as the outdoor temperature.
- **HVAC bar shows continuous heating in fan circulation mode** (#109): `hvac_action=
  "fan"` remapped to "heating" even when `fan_mode="on"` (continuous circulation). Fix
  reads `fan_mode` attribute and skips remap for any non-auto fan mode.
- **HVAC bar time alignment** (#103): Bar chart start/end times now align with
  temperature curve swings and track zoom/reset correctly.
- **Sleep temperatures buried under Schedule in settings** (#112): `sleep_heat` and
  `sleep_cool` had `category: "schedule"` since v15, grouping them with time fields.
  Changed to `category: "setpoints"`.
- **Sleep temperature ordering constraints removed** (#108): Config flow no longer
  enforces that sleep temps must fall strictly between setback and comfort bounds.
- **Status page showing °F when °C configured** (#100): Status tab cards now respect
  the configured temperature unit.
- **Thermal observation pipeline broken on `hvac_action="fan"` thermostats** (#93):
  Running-detection guard `if new_action and old_action` never fired for thermostats
  reporting `hvac_action="fan"` during heating cycles. Fixed to check set membership.
  `state_contradiction_warning` events now emitted to the structured event log (not
  only to AI narrative text).
- **`windows_recommended` did not reflect current outdoor conditions** (#111): The
  recommendation now evaluates whether opening windows would keep or move indoor temp
  toward the comfort zone, and suppresses the recommendation during extreme conditions.
- **Fan running untracked, chart indicator missing, timezone inconsistency** (#113):
  Fan state reclaimed after HA restart; fan indicator restored in chart; AI report
  timestamp corrected to UTC; investigator awareness of thermostat swing added.
- **Timezone audit: UTC/local bugs across predicted indoor and forecast** (#107): Seven
  timezone bugs fixed. Critical: forecast builder was reading key `"time"` instead of
  HA's `"datetime"` — all predicted indoor data silently dropped. Also fixed:
  naive/aware datetime mix, UTC/local date mismatch in forecast day selection near
  midnight, and naive AI report timestamps.
- **HVAC bar displaying incorrect "heating" state** (#102): Resolved with #93/#100
  combined fix batch.

### Changed

- Config entry schema version: **v14 → v15** (sleep temperature fields; migration is
  idempotent and backward compatible).
- `compute_bedtime_setback()` now checks explicit sleep temp config first; adaptive
  fallback retained for installs without sleep temps configured.
- `_build_predicted_indoor_future` now uses HA's `"datetime"` forecast key (with `"time"`
  fallback), `dt_util.as_local()` conversion, and `sleep_heat`/`sleep_cool` for overnight
  setpoints.

### Infrastructure

- **Simulator occupancy and thermostat-mode support** (#98): Simulator models internal
  `_occupancy_mode` state driven by `occupancy_change` events; warm-day setback scenarios
  explicitly documented as `simulator_support: false` with rationale. Manifest signing
  enforced for golden scenarios.
- **10 golden scenarios promoted**: Natural ventilation directional guard scenarios from
  #115 and related regression cases promoted from `pending/` after production validation.
- Config entry VERSION bumped to 15 in `config_flow.py`.

---

## [0.3.18] — (prior release)

See [GitHub release history](https://github.com/gunkl/ClimateAdvisor/releases) for prior
versions.
