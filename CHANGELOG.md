# Changelog

All notable changes to Climate Advisor are documented here.
This project follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions.

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
