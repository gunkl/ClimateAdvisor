# Climate Advisor — Computation Reference

This document is the authoritative reference for every formula, threshold, and decision table used by Climate Advisor to automate HVAC control. It covers day classification, trend analysis, temperature setpoints, occupancy logic, window management, the economizer, fan control, door/window pausing, grace periods, and all configurable defaults.

For structural context — how these computations fit into the coordinator, automation engine, and classifier modules — see [`docs/02-ARCHITECTURE-REFERENCE.md`](02-ARCHITECTURE-REFERENCE.md).

### Temperature Units

- All internal thresholds and calculations use **Fahrenheit as the canonical unit** (e.g., `THRESHOLD_HOT = 85`, `comfort_heat = 70`).
- The `temp_unit` config key controls the display unit (`fahrenheit` or `celsius`, default: `fahrenheit`).
- Temperatures received from Home Assistant (weather entity forecast highs/lows, indoor/outdoor sensor readings) are **automatically converted to °F** before any classification, trend, or setpoint calculation.
- Temperatures sent to Home Assistant (thermostat setpoints via `climate.set_temperature`) are **converted back to the user's chosen unit** before the service call.
- Briefings and log messages display temperatures in the user's chosen unit.

The automation logic table and all threshold constants in this document are expressed in °F. The unit conversion layer is transparent to all downstream logic — automation behavior is identical regardless of which display unit the user has selected.

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
| Bedtime | `compute_bedtime_setback()` (see §5a) | `compute_bedtime_setback()` (see §5a) | 66°F (modifier=0, no model) | 78°F (no model) |
| Morning Wakeup | `comfort_heat` | `comfort_cool` | 70°F | 75°F |
| Pre-cool (hot day) | n/a | `comfort_cool - 2` | n/a | 73°F |
| Pre-heat (cold front, moderate) | `comfort_heat + 2` | n/a | 72°F | n/a |
| Pre-heat (cold front, significant) | `comfort_heat + 3` | n/a | 73°F | n/a |

**Notes:**
- Bedtime setback depth is now computed by `compute_bedtime_setback()` in `automation.py` (see §5a). The hardcoded `−4°F / +3°F` defaults remain as fallbacks when the thermal model confidence is `"none"`.
- Bedtime cool still applies the same `+3°F` offset logic at default; when the thermal model is active, the depth is scaled to ensure the house warms/cools back to comfort within the overnight recovery window.
- Bedtime heat continues to incorporate `setback_modifier` on top of the computed depth.
- `VACATION_SETBACK_EXTRA = 3` degrees beyond the normal setback.
- Guest mode calls `handle_occupancy_home()` directly — no separate handler.
- Morning wakeup is skipped when occupancy is `away` or `vacation` (Issue #85).
- Bedtime setback is skipped when occupancy is `vacation` (vacation setback is deeper).
- The daily briefing TLDR table shows setback temps and an occupancy status row when not home.

### 5a. Adaptive Bedtime Setback (`compute_bedtime_setback()`)

Bedtime setback depth is computed from the thermal model HVAC rates and the overnight recovery window:

| Condition | Heat Mode | Cool Mode |
|---|---|---|
| Thermal model confidence is `"none"` | Fall back to `DEFAULT_SETBACK_DEPTH_F = 4°F` below `comfort_heat` | Fall back to `DEFAULT_SETBACK_DEPTH_COOL_F = 3°F` above `comfort_cool` |
| Model available | Depth = `heating_rate_f_per_hour` × recovery_window_hours; clamped to `[MIN_SETBACK_DEPTH, MAX_SETBACK_DEPTH]` | Same formula using `cooling_rate_f_per_hour` |

`heating_rate_f_per_hour` and `cooling_rate_f_per_hour` are the legacy alias fields returned by `get_thermal_model()` — they equal `abs(k_active_heat)` and `abs(k_active_cool)` respectively. Both are `None` when no model data is available, which triggers the fallback.

`setback_modifier` is always added to the heat setback result regardless of whether the model or the fallback was used.

### 5b. Adaptive Pre-heat Start Time

The pre-heat start time is computed from the thermal model heating rate and the temperature delta to be recovered:

| Condition | Pre-heat Start |
|---|---|
| No model data (`heating_rate_f_per_hour` is `None`) | Fall back to `DEFAULT_PREHEAT_MINUTES = 120` before wakeup |
| Model available | `minutes = (temp_delta / heating_rate_f_per_hour) × 60 × 1.3` (1.3× safety margin); clamped to `[MIN_PREHEAT_MINUTES=30, MAX_PREHEAT_MINUTES=240]` |

The temperature delta is `comfort_heat − bedtime_setpoint`. The safety margin of 1.3× ensures the house reaches comfort even on colder-than-average mornings.

### 5c. Predicted Temperature Graph — Physics Path

From Issue #114, when the thermal model has confidence ≥ `"low"` and `k_passive < 0`, the dashboard temperature forecast uses the ODE analytical solution to simulate future indoor temperatures instead of simple ramp interpolation:

```
T(t+dt) = T_outdoor + (T - T_outdoor) * exp(k_p * dt) + (Q/k_p) * (exp(k_p * dt) - 1)
```

`_simulate_indoor_physics()` in `coordinator.py` implements one ODE time step. `_build_predicted_indoor_future()` drives the simulation forward through the schedule, switching `Q` between `k_active_heat`, `k_active_cool`, and `0` depending on the HVAC mode in each period.

**Fallback (ramp interpolation):** When model confidence is `"none"` or `k_passive` is unavailable/non-negative, the legacy ramp path runs:

| Condition | Ramp Duration |
|---|---|
| No model data | Default 30-minute ramp |
| Model available (legacy path only) | `ramp_hours = temp_delta / rate`; minimum 15 minutes; computed by `_compute_ramp_hours()` |

`_compute_ramp_hours()` uses whichever rate applies to the transition direction (heating rate for rising ramps, cooling rate for falling ramps).

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

### 6a. Occupancy-Aware Automation Guards (Issue #85)

The automation engine tracks `_occupancy_mode` internally (synced by the coordinator). All temperature-setting code paths check occupancy before applying comfort temps:

| Code Path | Home/Guest | Away | Vacation |
|---|---|---|---|
| `apply_classification()` (30-min cycle) | Apply comfort temps | Reapply away setback | Skip entirely |
| `handle_morning_wakeup()` | Restore comfort | Skip (no wakeup) | Skip (no wakeup) |
| `handle_bedtime()` | Apply bedtime setback | Apply bedtime setback | Skip (vacation setback preserved) |
| `_set_temperature_for_mode()` (safety net) | Apply comfort | Redirect → `handle_occupancy_away()` | Redirect → `handle_occupancy_vacation()` |

The `_set_temperature_for_mode()` safety net catches all indirect callers (door/window resume, grace expiry, economizer deactivation) so comfort temps are never applied while away/vacation.

**Test coverage:** `tests/test_occupancy_automation.py` — 18 tests covering all cells above.

### 6b. Warm-Day Comfort-Floor Guard

When `apply_classification()` runs and the day type is `warm` or `hot` with HVAC mode `off`, the automation engine applies a comfort-floor guard before executing the shutoff. The guard fires on every 30-minute coordinator update until the indoor temperature has risen to the comfort floor.

| Condition | Action | Event emitted |
|---|---|---|
| `day_type in (warm, hot)` AND `indoor_temp < comfort_heat` | Set HVAC to `heat`, target = `comfort_heat` | `warm_day_comfort_gap` |
| `day_type in (warm, hot)` AND `indoor_temp >= comfort_heat` | Set HVAC to `off` as normal | — |
| `day_type in (warm, hot)` AND indoor temp unavailable | Set HVAC to `off` as normal (fail safe) | — |

**Why this guard exists:** Without it, the daily warm-day shutoff can leave the home 2–3°F below the comfort floor all morning. This accumulates comfort violations and depresses `comfort_score` even though the system was technically following the warm-day classification correctly.

**Interaction with occupancy guards:** The comfort-floor heat command goes through `_set_temperature_for_mode()`, so occupancy-away and vacation redirection (§6a) still applies — the guard will not heat to `comfort_heat` if the occupancy mode is `away` or `vacation`.

**30-minute convergence:** `apply_classification()` is called on every coordinator update (every 30 min). Once indoor temperature reaches `comfort_heat`, the guard condition is no longer met and the next update sets HVAC off normally. No separate timer is needed.

**Test coverage:** `tests/test_warm_day_comfort_gap.py`

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

### 9a. Fan State Tracking

The coordinator maintains five internal fields to manage fan state across activate/deactivate calls and detect user overrides:

| Field | Type | Purpose |
|---|---|---|
| `_fan_active` | `bool` | Whether the integration currently considers the fan on |
| `_fan_on_since` | `datetime \| None` | Timestamp of when `_activate_fan()` last turned the fan on |
| `_fan_override_active` | `bool` | Whether a user manual fan override is in effect |
| `_fan_override_time` | `datetime \| None` | Timestamp of when the fan override was detected |
| `_fan_command_pending` | `bool` | Set to `True` immediately before the integration issues a fan command; cleared immediately after |

**`_activate_fan()`** sets `_fan_command_pending = True`, issues the fan-on service call, then sets `_fan_active = True` and records `_fan_on_since`. If `_fan_override_active` is `True` at activation time, the call is skipped so the integration does not fight the user's manual setting.

**`_deactivate_fan()`** follows the same pattern in reverse: sets `_fan_command_pending = True`, issues the fan-off service call, then clears `_fan_active` and `_fan_on_since`. Override state is not checked on deactivation — the intent is always to stop the fan when the economizer or transition logic calls for it.

### 9b. Fan Override Detection

Fan override detection runs in two places:

1. **`_async_fan_entity_changed()`** — a state-change listener registered on the `fan_entity` (for `fan_mode == whole_house_fan` or `both`). When the entity state changes, the listener checks whether `_fan_command_pending` is set. If the flag is clear, the state change was user-initiated, not integration-initiated, and a fan override is recorded: `_fan_override_active = True`, `_fan_override_time = utcnow()`.

2. **`_async_thermostat_changed()`** — the existing thermostat state listener is extended to also inspect the thermostat's `fan_mode` attribute (for `fan_mode == hvac_fan` or `both`). If the fan_mode attribute changes while `_fan_command_pending` is clear, a fan override is recorded using the same fields.

Fan override is **separate** from HVAC override. The two override states are tracked independently and do not interfere with each other. Fan override uses the same grace period duration as manual HVAC override (`DEFAULT_MANUAL_GRACE_SECONDS`), but the timers run independently.

Fan override is **cleared** at transition points where the integration takes deliberate control of the fan (bedtime, morning wakeup — see Section 9c).

### 9c. Fan Behavior at Transitions

| Transition | Fan action | Override cleared? |
|---|---|---|
| Bedtime | `_deactivate_fan()` called; economizer also deactivated | Yes — `_fan_override_active` reset to `False` |
| Morning wakeup | `_deactivate_fan()` called | Yes — `_fan_override_active` reset to `False` |

At bedtime, both the fan and the economizer are explicitly shut down before the bedtime setpoints are applied. This ensures the overnight period starts with a clean fan state regardless of what the economizer was doing during the evening window. At morning wakeup, the fan is deactivated before comfort temperatures are restored, preventing carryover of an economizer fan session into the occupied-home daytime period.

Clearing the override flag at these transitions means the integration will not skip fan activation during the next economizer cycle just because the user had manually adjusted the fan during the previous day.

### 9d. Fan Status Sensor Values

The `sensor.climate_advisor_fan_status` entity exposes one of five state strings:

| Sensor state | Meaning |
|---|---|
| `disabled` | Fan control is not configured (`fan_mode = disabled`) |
| `inactive` | Fan is off; integration is in control |
| `active` | Fan is on; integration activated it (economizer maintain phase) |
| `override — on` | Fan is on; user turned it on manually — integration standing down |
| `override — off` | Fan is off; user turned it off manually — integration standing down |

The sensor also exposes these attributes:
- `fan_runtime_minutes` — minutes since the integration last activated the fan (0.0 when inactive or in override)
- `fan_override_since` — ISO timestamp of when the manual override was detected (`null` when no override is active)
- `fan_running` — boolean; `true` when the fan is physically running regardless of who controls it

**HVAC-off + fan-on (fan-only circulation):** When the economizer enters the maintain phase, HVAC mode is set to `off` but `climate.set_fan_mode: on` is called separately. This is the intended "fan-only circulation" mode — most thermostats support running the fan for air circulation independently of heating or cooling. A `DEBUG`-level log entry is emitted whenever the integration activates the HVAC fan while the thermostat reports `hvac_mode = off`.

---

## 10. Door/Window HVAC Pause

| Step | Behavior |
|---|---|
| Sensor opens | Debounce timer starts (`DEFAULT_SENSOR_DEBOUNCE_SECONDS = 300s / 5 min`, configurable) |
| During debounce | No HVAC action taken |
| Debounce expires (sensor still open) | `_hvac_command_pending` set; HVAC mode saved as `pre_pause_mode`; HVAC set to `off`; notification sent |
| Grace period active at debounce expiry | Pause **blocked** — no HVAC change, log message only |
| HVAC already `off` at pause time | No action (nothing to pause) |
| All monitored sensors close | Restore HVAC to `pre_pause_mode`; restore comfort temperature; start **automation** grace period |
| User manually turns HVAC on during pause | Clears pause state; starts **manual** grace period; manual override activated |
| User clicks "Resume HVAC (override pause)" button | Clears pause state; restores classification's recommended HVAC mode; starts **manual** grace period; status set to `"resumed — door/window override"` |
| `_hvac_command_pending` flag | Set `True` before any system-issued HVAC service call (including pause set-to-off); prevents `_async_thermostat_changed()` from misidentifying the system's own change as a user manual override. Cleared after the service call completes. `_hvac_command_time` records the timestamp of the command for additional recency checks. |

---

## 11. Grace Periods

| Type | Trigger | Default Duration | Configurable? | Effect | Notify on Expiry (default) |
|---|---|---|---|---|---|
| Manual | User overrides thermostat (including during a sensor pause) or clicks "Resume HVAC (override pause)" | `1800s` (30 min) | Yes — `CONF_MANUAL_GRACE_PERIOD` | Blocks door/window sensor from re-pausing HVAC; classification skips HVAC mode changes | No (`CONF_MANUAL_GRACE_NOTIFY = False`) |
| Automation | Climate Advisor resumes HVAC after all sensors close | `300s` (5 min) | Yes — `CONF_AUTOMATION_GRACE_PERIOD` | Blocks door/window sensor from immediately re-pausing HVAC | Yes (`CONF_AUTOMATION_GRACE_NOTIFY = True`) |

Both grace periods are cancelled and reset on HA restart. Only one grace timer of each type is active at a time; starting a new one cancels the previous.

**Grace expiry sensor re-check:** When either grace period expires, the system re-checks whether any monitored contact sensor is currently open. If one or more sensors are still open, HVAC is re-paused immediately (`_paused_by_door = True`, HVAC set to `off`) rather than restoring normal automation. This prevents the safety issue of running HVAC with a door or window open after the grace window closes.

### Startup Override Logic

On first data update after startup, Climate Advisor checks whether the HVAC's current mode matches the day classification's recommended mode before setting a manual override:

| HVAC state | Classification recommends | Result |
|---|---|---|
| `off` / `unavailable` / `unknown` | any | No override set |
| `heat` | `heat` | No override — modes match |
| `heat` | `cool` or `off` | Manual override set — respects current state |
| `cool` | `cool` | No override — modes match |
| `cool` | `heat` or `off` | Manual override set — respects current state |

This prevents unnecessary override lockouts after a Home Assistant restart when the HVAC is already in the mode that Climate Advisor would have set anyway. See Issue #42.

---

## 12. Revisit Mechanism

After any HVAC action (mode change or temperature set), the coordinator calls `_schedule_revisit()`, which posts a delayed `async_request_refresh()` for 5 minutes later (`REVISIT_DELAY_SECONDS = 300`). When the refresh fires, the full automation evaluation runs again — including re-checking eligibility for the economizer, any pending pre-conditioning, and the current occupancy and time context.

If that re-evaluation results in another HVAC action, `_schedule_revisit()` is called again, scheduling yet another follow-up 5 minutes out. The loop terminates naturally when an evaluation pass finds no action is needed. There is no explicit iteration cap; the exit condition is that the system has reached a stable state.

This mechanism ensures that a multi-step transition (for example: economizer detects indoor temp still high after fan activation, then re-evaluates whether to switch to Phase 1 AC assist) converges without requiring a separate scheduling path for each step. It also catches edge cases where conditions change in the minutes immediately following an automated action (e.g., a window is closed just after the economizer activated).

Only one pending revisit is active at a time. If `_schedule_revisit()` is called while a revisit is already scheduled, the previous scheduled call is cancelled and replaced by the new one.

---

## 13. Logging Level

HVAC action log statements use `_LOGGER.warning()` rather than `_LOGGER.info()`. This applies to the following operations:

- `_set_hvac_mode()` — mode changes (on, off, cool, heat)
- `_set_temperature()` — setpoint changes
- `_record_action()` — action history entries
- `handle_manual_override()` — override detection and grace period start
- `apply_classification()` — day classification application

Home Assistant's default log level for custom components is `warning`. Using `_LOGGER.info()` for these calls would make them invisible in the HA log under default settings, which makes diagnosing automation behavior in production impossible without a config change. Promoting these calls to `warning` means they appear in the log out of the box, without requiring the user to add a `logger:` block to `configuration.yaml`.

Routine diagnostic messages (coordinator polling, entity state reads, skip-due-to-grace-period notices) remain at `_LOGGER.debug()` and are suppressed under normal operation.

---

## 14. "Prefer Savings Over Comfort" (aggressive_savings)

The `aggressive_savings` flag currently affects one system:

| System | Normal (False) | Savings (True) |
|---|---|---|
| Economizer | Two-phase: AC cool-down first, then ventilation-only maintain | Skip AC entirely — go straight to ventilation-only maintain phase |

Future versions may extend `aggressive_savings` to apply more aggressive setback values. At this time, setback formulas are identical regardless of this flag.

---

## 15. Defaults Reference

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
| `DEFAULT_AUTOMATION_GRACE_SECONDS` | `300` | seconds (5 min) | Duration of automation grace period after HVAC resumes |
| `ECONOMIZER_TEMP_DELTA` | `3` | °F | Outdoor temp must be within this delta of comfort_cool for economizer eligibility |
| `ECONOMIZER_MORNING_START_HOUR` | `6` | hour (24h) | Economizer morning window start |
| `ECONOMIZER_MORNING_END_HOUR` | `9` | hour (24h) | Economizer morning window end |
| `ECONOMIZER_EVENING_START_HOUR` | `17` | hour (24h) | Economizer evening window start (5 PM) |
| `ECONOMIZER_EVENING_END_HOUR` | `24` | hour (24h) | Economizer evening window end (midnight) |
| `WARM_WINDOW_OPEN_HOUR` | `6` | hour (24h) | Warm-day window open time |
| `WARM_WINDOW_CLOSE_HOUR` | `10` | hour (24h) | Warm-day window close time |
| `REVISIT_DELAY_SECONDS` | `300` | seconds (5 min) | Follow-up re-evaluation delay after any HVAC action |
| `OCCUPANCY_SETBACK_MINUTES` | `15` | minutes | Delay before applying away setback temperature after departure |
| `MAX_CONTINUOUS_RUNTIME_HOURS` | `3` | hours | Reserved — maximum continuous HVAC runtime guard |
| `SUGGESTION_COOLDOWN_DAYS` | `7` | days | Learning engine: minimum days between repeat suggestions |
| `MIN_DATA_POINTS_FOR_SUGGESTION` | `14` | data points | Learning engine: minimum records before generating suggestions |
| `COMPLIANCE_THRESHOLD_LOW` | `0.3` | ratio | Learning engine: below 30% compliance triggers a suggestion |
| `COMPLIANCE_THRESHOLD_HIGH` | `0.8` | ratio | Learning engine: above 80% compliance means advice is working |
| `DEFAULT_FAN_MODE` | `disabled` | — | Fan control default (no fan control) |
| `DEFAULT_SETBACK_DEPTH_F` | `4` | °F | Bedtime heat setback depth fallback when thermal model confidence is `"none"` |
| `DEFAULT_SETBACK_DEPTH_COOL_F` | `3` | °F | Bedtime cool setback depth fallback when thermal model confidence is `"none"` |
| `DEFAULT_PREHEAT_MINUTES` | `120` | minutes | Pre-heat lead time fallback when no thermal model data |
| `MIN_PREHEAT_MINUTES` | `30` | minutes | Minimum clamped pre-heat lead time |
| `MAX_PREHEAT_MINUTES` | `240` | minutes | Maximum clamped pre-heat lead time |
| `THERMAL_POST_HEAT_TIMEOUT_MINUTES` | `45` | minutes | Maximum post-heat observation window before abandoning |
| `THERMAL_STABILIZATION_THRESHOLD_F` | `0.3` | °F | |ΔT| threshold for stabilization criterion |
| `THERMAL_STABILIZATION_WINDOW_MINUTES` | `5` | minutes | Duration |ΔT| must remain below threshold to count as stabilized |
| `THERMAL_SAMPLE_INTERVAL_SECONDS` | `60` | seconds | Active and post-heat sampling cadence |
| `THERMAL_PRE_HEAT_BUFFER_MINUTES` | `15` | minutes | Rolling pre-HVAC sample window included in k_passive regression |
| `THERMAL_MAX_ACTIVE_SAMPLES` | `120` | samples | Cap on active-phase samples (2 hours at 60s cadence) |
| `THERMAL_MAX_POST_HEAT_SAMPLES` | `45` | samples | Cap on post-heat samples (45 min at 60s cadence) |
| `THERMAL_MIN_R_SQUARED` | `0.2` | — | Minimum R² for k_passive OLS regression to accept an observation |
| `THERMAL_MIN_POST_HEAT_SAMPLES` | `10` | samples | Minimum post-heat samples required for regression |
| `THERMAL_K_PASSIVE_MIN` | `-0.5` | hr⁻¹ | Sanity lower bound for k_passive (very leaky envelope) |
| `THERMAL_K_PASSIVE_MAX` | `-0.001` | hr⁻¹ | Sanity upper bound for k_passive (very well insulated) |
| `THERMAL_K_ACTIVE_HEAT_MIN` | `0.5` | °F/hr | Minimum credible HVAC heating contribution |
| `THERMAL_K_ACTIVE_HEAT_MAX` | `15.0` | °F/hr | Maximum credible HVAC heating contribution |
| `THERMAL_K_ACTIVE_COOL_MIN` | `-15.0` | °F/hr | Maximum credible HVAC cooling contribution (magnitude) |
| `THERMAL_K_ACTIVE_COOL_MAX` | `-0.5` | °F/hr | Minimum credible HVAC cooling contribution (magnitude) |

**User-facing config keys** (set via config flow, stored in the config entry):

| Config Key | Default | Description |
|---|---|---|
| `temp_unit` | `fahrenheit` | Temperature unit for display and input (`fahrenheit` or `celsius`). All internal calculations use Fahrenheit as the canonical unit; this setting controls conversion at the HA boundary (inbound sensor readings and outbound thermostat setpoints) and the display unit in briefings and logs. |

**AI settings** (set via config flow, affect AI feature behavior):

| Constant Name | Default Value | Unit | Description |
|---|---|---|---|
| `DEFAULT_AI_ENABLED` | `False` | — | AI features disabled by default; user must opt in |
| `DEFAULT_AI_MODEL` | `"claude-sonnet-4-6"` | — | Claude model used for all AI requests |
| `DEFAULT_AI_REASONING_EFFORT` | `"medium"` | — | Reasoning effort level passed to the Claude API |
| `DEFAULT_AI_MAX_TOKENS` | `4096` | tokens | Maximum tokens per AI response |
| `DEFAULT_AI_TEMPERATURE` | `0.3` | — | Sampling temperature for AI responses (lower = more deterministic) |
| `DEFAULT_AI_MONTHLY_BUDGET` | `0` | USD | Monthly spend cap; `0` means no cap |
| `DEFAULT_AI_AUTO_REQUESTS_PER_DAY` | `5` | requests/day | Maximum automated AI requests per day |
| `DEFAULT_AI_MANUAL_REQUESTS_PER_DAY` | `20` | requests/day | Maximum user-triggered AI requests per day |
| `AI_CIRCUIT_BREAKER_THRESHOLD` | `5` | failures | Consecutive failures before the circuit breaker trips |
| `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `300` | seconds (5 min) | Cooldown duration after circuit breaker trips before retrying |
| `AI_REQUEST_HISTORY_CAP` | `50` | entries | Maximum in-memory request history entries (prevents unbounded growth) |
| `AI_REPORT_HISTORY_CAP` | `10` | entries | Maximum persisted AI reports in `climate_advisor_ai_reports.json` |

**Fan state tracking fields** (runtime coordinator state, not configurable constants):

| Field | Initial Value | Description |
|---|---|---|
| `_fan_active` | `False` | Whether the integration currently has the fan on |
| `_fan_on_since` | `None` | UTC timestamp of last fan activation by the integration |
| `_fan_override_active` | `False` | Whether a user manual fan override is in effect |
| `_fan_override_time` | `None` | UTC timestamp of when the fan override was detected |
| `_fan_command_pending` | `False` | Set during integration-issued fan commands to suppress false override detection |

---

## 16. Planned Window Period

`_is_within_planned_window_period()` is a predicate in `AutomationEngine` that returns `True` when opening sensors should be treated as expected — because the current classification recommends opening windows right now.

### The Three Conditions

All three must be true simultaneously for the check to return `True`:

| # | Condition | Details |
|---|---|---|
| 1 | `windows_recommended == True` | Classification set this flag at classification time — `warm` day (when `today_low` is low enough) or `mild` day (always) |
| 2 | Current local time is within the recommended open window | `warm`: 6:00 AM – 10:00 AM; `mild`: 10:00 AM – 5:00 PM (constants: `WARM_WINDOW_OPEN_HOUR`, `WARM_WINDOW_CLOSE_HOUR`, `MILD_WINDOW_OPEN_HOUR`, `MILD_WINDOW_CLOSE_HOUR`) |
| 3 | HVAC mode is `off` | The classification itself set HVAC to `off` for warm/mild days — if HVAC is running (e.g. classification changed to cool/heat), normal pause rules apply |

### What It Suppresses

When `_is_within_planned_window_period()` returns `True`, the following are suppressed:

- **Pause** — `handle_door_window_open()` logs "not pausing (windows recommended)" and returns without pausing
- **Re-pause after grace expiry** — `_grace_expired()` and `_re_pause_for_open_sensor()` clear grace and return without re-pausing
- **Duplicate open notifications** — no notification is sent when the open sensor is expected

### Where It Is Checked

| Call site | Purpose |
|---|---|
| `handle_door_window_open()` | Blocks initial pause when sensor opens |
| `_grace_expired()` | Blocks re-pause when grace timer fires with sensor still open |
| `_re_pause_for_open_sensor()` | Blocks re-pause called from the grace expiry path |
| `_compute_automation_status()` | Returns `"windows open (as planned)"` instead of a pause/warning status |
| `_compute_next_automation_action()` | Returns `"Windows open as recommended"` in the next-action field |

---

## 17. Natural Ventilation

### Philosophy

Natural ventilation is the cheap path. When outdoor air is cooler than indoor air, pulling it through an open door or window moves heat out of the house at zero energy cost. Running the HVAC system to achieve the same result burns electricity or gas. Climate Advisor treats outdoor air as a free resource to be used whenever three conditions are simultaneously true: the airflow is directionally beneficial, the house has not yet reached the comfort floor, and the outdoor air is not too warm to be useful. When any of those conditions fails, the system either suspends ventilation (if outdoor conditions have temporarily turned unfavorable) or restores heating (if the comfort floor has been reached). HVAC resumes only when outdoor air stops being the better option.

### Activation Conditions

All four must be true simultaneously for natural ventilation to activate.

| Condition | Guard | Rationale |
|---|---|---|
| `outdoor_temp < indoor_temp` | Directional — outdoor must be cooler than indoor | Pulling in warmer air heats the house instead of cooling it; nat vent would work against the goal |
| `indoor_temp > comfort_heat` | Floor guard | If indoor is already at or below the comfort floor, nat vent would immediately trigger a comfort-floor exit — no benefit from activating first |
| `outdoor_temp < comfort_cool + nat_vent_delta` | Ceiling | Outdoor air too warm (even for transitional cooling) should not enter; `nat_vent_delta` provides a configurable tolerance band above `comfort_cool` |
| At least one door/window sensor open | Physical prerequisite | Natural ventilation requires an open path for airflow |

When all conditions are met: HVAC is set to `off`, the fan is activated (per the configured `fan_mode`), and `_natural_vent_active` is set to `True`.

### Exit Hierarchy

Exit conditions are evaluated in priority order on every continuous-monitoring check (`check_natural_vent_conditions()`). The highest-priority matching condition wins.

| Priority | Trigger | Action | Event emitted |
|---|---|---|---|
| 1 | All monitored sensors close | Exit nat vent; resume HVAC from current classification | — |
| 2 | `indoor_temp ≤ comfort_heat` | Exit; restore heat mode at `comfort_heat` (Issue #99 comfort floor exit) | `nat_vent_comfort_floor_exit` |
| 3 | `outdoor_temp ≥ indoor_temp` | Exit to paused state; fan off; start hysteresis lockout timer | `nat_vent_outdoor_rise_exit` |
| 4 | `outdoor_temp > comfort_cool + nat_vent_delta` | Exit to paused state; fan off | — |

**Priority 1 (sensor closes)** always wins. When the physical path for airflow is closed, nat vent ends immediately regardless of outdoor temperature comparisons.

**Priority 2 (comfort floor)** restores heat rather than simply pausing. Once indoor temperature has dropped to `comfort_heat`, the right action is to heat the space back up, not to wait for outdoor conditions to change.

**Priority 3 (outdoor warms above indoor)** starts a hysteresis lockout timer (see Re-activation section below). Without this lockout, the system would oscillate at thermal equilibrium: outdoor rises above indoor → exit → cooling resumes → outdoor drops below indoor → re-activate → repeat.

### Re-activation from Pause

When nat vent has exited due to an outdoor-warm event (Priority 2 above), re-activation requires all three of the following simultaneously:

| Condition | Value | Rationale |
|---|---|---|
| `outdoor_temp < indoor_temp - 1.0°F` | 1°F hysteresis band | Prevents immediate re-activation when temperatures are nearly equal; outdoor must be meaningfully cooler |
| Time elapsed since last outdoor-warm exit ≥ 300 seconds | 5-minute lockout | Prevents oscillation when outdoor and indoor temperatures are at near-equilibrium; gives thermal conditions time to settle |
| `outdoor_temp < comfort_cool + nat_vent_delta` | Ceiling still valid | Ensures outdoor air is still within the useful temperature range |

If all three conditions are met, nat vent re-activates: HVAC remains off, fan turns on, `_natural_vent_active` is set back to `True`.

### `natural_vent_delta` Semantics

`natural_vent_delta` is a ceiling tolerance: the number of degrees above `comfort_cool` that outdoor air is still considered acceptable for natural ventilation. The effective outdoor temperature ceiling is `comfort_cool + natural_vent_delta`.

**Worked example:** indoor = 78°F, outdoor = 74°F, comfort_heat = 70°F, comfort_cool = 72°F, delta = 3°F.

- Ceiling threshold = 72 + 3 = **75°F**
- `outdoor (74) < indoor (78)` ✓ — airflow is directionally beneficial
- `indoor (78) > comfort_heat (70)` ✓ — above comfort floor
- `outdoor (74) < ceiling (75)` ✓ — outdoor is within the useful range

All conditions met → natural ventilation activates.

If outdoor were 76°F instead, the ceiling check would fail (`76 ≥ 75`) and nat vent would not activate despite outdoor still being cooler than indoor.

Default value: `NAT_VENT_DELTA_DEFAULT = 3°F` (see §15 Defaults Reference).

### Phase 2 Note

Trajectory-aware look-ahead — using the thermal model and short-range outdoor temperature forecast to project the activation window into the future — is deferred to Issue #116.

---

## 18. Automation Logic Table

This is the definitive reference for expected system behavior across all classification contexts and sensor/user events. Every cell describes what the automation engine does when a given event fires in a given classification context.

### Classification Contexts

| Code | Day Type | HVAC Mode | windows_recommended | Window Period |
|------|----------|-----------|---------------------|---------------|
| C1 | Hot | cool | False | N/A |
| C2 | Warm | off | True | In period (6–10 AM) |
| C3 | Warm | off | True | Outside period |
| C4 | Warm | off | False | N/A (today_low too high) |
| C5 | Mild | off | True | In period (10 AM – 5 PM) |
| C6 | Cool | heat | False | N/A |
| C7 | Cold | heat | False | N/A |

### Events

| Code | Event |
|------|-------|
| E1 | Door/window sensor opens (after debounce) |
| E2 | All door/window sensors close |
| E3 | Grace period expires with sensor still open |
| E4 | Manual HVAC override during pause |
| E5 | Fan mode change |
| E6 | Classification changes (e.g., warm→hot) |
| E7 | User clicks "Resume HVAC (override pause)" |

### Expected Outcomes

| | E1: Sensor Open | E2: All Closed | E3: Grace+Open | E4: Override | E5: Fan Change | E6: Class Change | E7: Resume |
|---|---|---|---|---|---|---|---|
| C1 (hot/cool) | Pause HVAC→off, notify | Resume to cool, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace | Re-apply classification | Resume cool, manual grace |
| **C2 (warm/off/win=T/in)** | **No pause** (planned window) | No-op (not paused) | **No re-pause** (planned) | N/A (not paused) | No grace (HVAC off) | Re-apply; comfort-floor guard fires if indoor < comfort_heat (see §6b) | N/A (not paused) |
| C3 (warm/off/win=T/out) | No pause (HVAC already off) | No-op | N/A | N/A | No grace | Re-apply; comfort-floor guard fires if indoor < comfort_heat (see §6b) | N/A |
| C4 (warm/off/win=F) | No pause (HVAC already off) | No-op | N/A | N/A | No grace | Re-apply; comfort-floor guard fires if indoor < comfort_heat (see §6b) | N/A |
| **C5 (mild/off/win=T/in)** | **No pause** (planned window) | No-op | **No re-pause** (planned) | N/A | No grace | Re-apply | N/A |
| C6 (cool/heat) | Pause HVAC→off, notify | Resume to heat, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace | Re-apply | Resume heat, manual grace |
| C7 (cold/heat) | Pause HVAC→off, notify | Resume to heat, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace | Re-apply | Resume heat, manual grace |

**Bolded cells** have corresponding test coverage in `tests/test_windows_recommended_integration.py`.

**Warm-day comfort-floor guard (§6b):** In C2, C3, and C4 contexts, `apply_classification()` runs every 30 minutes. If `indoor_temp < comfort_heat`, HVAC is set to heat at `comfort_heat` and the `warm_day_comfort_gap` event is emitted instead of setting HVAC off. Once indoor temp reaches the floor, the next update applies the normal warm-day shutoff. Test coverage: `tests/test_warm_day_comfort_gap.py`.

This logic table MUST be kept current for any changes to automation behavior.

### Test Reference Mapping

| Cell | Test File | Test Name |
|------|-----------|-----------|
| C2×E1 | test_windows_recommended_integration.py | test_no_pause_when_windows_recommended_warm_day |
| C5×E1 | test_windows_recommended_integration.py | test_no_pause_when_windows_recommended_mild_day |
| C1×E1 | test_windows_recommended_integration.py | test_pause_still_fires_for_hot_day |
| C2×E1 (grace) | test_windows_recommended_integration.py | test_no_grace_when_windows_recommended |
| C2×E3 | test_windows_recommended_integration.py | test_grace_expiry_no_repause_during_window_period |
| C2→C1×E6 | test_windows_recommended_integration.py | test_classification_change_warm_to_hot_enables_pause |
| C3×E1 | test_windows_recommended_integration.py | test_pause_fires_outside_window_period_with_active_hvac |
| C2×E6 (comfort gap) | test_warm_day_comfort_gap.py | warm-day indoor < comfort_heat → heat first, then off |
| C4×E6 (comfort gap) | test_warm_day_comfort_gap.py | warm-day (no window rec) indoor < comfort_heat → heat first |

---

_Last Updated: 2026-04-20_
