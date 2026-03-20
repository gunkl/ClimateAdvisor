# Climate Advisor — Computation Reference

This document is the authoritative reference for every formula, threshold, and decision table used by Climate Advisor to automate HVAC control. It covers day classification, trend analysis, temperature setpoints, occupancy logic, window management, the economizer, fan control, door/window pausing, grace periods, and all configurable defaults.

For structural context — how these computations fit into the coordinator, automation engine, and classifier modules — see [`docs/02-ARCHITECTURE-REFERENCE.md`](02-ARCHITECTURE-REFERENCE.md).

All temperatures are in **Fahrenheit**.

---

## 1. Day Classification

Today's high temperature is compared against fixed thresholds to assign a `day_type`. All downstream decisions (HVAC mode, setpoints, window advice, pre-conditioning) flow from this classification.

| today_high condition | day_type | HVAC mode | Constant name |
|---|---|---|---|
| `today_high >= 85` | `hot` | `cool` | `THRESHOLD_HOT = 85` |
| `75 <= today_high < 85` | `warm` | `off` | `THRESHOLD_WARM = 75` |
| `60 <= today_high < 75` | `mild` | `off` | `THRESHOLD_MILD = 60` |
| `45 <= today_high < 60` | `cool` | `heat` | `THRESHOLD_COOL = 45` |
| `today_high < 45` | `cold` | `heat` | _(below all thresholds)_ |

---

## 2. Trend Computation

The trend is computed from the difference between tomorrow's and today's forecast highs and lows:

```
avg_delta = ((tomorrow_high - today_high) + (tomorrow_low - today_low)) / 2
trend_magnitude = abs(avg_delta)
```

| avg_delta condition | trend_direction |
|---|---|
| `avg_delta > 2` | `warming` |
| `avg_delta < -2` | `cooling` |
| `-2 <= avg_delta <= 2` | `stable` |

---

## 3. Setback Modifier

The setback modifier adjusts how aggressively the system setbacks or pre-conditions based on the incoming trend. It is applied on top of base setback values during occupancy-away, vacation, and bedtime calculations (see Section 5).

| trend_direction | trend_magnitude condition | setback_modifier | pre_condition_target | Notes |
|---|---|---|---|---|
| `cooling` | `magnitude >= 10` (significant) | `+3.0` | `+3.0°F above comfort_heat` | Big cold front — don't set back far, pre-heat |
| `cooling` | `5 <= magnitude < 10` (moderate) | `+2.0` | `+2.0°F above comfort_heat` | Moderate cold front — slight pre-heat |
| `stable` | any | `0` | none | No adjustment |
| `warming` | `5 <= magnitude < 10` (moderate) | `-2.0` | none | Warming coming — set back further tonight |
| `warming` | `magnitude >= 10` (significant) | `-3.0` | none | Strong warming — aggressive setback tonight |

Threshold constants: `TREND_THRESHOLD_SIGNIFICANT = 10`, `TREND_THRESHOLD_MODERATE = 5`.

---

## 4. Pre-Conditioning

Pre-conditioning sets the HVAC system up ahead of an expected temperature change.

| Trigger | Target temperature formula | When applied |
|---|---|---|
| Hot day (`day_type == hot`) | `comfort_cool + (-2)` = `comfort_cool - 2` | At classification time (morning) |
| Moderate cold front (`cooling`, magnitude 5–9°F) | `comfort_heat + 2.0` | Scheduled at 7:00 PM |
| Significant cold front (`cooling`, magnitude ≥ 10°F) | `comfort_heat + 3.0` | Scheduled at 7:00 PM |

**Hot-day pre-cool detail:** The `pre_condition_target` is stored as `-2.0` (a negative offset). `_set_temperature_for_mode()` applies it as `comfort_cool + pre_condition_target`, so a `comfort_cool` of 75°F yields a pre-cool target of **73°F**.

**Cold-front pre-heat detail:** The pre-heat target is stored in `config["_pending_preheat"]` for the coordinator to schedule. The target is `comfort_heat + pre_condition_target` (e.g., 70 + 3 = **73°F** for a significant cold front).

---

## 5. Temperature Setpoints by Context

Default values used in examples: `comfort_heat = 70`, `comfort_cool = 75`, `setback_heat = 60`, `setback_cool = 80`.

| Context | Heat Mode Formula | Cool Mode Formula | Example (heat) | Example (cool) |
|---|---|---|---|---|
| Home (comfort) | `comfort_heat` | `comfort_cool` | 70°F | 75°F |
| Away | `setback_heat + setback_modifier` | `setback_cool - setback_modifier` | 60°F (modifier=0) | 80°F (modifier=0) |
| Vacation | `setback_heat + setback_modifier - VACATION_SETBACK_EXTRA` | `setback_cool - setback_modifier + VACATION_SETBACK_EXTRA` | 57°F (modifier=0) | 83°F (modifier=0) |
| Guest | Same as Home — dispatches to `handle_occupancy_home()` | Same as Home | 70°F | 75°F |
| Bedtime | `comfort_heat - 4 + setback_modifier` | `comfort_cool + 3` | 66°F (modifier=0) | 78°F |
| Morning Wakeup | `comfort_heat` | `comfort_cool` | 70°F | 75°F |
| Pre-cool (hot day) | n/a | `comfort_cool - 2` | n/a | 73°F |
| Pre-heat (cold front, moderate) | `comfort_heat + 2` | n/a | 72°F | n/a |
| Pre-heat (cold front, significant) | `comfort_heat + 3` | n/a | 73°F | n/a |

**Notes:**
- Bedtime cool does **not** use `setback_modifier` — it is always `comfort_cool + 3`.
- Bedtime heat **does** use `setback_modifier` — `comfort_heat - 4 + setback_modifier`.
- `VACATION_SETBACK_EXTRA = 3` degrees beyond the normal setback.
- Guest mode calls `handle_occupancy_home()` directly — no separate handler.

---

## 6. Occupancy Mode Priority

When multiple toggles are active simultaneously, the highest-priority mode wins.

| Priority | Mode | Handler called | Behavior |
|---|---|---|---|
| 1 (highest) | `guest` | `handle_occupancy_home()` | Comfort temps — guests always get full comfort |
| 2 | `vacation` | `handle_occupancy_vacation()` | Deep setback (`VACATION_SETBACK_EXTRA` beyond normal away) |
| 3 | `away` | `handle_occupancy_away()` | Normal setback |
| 4 (lowest) | `home` | `handle_occupancy_home()` | Comfort temps restored |

**Toggle resolution logic:**
1. Read home, vacation, and guest toggle entities (respecting any invert flags).
2. If **guest** toggle is on → mode = `guest`.
3. Else if **vacation** toggle is on → mode = `vacation`.
4. Else if **home** toggle is **off** → mode = `away`.
5. Else → mode = `home`.

---

## 7. Window Recommendations

Window advice is set by the classifier at classification time, based on `day_type` and forecast lows.

| Day Type | Windows Recommended? | Open Time | Close Time | Condition |
|---|---|---|---|---|
| `hot` | Not a traditional recommendation — window *opportunities* only | 6:00 AM | 9:00 AM | Morning opportunity: `today_low <= 80` |
| `hot` | Evening opportunity | 5:00 PM | Midnight (00:00) | Evening opportunity: `tomorrow_low <= 80` |
| `warm` | Yes (if condition met) | 6:00 AM | 10:00 AM | `today_low <= comfort_cool - ECONOMIZER_TEMP_DELTA` = `today_low <= 72°F` (defaults) |
| `mild` | Always yes | 10:00 AM | 5:00 PM | No condition — always recommended |
| `cool` | No | — | — | — |
| `cold` | No | — | — | — |

**Warm-day window condition formula:** `today_low <= DEFAULT_COMFORT_COOL - ECONOMIZER_TEMP_DELTA` = `75 - 3 = 72°F` at defaults. Constant: `WARM_WINDOW_OPEN_HOUR = 6`, `WARM_WINDOW_CLOSE_HOUR = 10`.

---

## 8. Economizer (Window Cooling on Hot Days)

The economizer is a two-phase strategy that uses open windows to reduce AC load on hot days.

### Eligibility

All of the following must be true simultaneously:

| Condition | Formula / Value |
|---|---|
| Day type | `day_type == hot` |
| Windows open | `windows_physically_open == True` |
| Outdoor temp | `outdoor_temp <= comfort_cool + ECONOMIZER_TEMP_DELTA` = `outdoor_temp <= 78°F` (defaults) |
| Time window | 6:00–9:00 AM **or** 5:00 PM–midnight |

### Phase Behavior

| Mode | aggressive_savings | Phase | Condition | Action |
|---|---|---|---|---|
| Normal | `False` | Phase 1: cool-down | `indoor_temp > comfort_cool` | Set HVAC to `cool`, target = `comfort_cool`; outdoor air assists efficiency |
| Normal | `False` | Phase 2: maintain | `indoor_temp <= comfort_cool` | Set HVAC to `off`; activate fan for ventilation |
| Savings | `True` | Maintain only (skip Phase 1) | Any eligible condition | Set HVAC to `off` immediately; activate fan; no AC assist |

When the economizer deactivates (conditions no longer met), the fan is turned off and HVAC resumes normal `cool` mode at `comfort_cool`.

---

## 9. Fan Control

Fans only activate during the economizer **maintain** phase (Phase 2 or savings-mode ventilation). Fan behavior is controlled by the `fan_mode` config setting.

| fan_mode value | Activate action | Deactivate action |
|---|---|---|
| `disabled` | No action | No action |
| `whole_house_fan` | `turn_on` the configured `fan_entity` (using the entity's own domain — `fan` or `switch`) | `turn_off` the configured `fan_entity` |
| `hvac_fan` | `climate.set_fan_mode` → `"on"` on the thermostat entity | `climate.set_fan_mode` → `"auto"` on the thermostat entity |
| `both` | Both `whole_house_fan` and `hvac_fan` actions | Both deactivate actions |

---

## 10. Door/Window HVAC Pause

| Step | Behavior |
|---|---|
| Sensor opens | Debounce timer starts (`DEFAULT_SENSOR_DEBOUNCE_SECONDS = 300s / 5 min`, configurable) |
| During debounce | No HVAC action taken |
| Debounce expires (sensor still open) | HVAC mode saved as `pre_pause_mode`; HVAC set to `off`; notification sent |
| Grace period active at debounce expiry | Pause **blocked** — no HVAC change, log message only |
| HVAC already `off` at pause time | No action (nothing to pause) |
| All monitored sensors close | Restore HVAC to `pre_pause_mode`; restore comfort temperature; start **automation** grace period |
| User manually turns HVAC on during pause | Clears pause state; starts **manual** grace period; manual override activated |

---

## 11. Grace Periods

| Type | Trigger | Default Duration | Configurable? | Effect | Notify on Expiry (default) |
|---|---|---|---|---|---|
| Manual | User overrides thermostat (including during a sensor pause) | `1800s` (30 min) | Yes — `CONF_MANUAL_GRACE_PERIOD` | Blocks door/window sensor from re-pausing HVAC; classification skips HVAC mode changes | No (`CONF_MANUAL_GRACE_NOTIFY = False`) |
| Automation | Climate Advisor resumes HVAC after all sensors close | `3600s` (60 min) | Yes — `CONF_AUTOMATION_GRACE_PERIOD` | Blocks door/window sensor from immediately re-pausing HVAC | Yes (`CONF_AUTOMATION_GRACE_NOTIFY = True`) |

Both grace periods are cancelled and reset on HA restart. Only one grace timer of each type is active at a time; starting a new one cancels the previous.

---

## 12. "Prefer Savings Over Comfort" (aggressive_savings)

The `aggressive_savings` flag currently affects one system:

| System | Normal (False) | Savings (True) |
|---|---|---|
| Economizer | Two-phase: AC cool-down first, then ventilation-only maintain | Skip AC entirely — go straight to ventilation-only maintain phase |

Future versions may extend `aggressive_savings` to apply more aggressive setback values. At this time, setback formulas are identical regardless of this flag.

---

## 13. Defaults Reference

Complete list of all constants from `const.py` that affect runtime behavior.

| Constant Name | Default Value | Unit | Description |
|---|---|---|---|
| `DEFAULT_COMFORT_HEAT` | `70` | °F | Heating target when home/comfort |
| `DEFAULT_COMFORT_COOL` | `75` | °F | Cooling target when home/comfort |
| `DEFAULT_SETBACK_HEAT` | `60` | °F | Heating target when away |
| `DEFAULT_SETBACK_COOL` | `80` | °F | Cooling target when away |
| `THRESHOLD_HOT` | `85` | °F | today_high threshold for `hot` day type |
| `THRESHOLD_WARM` | `75` | °F | today_high threshold for `warm` day type |
| `THRESHOLD_MILD` | `60` | °F | today_high threshold for `mild` day type |
| `THRESHOLD_COOL` | `45` | °F | today_high threshold for `cool` day type |
| `TREND_THRESHOLD_SIGNIFICANT` | `10` | °F | avg_delta magnitude for significant trend |
| `TREND_THRESHOLD_MODERATE` | `5` | °F | avg_delta magnitude for moderate trend |
| `VACATION_SETBACK_EXTRA` | `3` | °F | Extra setback depth beyond normal away setback during vacation |
| `DEFAULT_SENSOR_DEBOUNCE_SECONDS` | `300` | seconds (5 min) | Door/window must stay open this long before HVAC pauses |
| `DEFAULT_MANUAL_GRACE_SECONDS` | `1800` | seconds (30 min) | Duration of manual grace period after user override |
| `DEFAULT_AUTOMATION_GRACE_SECONDS` | `3600` | seconds (60 min) | Duration of automation grace period after HVAC resumes |
| `ECONOMIZER_TEMP_DELTA` | `3` | °F | Outdoor temp must be within this delta of comfort_cool for economizer eligibility |
| `ECONOMIZER_MORNING_START_HOUR` | `6` | hour (24h) | Economizer morning window start |
| `ECONOMIZER_MORNING_END_HOUR` | `9` | hour (24h) | Economizer morning window end |
| `ECONOMIZER_EVENING_START_HOUR` | `17` | hour (24h) | Economizer evening window start (5 PM) |
| `ECONOMIZER_EVENING_END_HOUR` | `24` | hour (24h) | Economizer evening window end (midnight) |
| `WARM_WINDOW_OPEN_HOUR` | `6` | hour (24h) | Warm-day window open time |
| `WARM_WINDOW_CLOSE_HOUR` | `10` | hour (24h) | Warm-day window close time |
| `REVISIT_DELAY_SECONDS` | `300` | seconds (5 min) | Follow-up re-evaluation delay after any HVAC action |
| `OCCUPANCY_SETBACK_MINUTES` | `15` | minutes | Reserved — occupancy setback lead time |
| `MAX_CONTINUOUS_RUNTIME_HOURS` | `3` | hours | Reserved — maximum continuous HVAC runtime guard |
| `SUGGESTION_COOLDOWN_DAYS` | `7` | days | Learning engine: minimum days between repeat suggestions |
| `MIN_DATA_POINTS_FOR_SUGGESTION` | `14` | data points | Learning engine: minimum records before generating suggestions |
| `COMPLIANCE_THRESHOLD_LOW` | `0.3` | ratio | Learning engine: below 30% compliance triggers a suggestion |
| `COMPLIANCE_THRESHOLD_HIGH` | `0.8` | ratio | Learning engine: above 80% compliance means advice is working |
| `DEFAULT_FAN_MODE` | `disabled` | — | Fan control default (no fan control) |

---

_Last Updated: 2026-03-19_
