<!-- Nav: <- Context: [Simulation Feedback Loop](simulation-feedback-loop.md) | -> Detail: coordinator.py (_detect_and_emit_incidents) · automation.py (_set_temperature) -->

# Incident Classes -- Reference Table

## Anchors

| Question | Short answer (<= 2 sentences) | -> Full answer |
|---|---|---|
| Which incident class is proactive (fires at command time)? | Only `setpoint_mode_inconsistency` is proactive -- it fires inside automation.py _set_temperature() before the HA service call. All other classes are reactive and fire post-cycle in coordinator._detect_and_emit_incidents. | [Proactive vs. Reactive column below](#incident-class-reference) |
| What is the `incident_detected` event payload schema? | `type`, `time`, `incident_class`, `incident_id`, `indoor_f`, `outdoor_f`, `hvac_mode`, `comfort_cool`, `comfort_heat`, plus class-specific fields (`nat_vent_active`, `manual_override_active`, `occupancy_mode`, `setpoint_f`). | [Event Payload](#event-payload) |
| Does `comfort_violation`/`comfort_undertemp` require a sustained 15-min deviation? | No -- both are a **point-in-time** check (>0.5 F past the comfort band edge) with a 30-min incident-dedup window, not a duration tracker; as of Issue #411 they also skip emission when the deviation is within nat-vent-cycling tolerance (`coordinator._is_nat_vent_tolerated_deviation()`). | [Incident Class Reference below](#incident-class-reference) |
| Which classes were added for bugs #220-222? | Four new classes: `setpoint_mode_inconsistency` (#222 -- silent wrong setpoint), `rapid_override_after_automation` (#221 -- false override detection), `occupancy_transition` (#220 -- coverage gap), `override_active_on_occupancy` (compound condition at occupancy change). | [Bug Motivation column below](#incident-class-reference) |
| How are incidents deduplicated within a cycle? | Each reactive class is emitted at most once per 30-min coordinator cycle. Proactive `setpoint_mode_inconsistency` fires once per command call (only when inconsistency is detected). The loop tool cross-references `results/processed_incidents.json` to avoid re-processing the same incident_id. | [Deduplication](#deduplication) |
| Which classes have HIGH silent failure risk? | `setpoint_mode_inconsistency` (wrong setpoint in wrong HVAC mode; no comfort violation for hours) and `rapid_override_after_automation` (false override detection blocks automation silently). | [Silent Failure Risk column below](#incident-class-reference) |

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

All numeric fields are in Fahrenheit regardless of the user's display unit setting. `setpoint_f` is the applied setpoint (relevant for `setpoint_mode_inconsistency`; null for other classes where no setpoint was issued). `nat_vent_active`, `manual_override_active`, `occupancy_mode` provide context for all classes -- they reflect coordinator state at the time of detection. As of Issue #411, `comfort_undertemp` also carries `nat_vent_active` (matching `comfort_violation`, which already had it), so a genuine sustained violation that fires *during* an active nat-vent session is still visible with full context, even though in-tolerance deviations during nat-vent cycling no longer emit an incident at all.

---

## Deduplication

**Within a cycle:** `_detect_and_emit_incidents()` runs once per 30-min coordinator cycle. Each reactive class is checked once per call and emitted at most once. If the same class condition persists across multiple cycles, one `incident_detected` event is emitted per cycle.

**Nat-vent-tolerance gate (Issue #411):** `comfort_violation`/`comfort_undertemp` additionally call `coordinator._is_nat_vent_tolerated_deviation(indoor, comfort_heat, comfort_cool)` before emitting. This returns `True` (suppress) only when `automation_engine._natural_vent_active` is `True` **and** indoor is within the configured nat-vent hysteresis band of the comfort edge -- i.e. the deviation is the system's own designed-in cycling tolerance, not a failure to hold the band. It does not add a sustained-duration tracker and does not change the underlying >0.5 F trigger threshold; it only prevents a false-positive incident (and the identical blind spot in `comfort_violations_minutes` accumulation, which feeds `comfort_score`) during correct, expected WHF/fan cycling.

**Across loop runs:** `tools/simulation_loop.py` maintains `results/processed_incidents.json`, a list of `incident_id` values already processed. Before building a BSpec for an incident, the loop checks this file. Incidents with a matching `incident_id` are skipped, regardless of when the loop last ran.

**Proactive class:** `setpoint_mode_inconsistency` fires per command call, not per cycle. If a single coordinator cycle issues multiple inconsistent setpoints (rare), one event is emitted per command.

---

## Detection Code Locations

| Class | Where it fires | Code location |
|---|---|---|
| `setpoint_mode_inconsistency` | Before HA service call | `automation.py _set_temperature()` |
| All others | Post-cycle | `coordinator.py _detect_and_emit_incidents()` |

The `incident_detected` event is appended to `coordinator._event_log` in both cases. It is visible in the event log API at `GET /api/climate_advisor/event_log`.
