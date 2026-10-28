<!-- Nav: <- Context: [Simulation Feedback Loop](simulation-feedback-loop.md) | -> Detail: coordinator.py (_detect_and_emit_incidents) · automation.py (_set_temperature) -->

# Incident Classes -- Reference Table

## Anchors

| Question | Short answer (<= 2 sentences) | -> Full answer |
|---|---|---|
| Which incident classes are proactive (fire at command time)? | Two: `setpoint_mode_inconsistency` — fires inside automation.py _set_temperature() before the HA service call; `hvac_command_failed` (Issue #805) — fires in automation.py (via `_call_climate` / `_note_hvac_command_failed`, once per failure episode, Issue #1049) when the HA service call itself fails (e.g. a removed climate_entity). Most other classes are reactive and fire post-cycle in coordinator._detect_and_emit_incidents. | [Proactive vs. Reactive column below](#incident-class-reference) |
| What is the `incident_detected` event payload schema? | `type`, `time`, `incident_class`, `incident_id`, `indoor_f`, `outdoor_f`, `hvac_mode`, `comfort_cool`, `comfort_heat`, plus class-specific fields (`nat_vent_active`, `manual_override_active`, `occupancy_mode`, `setpoint_f`, and — for `hvac_command_failed` (#805) — `hvac_mode`, `climate_entity`, `error`, and (when a setpoint was being written) `setpoint_f`; for `comfort_violation`/`comfort_undertemp` — `thermostat_mode`, `thermostat_action`, `last_commanded_mode`, `last_commanded_setpoint_f`, `last_command_result`). | [Event Payload](#event-payload) |
| Does `comfort_violation`/`comfort_undertemp` require a sustained 15-min deviation? | No -- both are a **point-in-time** check (>0.5 F past the comfort band edge) with a 30-min incident-dedup window, not a duration tracker; as of Issue #411 they also skip emission when the deviation is within nat-vent-cycling tolerance (`coordinator._is_nat_vent_tolerated_deviation()`). | [Incident Class Reference below](#incident-class-reference) |
| Which classes were added for bugs #220-222? | Four new classes: `setpoint_mode_inconsistency` (#222 -- silent wrong setpoint), `rapid_override_after_automation` (#221 -- false override detection), `occupancy_transition` (#220 -- coverage gap), `override_active_on_occupancy` (compound condition at occupancy change). | [Bug Motivation column below](#incident-class-reference) |
| How are incidents deduplicated within a cycle? | Each reactive class is emitted at most once per 30-min coordinator cycle. Proactive `setpoint_mode_inconsistency` fires once per command call (only when inconsistency is detected). The loop tool cross-references `results/processed_incidents.json` to avoid re-processing the same incident_id. | [Deduplication](#deduplication) |
| Which classes have HIGH silent failure risk? | `setpoint_mode_inconsistency` (wrong setpoint in wrong HVAC mode; no comfort violation for hours) and `rapid_override_after_automation` (false override detection blocks automation silently). | [Silent Failure Risk column below](#incident-class-reference) |
| Is the "house off-target, no equipment running" stall alert an incident class? | No (Issue #1050). `hvac_output_stall` / `hvac_output_stall_recovered` are ordinary Activity Log events emitted by `coordinator._update_comfort_stall()` -- they never produce an `incident_detected`, do not use the dedup window above, and are not in the class table below. The existing point-in-time `comfort_violation`/`comfort_undertemp` incidents still fire independently (their payload gained the thermostat mode/action context in Issue #1049). | [08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md#house-off-target-stall-alert-issue-1050-part-of-1048) |
| Is `hvac_command_failed` emitted on every failed command? | No (Issue #1049). It is episode-gated: `_note_hvac_command_failed()` announces once per failure episode via `next_fallback_episode`, the ERROR log line is still written for every failure, and the next successful climate call closes the episode with an `hvac_command_recovered` Activity Log event. HA validation rejections (out-of-range setpoint, mode not offered) are not failures and open no episode. | [Event Payload](#event-payload) |

---

## Incident Class Reference

| Class | Production Signal | Proactive / Reactive | Track | Silent Failure Risk | Bug Motivation |
|---|---|---|---|---|---|
| `comfort_violation` | Indoor > comfort_cool + 0.5 F at the moment of a coordinator cycle (point-in-time check, not a sustained-duration tracker), with a 30-min incident-dedup window; suppressed when the deviation is within nat-vent-cycling tolerance while a nat-vent session is active (Issue #411) | Reactive | Both | Low (visible to user) | Baseline coverage; nat-vent-tolerance gate added #411 |
| `comfort_undertemp` | Indoor < comfort_heat - 0.5 F at the moment of a coordinator cycle (point-in-time check, not a sustained-duration tracker; sub-class of comfort_violation, tracked separately), with a 30-min incident-dedup window; suppressed when the deviation is within nat-vent-cycling tolerance while a nat-vent session is active (Issue #411) | Reactive | Both | Low (visible) | Baseline coverage; nat-vent-tolerance gate added #411 |
| `nat_vent_escalation` | `nat_vent_ceiling_escalation` event in event_log | Reactive | Logic | Low | Baseline coverage |
| `override_detected` | Any `override_detected` event in event_log | Reactive | Integration | Medium | Baseline coverage |
| `system_restart` | `system_restarted` event in event_log | Reactive | Both | Low | Baseline coverage |
| `setpoint_mode_inconsistency` | Applied setpoint < comfort_heat while HVAC mode is `cool`; or applied setpoint > comfort_cool while HVAC mode is `heat` | **Proactive** | Integration | **HIGH** | #222 -- 61 F setpoint in cool mode, silent for hours |
| `rapid_override_after_automation` | `override_detected` event within 60s of any automation decision event in event_log | Reactive | Integration | **HIGH** | #221 -- automation-driven setpoint change falsely flagged as manual override |
| `occupancy_transition` | Any `occupancy_change` event in event_log during the last cycle | Reactive | Both | Medium | #220 -- near-zero occupancy transition coverage in golden suite |
| `override_active_on_occupancy` | `manual_override_active=True` at the time of an `occupancy_change` event | Reactive | Integration | Medium | Compound condition exposed by #220 investigation |
| `hvac_command_failed` | `hass.services.async_call("climate", ...)` fails inside `_call_climate` (used by `_set_hvac_mode()`, `_set_temperature()` and the retry nudge) — announced once per episode; recovery is the `hvac_command_recovered` event | **Proactive** | Integration | **HIGH** | #805 -- a removed/invalid `climate_entity` previously propagated uncaught, risking an aborted update cycle instead of a caught, logged, notified failure |
| `grace_churn` | The same automation-grace trigger starts 3 or more times within 60 minutes (`automation.py` `_note_automation_grace_start`, called from the unprotected-grace start); at most one incident per trigger per 6 hours | **Proactive** | Logic | Medium | #1103/#1104 -- a thermostat-initiated fan blower CA kept stopping caused repeated automation graces that looked like manual overrides; the loop is now reported once instead of staying silent |

---

## Event Payload

All `incident_detected` events share a common payload schema:

```json
{
  "type": "incident_detected",
  "time": "<ISO-8601 timestamp>",
  "incident_class": "<one of the 9 classes above>",
  "incident_id": "<ISO-8601 timestamp used as unique ID>",
  "indoor_f": 76.2,
  "outdoor_f": 68.1,
  "comfort_cool": 74.0,
  "comfort_heat": 70.0,
  "hvac_mode": "cool",
  "nat_vent_active": false,
  "manual_override_active": false,
  "occupancy_mode": "home",
  "setpoint_f": 61.0
}
```

All numeric fields are in Fahrenheit regardless of the user's display unit setting. `setpoint_f` is the applied setpoint (relevant for `setpoint_mode_inconsistency`; null for other classes where no setpoint was issued). As of Issue #1049, `setpoint_f` is always the internal °F setpoint in every emitter (one engine copy previously carried the display-unit value under the `_f` key); all payloads are built by `incident_payload.build_incident_payload()` ([Incident Payload Brief](incident-payload-brief.md)).

**`hvac_command_failed` (Issue #1049):** payload `hvac_mode`, `climate_entity`, `error`, and `setpoint_f` when a setpoint write failed. Episode-gated (see Deduplication). Companion Activity Log events that are NOT incidents: `hvac_command_recovered` (`hvac_mode`, `climate_entity`, `minutes_down`), `hvac_mode_not_applied` (`kind` = `not_obeyed`/`rejected`, `requested_mode`, `actual_mode`, `climate_entity`) and `hvac_mode_applied_recovered` (`requested_mode`, `climate_entity`, `minutes_down`) -- see [Computation Reference, Command shapes since #301](08-COMPUTATION-REFERENCE.md#command-shapes-since-301-explicit-mode-switch-issue-1049).

**Thermostat context on comfort incidents (Issue #1049, change 8):** `comfort_violation` and `comfort_undertemp` also carry, from `coordinator._thermostat_incident_context()`: `thermostat_mode` (the climate entity's live state, e.g. `off`), `thermostat_action` (`hvac_action`: heating/cooling/idle), `last_commanded_mode`, `last_commanded_setpoint_f` (internal °F) and `last_command_result` (`sent`/`blocked`/`dry_run`/`failed`/`superseded`/`not_sent`). They answer "was the house cold because the thermostat was Off / in the wrong mode / idle, and what did CA last ask of it?" Fields are omitted when unknown. `nat_vent_active`, `manual_override_active`, `occupancy_mode` provide context for all classes -- they reflect coordinator state at the time of detection. As of Issue #411, `comfort_undertemp` also carries `nat_vent_active` (matching `comfort_violation`, which already had it), so a genuine sustained violation that fires *during* an active nat-vent session is still visible with full context, even though in-tolerance deviations during nat-vent cycling no longer emit an incident at all.

---

## Deduplication

**Within a cycle:** `_detect_and_emit_incidents()` runs once per 30-min coordinator cycle. Each reactive class is checked once per call and emitted at most once. If the same class condition persists across multiple cycles, one `incident_detected` event is emitted per cycle.

**Nat-vent-tolerance gate (Issue #411):** `comfort_violation`/`comfort_undertemp` additionally call `coordinator._is_nat_vent_tolerated_deviation(indoor, comfort_heat, comfort_cool)` before emitting. This returns `True` (suppress) only when `automation_engine._natural_vent_active` is `True` **and** indoor is within the configured nat-vent hysteresis band of the comfort edge -- i.e. the deviation is the system's own designed-in cycling tolerance, not a failure to hold the band. It does not add a sustained-duration tracker and does not change the underlying >0.5 F trigger threshold; it only prevents a false-positive incident (and the identical blind spot in `comfort_violations_minutes` accumulation, which feeds `comfort_score`; since Issue #1107 that accumulation passes the resolved target band in effect, the same range it scores against) during correct, expected WHF/fan cycling.

**Across loop runs:** `tools/simulation_loop.py` maintains `results/processed_incidents.json`, a list of `incident_id` values already processed. Before building a BSpec for an incident, the loop checks this file. Incidents with a matching `incident_id` are skipped, regardless of when the loop last ran.

**`grace_churn` (Issues #1103/#1104):** payload `trigger`, `count`, `window_minutes` (60), `first_time`, `last_time`, `fan_mode_config` and `last_fan_event` (`type`/`time` of the most recent `fan_*` Activity Log event, added by `coordinator._emit_event`, or `null`). Only automation-sourced graces count (`source == "automation"`); manual graces never do. Per-trigger dedup: one incident per trigger per 6 hours. It is a symptom detector -- it names the loop, not its cause -- so the `last_fan_event` field is what points an investigator at the fan.

**Episode-gated proactive class (Issue #1049):** `hvac_command_failed` is announced once per failure episode (state `_hvac_cmd_fail_announced` / `_hvac_cmd_fail_since` on the engine), not once per failing command; `hvac_command_recovered` closes it.

**Proactive class:** `setpoint_mode_inconsistency` fires per command call, not per cycle. If a single coordinator cycle issues multiple inconsistent setpoints (rare), one event is emitted per command.

**Historical:** A proactive `fan_rapid_cycling` class was briefly added in Issue #641 to detect when the rate-limit backstop suppressed a fan command, but removed in #649. A suppressed-and-deferred toggle is the backstop working correctly, not an anomaly — it is now surfaced as a `fan_toggle_deferred`-flavored event payload plus a WHF status-card suffix instead of an incident.

---

## Detection Code Locations

| Class | Where it fires | Code location |
|---|---|---|
| `setpoint_mode_inconsistency` | Before HA service call | `automation.py _set_temperature()` |
| All others | Post-cycle | `coordinator.py _detect_and_emit_incidents()` |

The `incident_detected` event is appended to `coordinator._event_log` in both cases. It is visible in the event log API at `GET /api/climate_advisor/event_log`.
