<!-- Nav: ← [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) -->

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

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What temperature thresholds map to each day type? | HOT ≥ 85°F, WARM ≥ 75°F, MILD ≥ 60°F, COOL ≥ 45°F, COLD < 45°F; all thresholds are °F constants in `const.py`. | [§1. Day Classification](08-COMPUTATION-REFERENCE.md#1-day-classification) |
| How is the setback modifier computed and what values can it take? | `avg_delta = ((tomorrow_high − today_high) + (tomorrow_low − today_low)) / 2`; modifier ranges from −3.0 (strong warming) to +3.0 (significant cold front); stable trend → 0. | [§3. Setback Modifier](08-COMPUTATION-REFERENCE.md#3-setback-modifier) |
| What is the bedtime setpoint formula and when does the thermal model change it? | Default: `comfort_heat − 4°F` (heat) / `comfort_cool + 3°F` (cool). When thermal model confidence ≥ "low", `compute_bedtime_setback()` scales depth from `heating_rate_f_per_hour × recovery_window_hours`, clamped to `[MIN_SETBACK_DEPTH, MAX_SETBACK_DEPTH]`. | [§5a. Adaptive Bedtime Setback](08-COMPUTATION-REFERENCE.md#5a-adaptive-bedtime-setback-compute_bedtime_setback) |
| When does nat-vent continue past bedtime instead of stopping, and what stops it afterward? | When nat-vent is active, the fan is CA-owned, and `outdoor < sleep_cool`, bedtime emits `nat_vent_bedtime_continue` and keeps the fan running. The fan stops when `check_natural_vent_conditions()` detects `indoor ≤ sleep_cool` in the sleep window (Priority 0 exit, event `nat_vent_sleep_ceiling_reached`). | [§5a nat-vent continuation gate](08-COMPUTATION-REFERENCE.md#5a-adaptive-bedtime-setback-compute_bedtime_setback) · [Exit Hierarchy Priority 0](08-COMPUTATION-REFERENCE.md#exit-hierarchy) |
| When and how does CA pre-cool the home on warming-trend nights? | Mid-night trigger (nat-vent close + 30 min, or wake − 4 h fallback); target = `sleep_cool + setback_modifier` floored at `comfort_heat + 2°F`. AC suppressed if nat-vent already reached target. Morning guard emits `pre_cool_overshoot` if indoor < `comfort_heat` at wake-up. | [§5a-i. Overnight Pre-Cool Phase](08-COMPUTATION-REFERENCE.md#5a-i-overnight-pre-cool-phase-issue-258) |
| How does the physics ODE predict future indoor temperature? | `T(t+dt) = T_outdoor + (T − T_outdoor) × exp(k_p × dt) + (Q/k_p) × (exp(k_p × dt) − 1)`, where Q switches between k_active_heat, k_active_cool, and 0 per schedule period. | [§5c. Predicted Temperature Graph — Physics Path](08-COMPUTATION-REFERENCE.md#5c-predicted-temperature-graph--physics-path) |
| What is the dynamic target band and how does occupancy mode change it? | `_compute_target_band_schedule()` returns `[{ts, lower, upper}]` per forecast hour; away = setback today only, vacation = deep setback all days, home/guest = comfort with sleep/wake ramps. | [§5d. Dynamic Target Band](08-COMPUTATION-REFERENCE.md#5d-dynamic-target-band--_compute_target_band_schedule) |
| How does comfort score accumulate and what triggers a suggestion? | `comfort_score = 1 − (total_violation_minutes / (days_recorded × 1440))`; more than 5 days with > 30 violation minutes triggers the `comfort_violations` suggestion. | [§Metric Definitions — Comfort Score](05-LEARNING-ENGINE-DESIGN.md#comfort-score-comfort_score) |
| When does the ODE ceiling guard fire on a warm day and what activates AC? | The guard scans the predicted indoor curve on every 30-min cycle and sets HVAC to cool at `comfort_cool` when a breach is predicted within the lead time (or 120-min fallback). It is **dormant only when all 3 hold**: outdoor <= indoor AND nat-vent is actually running AND indoor still <= ceiling. So it also fires when indoor already exceeds the ceiling (even if outdoor < indoor) or when nat-vent is not running — clearing nat-vent on escalation. Guard skips when no calibrated model or occupancy is away/vacation. | [§6c. Warm-Day ODE Ceiling Guard](08-COMPUTATION-REFERENCE.md#6c-warm-day-ode-ceiling-guard-issue-136) |
| How does MILD day window scheduling change when the ODE is available (Fix C, Issue #147)? | Before Fix C: MILD days used hardcoded `time(10, 0)` open / `time(17, 0)` close. After Fix C: constants `MILD_WINDOW_OPEN_HOUR = 10` and `MILD_WINDOW_CLOSE_HOUR = 17` are fallbacks; when the ODE is available, `nat_vent_cutoff` drives the close time — the same dynamic logic as warm days. | [§6d. MILD Day Dynamic Window Close Time](08-COMPUTATION-REFERENCE.md#6d-mild-day-dynamic-window-close-time-fix-c-issue-147) |
| What invariant must `_async_send_briefing()` maintain when replacing `_today_record`? | It must copy all accumulated counters (`hvac_runtime_minutes`, `comfort_violations_minutes`, etc.) from the existing same-day record before constructing the new one. Creating a fresh `DailyRecord` unconditionally resets all counters to zero (Issue #176 bug). | [DailyRecord Persistence Invariant](08-COMPUTATION-REFERENCE.md#dailyrecord-persistence-invariant-issue-176) |
| Why must `_async_thermostat_changed()` check all three command-pending flags, not just `_hvac_command_pending`? | Automation sequences (e.g., nat vent exit) call `_deactivate_fan()` before `_set_hvac_mode()`. The fan command sets `_fan_command_pending` but leaves `_hvac_command_pending` False. Checking only `_hvac_command_pending` bypasses the override-detection guard during that window. | [§9b Compound command-pending guard](08-COMPUTATION-REFERENCE.md#compound-command-pending-guard-in-_async_thermostat_changed-issue-205206) |
| Why was a manual mode override not detected on dual-setpoint (`heat_cool`) thermostats? | CA commands `heat_cool` mode but the old code compared the thermostat's `hvac_mode` against `classification.hvac_mode` (e.g., `"cool"`). A user switching from `heat_cool` to `cool` evaluated as equal and was ignored. Fix: compare against `_last_commanded_hvac_mode` first. | [§9b Mode Override Detection — `_last_commanded_hvac_mode`](08-COMPUTATION-REFERENCE.md#mode-override-detection--_last_commanded_hvac_mode-issue-269-bug-c) |
| Why was a manual setpoint change not detected on dual-setpoint (`heat_cool`) thermostats? | In `heat_cool` mode the `temperature` attribute is `None`; only `target_temp_low`/`target_temp_high` are populated. The setpoint override check now reads those attributes when mode is `heat_cool`. | [§9b Dual Setpoint Override Detection](08-COMPUTATION-REFERENCE.md#dual-setpoint-override-detection--heat_cool-mode-issue-269-bug-d) |
| Why do cloud thermostats (Nest, Ecobee) falsely trigger fan overrides after HVAC mode changes? | Cloud polling echoes `fan_mode` attribute changes as delayed side-effects 30–120 s after the command, outside the 30 s `_is_recent_hvac_command` window. The `_is_expected_confirmation` flag extends suppression to 120 s for the `fan_mode` path. | [§9b `_is_expected_confirmation`](08-COMPUTATION-REFERENCE.md#_is_expected_confirmation-issue-269-bug-a) |
| What is the comfort-band programming model introduced in Issue #249? | CA programs a floor+ceiling band every 30 min via one `select_comfort_band` decision and one `_apply_comfort_band` actuation; the thermostat's own deadband holds the house inside the band continuously — no more off+supervisor pattern. | [§6e Comfort-Band Programming](08-COMPUTATION-REFERENCE.md#6e-comfort-band-programming-issue-249) |
| What command shape does `_apply_comfort_band` emit per thermostat capability? | Dual-capable: `heat_cool` mode + `set_temperature(target_temp_low=floor, target_temp_high=ceiling)`. Cool-only (active=ceiling): `cool` + `set_temperature(ceiling)`. Heat-only (active=floor): `heat` + `set_temperature(floor)`. | [§6e — `_apply_comfort_band` command shapes](08-COMPUTATION-REFERENCE.md#_apply_comfort_band-command-shapes) |
| Why does nat-vent no longer set HVAC off when windows open (Issue #249)? | The band stays armed when nat-vent activates; the thermostat self-arbitrates — free cooling is free, AC kicks in only if the breeze can't hold the ceiling. Turning HVAC off also disarmed the floor, making cold-snap escalation impossible. Fix #338 adds: when nat-vent activates (or re-activates from paused), `_apply_nat_vent_hvac_state()` immediately re-arms the appropriate band (full or floor-only per `aggressive_savings`), closing a gap where Path B re-activation deferred re-arming up to 30 min. | [§6e — Nat-vent and economizer with the band armed](08-COMPUTATION-REFERENCE.md#nat-vent-and-economizer-with-the-band-armed) |
| What band does `_apply_nat_vent_hvac_state()` arm when nat-vent activates, and how does `aggressive_savings` affect it? | `FAN_MODE_WHOLE_HOUSE`/`DISABLED` → no-op. `FAN_MODE_HVAC` + `aggressive_savings=False` → full band `[comfort_heat, comfort_cool]`. `FAN_MODE_HVAC` + `aggressive_savings=True` → floor-only (heat @ `comfort_heat`, ceiling disarmed — no compressor through open windows). Called at initial activation, paused re-activation, and every 30-min `apply_classification()` cycle. | [§6e — `_apply_nat_vent_hvac_state()`](08-COMPUTATION-REFERENCE.md#_apply_nat_vent_hvac_state--band-arming-on-nat-vent-activate-fix-338) |
| How does the solar phase offset resolver decide which EWMA to use, and what is the fallback? | Fresh primary wins; fresh secondary (≥ 3 obs) next; then stale primary; stale secondary; generic default only when nothing has ever been learned. Staleness = last_obs_date absent or > 90 days old (`THERMAL_PARAM_STALE_DAYS`). | [§5e-viii Two-EWMA Solar Phase Architecture](08-COMPUTATION-REFERENCE.md#5e-viii-solar-phase-offset--two-ewma-architecture-issue-312) |
| What quality gates must a chart_log day pass before the AC duty cycle solar phase method estimates an offset? | Five gates: setpoint_cool field present; setpoint in [68, 80]°F; spread < 1.5°F over 11:00–18:00; ≥ 4 cool entries in 11:00–16:00; at least one 11:00–16:00 entry has indoor > median setpoint. | [§5e-viii AC duty cycle quality filter](08-COMPUTATION-REFERENCE.md#5e-viii-solar-phase-offset--two-ewma-architecture-issue-312) |
| What are the two fan archetypes and how does each affect HVAC mode and fan-stops-on-close behavior? | `FAN_MODE_HVAC` (HVAC blower): band stays armed, HVAC unchanged when fan activates, fan does NOT stop when windows close unless `_natural_vent_active=True`. `FAN_MODE_WHOLE_HOUSE` (exhaust fan): HVAC set to off on activation (mode captured in `_pre_fan_hvac_mode`), restored on deactivation, fan stops when ALL sensors close even if `_natural_vent_active=False`. | [§9 Fan Archetype Behavioral Contract](08-COMPUTATION-REFERENCE.md#fan-archetype-behavioral-contract-issue-277) |
| Why does `_set_hvac_mode("off")` also set `_fan_command_time` (Issue #277 Bug A1)? | The `set_fan_mode(auto)` assertion inside `_set_hvac_mode("off")` sets `_fan_command_time = dt_util.now()` before the service call, so cloud thermostat echoes of the fan_mode attribute change are suppressed within the `_is_recent_fan_command` window instead of triggering a false manual override. | [§9b Race Guard — `_set_hvac_mode("off")` fan_command_time](08-COMPUTATION-REFERENCE.md#_set_hvac_mode-off-fan_command_time-guard-issue-277-bug-a1) |
| How does `_async_thermostat_changed()` prevent a single event from triggering both a setpoint and a fan override (Issue #277 Bug B)? | A local `_setpoint_override_detected` flag is initialized to `False` before Block 2 (setpoint detection) and Block 3 (fan_mode detection). If Block 2 fires and sets it `True`, Block 3 is suppressed via `and not _setpoint_override_detected`. One event → at most one override type. | [§9b Setpoint/Fan Mutual Exclusion](08-COMPUTATION-REFERENCE.md#setpointfan-override-mutual-exclusion-issue-277-bug-b) |
| What override and grace state is preserved vs discarded on HA restart (Issue #282/#306)? | Both pause state (`_paused_by_door`, `_pre_pause_mode`) and override state (`_manual_override_active`, `_grace_active`, `_override_confirm_pending`) are discarded — CA always starts in full clean-slate automation mode. Open sensors are re-detected within 30–90 s via the state-change listener (None → "on" transition). A 5-minute `_first_run` settling window provides startup debounce. | [§11 Clean-Slate Override State on HA Restart](08-COMPUTATION-REFERENCE.md#clean-slate-override-state-on-ha-restart-issue-282) |
| What notification does the user receive when PATH B (transient thermostat adjustment) fires (Issue #200)? | "Brief thermostat adjustment detected — treated as transient. Climate Advisor continues normal operation." No grace period starts; automation resumes immediately. | [§11 PATH B Notification](08-COMPUTATION-REFERENCE.md#path-b-notification--transient-thermostat-adjustment-issue-200) |
| What happens if the user changes to a different HVAC mode while a grace period is already active (Issue #201)? | The current override and grace are cleared, and a fresh 10-minute confirmation window starts for the new mode. Latest user action wins. | [§11 Second Override During Active Grace](08-COMPUTATION-REFERENCE.md#second-override-during-active-grace-issue-201) |
| How does `_run_solar_phase_chart_log_fit()` stay current without re-scanning months of history on every cycle (Issue #310)? | Two-tier schedule: one-shot backfill (30-day lookback, `backfill_done` flag) runs once on fresh install; periodic daily re-fit (2-day lookback, `_last_solar_phase_fit_date` gate) runs at most once per calendar day thereafter. | [§5e-v Two-tier fit scheduling](08-COMPUTATION-REFERENCE.md#two-tier-fit-scheduling-issue-310) |
| What guarantees that a running fan always has an owner after Issues #327 and #347? | Restart now clears `_fan_override_active` for a clean slate; `_do_startup_coalesce` reconciles the physical fan state (adopt-on / turn-off / no-fan); `fan_thermostat_check()` re-evaluates on every indoor or outdoor temp change; the economizer gains an `outdoor < indoor` direction guard. Post-startup `hvac_action` transitions to `"fan"` while CA does not own the fan are caught in `_async_thermostat_changed` and resolved by `reconcile_fan_on_startup` immediately (Issue #347). `"Running (untracked)"` remains only as a brief transient, not an indefinite limbo. | [§9e Thermostatic Fan Loop and Startup Reconciliation (Issue #327)](08-COMPUTATION-REFERENCE.md#9e-thermostatic-fan-loop-and-startup-reconciliation-issue-327) |

## 1. Day Classification

Today's high temperature is compared against fixed thresholds to assign a `day_type`. All downstream decisions (HVAC mode, setpoints, window advice, pre-conditioning) flow from this classification.

| today_high condition | day_type | HVAC mode (classifier) | Constant name |
|---|---|---|---|
| `today_high >= 85` | `hot` | `cool` | `THRESHOLD_HOT = 85` |
| `75 <= today_high < 85` | `warm` | `off` ¹ | `THRESHOLD_WARM = 75` |
| `60 <= today_high < 75` | `mild` | `off` ¹ | `THRESHOLD_MILD = 60` |
| `45 <= today_high < 60` | `cool` | `heat` | `THRESHOLD_COOL = 45` |
| `today_high < 45` | `cold` | `heat` | _(below all thresholds)_ |

¹ The `off` field in `DayClassification` is a historical label from the classifier's perspective (no active HVAC needed at peak). In practice, the automation engine programs a comfort band (floor = `comfort_heat`, ceiling = `comfort_cool` while occupied + awake) rather than issuing an actual `hvac_mode=off` command — the thermostat holds the band autonomously and runs the compressor only if natural ventilation can't keep up. See [§6e Comfort-Band Programming](#6e-comfort-band-programming-issue-249).

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

| Trigger | Target temperature formula | When applied | Exit condition |
|---|---|---|---|
| Hot day (`day_type == hot`) | `comfort_cool + (-2)` = `comfort_cool - 2` | At classification time (morning) | Once `indoor_temp ≤ comfort_cool + pre_condition_target` (e.g. ≤ 73°F), `_pre_condition_achieved` is set and the ceiling offset is skipped for the rest of the day; flag resets daily. |
| Moderate cold front (`cooling`, magnitude 5–9°F) | `comfort_heat + 2.0` | Scheduled at 7:00 PM | Not yet implemented. |
| Significant cold front (`cooling`, magnitude ≥ 10°F) | `comfort_heat + 3.0` | Scheduled at 7:00 PM | Not yet implemented. |
| ODE ceiling defense (`warm` or `mild` day, model calibrated, breach predicted) | `comfort_cool` | Reactive: passive safety backstop (§6c); naturally dormant when the comfort band is armed because the band's ceiling already holds the house below `comfort_cool` | N/A — fires only when a breach is predicted; not a sustained hold. |

> **Issue #249 — band model change:** Warm and mild days previously issued an `hvac_mode=off` command at classification time and relied on §6b/§6c guards to rescue the home if temperatures drifted. The automation engine now programs the occupied comfort band `[comfort_heat, comfort_cool]` (suppression to setback applies only away/asleep) instead. The thermostat holds both edges autonomously; the pre-conditioning column above reflects the new steady-state where the ODE ceiling guard is a passive backstop rather than the primary defense. See [§6e](#6e-comfort-band-programming-issue-249).

**Hot-day pre-cool detail:** The `pre_condition_target` is stored as `-2.0` (a negative offset). `_set_temperature_for_mode()` applies it as `comfort_cool + pre_condition_target`, so a `comfort_cool` of 75°F yields a pre-cool target of **73°F**.

**Pre-cool exit:** `_pre_condition_achieved` is set on `AutomationEngine` when `indoor_temp ≤ absolute_target` (i.e. `indoor_temp ≤ comfort_cool + pre_condition_target`). It is passed to `select_comfort_band()` to suppress the ceiling offset for subsequent 30-min cycles — the ceiling reverts to `comfort_cool` for the rest of the day. The flag persists through HA restarts (serialized to state) and resets at the start of each new day. The flag is **not re-armed** if indoor temperature later drifts above the pre-cool target — once achieved, the offset is suppressed for the remainder of the day regardless of subsequent temperature changes.

**Test coverage:** golden scenario `hot_day_precool_achieved_reverts_to_comfort` (Issue #295).

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
- Bedtime setback depth is now computed by `compute_bedtime_setback()` in `automation.py` (see §5a). When `sleep_heat` / `sleep_cool` are explicitly configured (#101), those values are used directly as the bedtime setpoint, bypassing the adaptive depth computation. The hardcoded defaults (`DEFAULT_SLEEP_HEAT = 66°F`, `DEFAULT_SLEEP_COOL = 78°F`) apply when neither sleep temps are configured nor thermal model data is available.
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

**Cool-mode sign convention (Issue #258):** For cool-mode nights, `setback_modifier < 0` means a warming trend — the next day will be hotter. The modifier is applied as `sleep_cool + setback_modifier`, which _lowers_ the cool ceiling (more aggressive cooling, thermal mass banking). A _positive_ modifier (cooling trend) _raises_ the ceiling (relaxed setback, AC cycles less). No sign flip is applied in cool mode.

**Nat-vent continuation gate at bedtime (Issue #370):** `handle_bedtime()` evaluates the sleep band before deciding whether to deactivate the fan. When nat-vent is active (`_natural_vent_active=True`), the fan is running under CA control (`_fan_active=True`), no manual override is in effect, and `outdoor_temp < sleep_band.ceiling` (outdoor air is still cooler than the sleep target), bedtime skips fan deactivation and emits `nat_vent_bedtime_continue`. The fan continues until `check_natural_vent_conditions()` detects the sleep ceiling has been reached (see Priority 0 exit in §check_natural_vent_conditions below). If any gate fails, `_deactivate_fan()` is called and `_natural_vent_active` is cleared to `False`. This applies to all fan archetypes (WHF, HVAC fan, BOTH).

### 5a-i. Overnight Pre-Cool Phase (Issue #258)

On warming-trend nights (`setback_modifier < 0`), the coordinator schedules a second setpoint change mid-night — after nat-vent has had its window — to bank cold thermal mass before the afternoon peak:

**Trigger timing** (coordinator `_compute_pre_cool_trigger_time()`):

| Condition | Trigger time |
|---|---|
| `classification.window_close_time` is set (nat-vent configured) | `window_close_time + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES (30 min)` — gives nat-vent a complete window first |
| No nat-vent config | `wake_time − PRE_COOL_WAKE_OFFSET_HOURS (4 h)` — fallback |
| `setback_modifier >= 0` | No trigger scheduled (no warming trend) |

**Target formula** (`handle_pre_cool()` in `automation.py`):

```
raw_target  = sleep_cool + setback_modifier          # modifier is negative → lower ceiling
floor       = comfort_heat + PRE_COOL_MIN_HEADROOM_F  # default: comfort_heat + 2°F
pre_cool_target = max(raw_target, floor)              # clamp prevents morning heating
```

The floor guard prevents the home from dropping so far below `comfort_heat` that the heat fires at wake-up (doubly wasteful: night AC + morning heat).

**Nat-vent bypass condition:** If the pre-cool trigger fires with `nat_vent_just_closed=True` AND `indoor_temp ≤ pre_cool_target`, the AC service call is suppressed — free cooling via open windows already achieved the target. Event `pre_cool_suppressed_nat_vent` is emitted.

**Applied path:** `_apply_comfort_band(ComfortBand(floor=sleep_heat, ceiling=pre_cool_target, active="ceiling"))` — the heat floor is preserved from the current sleep band. Event `pre_cool_applied` is emitted with `{target, modifier, sleep_cool, floor, indoor, nat_vent_suppressed}`.

**Skip conditions:**

| Condition | Result |
|---|---|
| `setback_modifier >= 0` (stable or cooling trend) | Skip silently |
| Occupancy is `away` or `vacation` | Skip (setback already active) |
| `_manual_override_active` | Skip (user in control) |
| `indoor_temp is None` with `nat_vent_just_closed=True` | No bypass possible → apply setpoint |

**Morning guard:** `handle_morning_wakeup(indoor_temp=...)` now accepts the current indoor temperature. If `indoor_temp < comfort_heat` at wake-up, event `pre_cool_overshoot` is emitted (diagnostic) and the heat may fire. The floor guard on `pre_cool_target` is the primary prevention; the morning guard is observability for cases where thermal drift exceeded the floor.

**Status visibility:** Coordinator exposes `pre_cool_status` string in `_async_update_data()` result dict → `api.py` status response → dashboard Automation Status card (secondary line when non-null). Values: `"pre-cool tonight (75°F @ 2:30 AM)"` / `"pre-cool active (75°F ceiling)"` / `"pre-cool suppressed · nat-vent cooled to 74°F"` / `null` (no warming trend).

**Chart:** `_compute_target_band_schedule()` accepts `pre_cool_trigger_h` and `pre_cool_target` params. When non-null, the band ceiling steps down from `sleep_cool` to `pre_cool_target` at the trigger hour and holds until `wake_time`.

**Test coverage:** `tests/test_pre_cool.py`; golden scenarios `warming_trend_pre_cool_applied` and `warming_trend_pre_cool_nat_vent_bypass` (Issue #258).

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

`_build_predicted_indoor_future()` accepts `occupancy_mode` (default `OCCUPANCY_HOME`) and `classification` parameters. It pre-computes the band schedule once via `_compute_target_band_schedule()` — passing `thermal_model`, `classification`, and `setback_modifier` — before iterating forecast hours. This means the predicted indoor curve uses the same adaptive sleep setpoints as the automation engine, and correctly targets setback temperatures on away/vacation days. Vacation mode propagates setback to all forecast days; away mode applies setback to today only.

**Gate bridge self-healing (Issue #126 Phase A):** When `k_passive` is `None` but
`k_vent_window` is available (homes with ventilated-only observations and no passive or
HVAC cycles), the coordinator promotes `k_vent_window` to stand in as the proxy decay
rate. Two bugs fixed:

- **Bug A:** The bridge now fires when `_conf_k_passive == "none"` (string equality), not
  only when `k_passive is None`. Pre-Issue #126 installs that stored `k_passive=None` with
  `confidence="none"` self-heal automatically on the next coordinator update — the bridge
  detects the "none" string and promotes `k_vent_window`.
- **Bug B:** The `_k_passive_via_bridge=True` flag bypasses the `_physics_eligible()`
  confidence check. Without this flag, bridge-provided k_passive would still fail the
  `conf != "none"` guard and fall through to the ramp path, defeating the purpose of the
  bridge.

Install states handled:

| Install state | k_passive | confidence | k_vent_window | Bridge fires? | Physics eligible? |
|---|---|---|---|---|---|
| Fresh — no data | `None` | `"none"` | `None` | No (nothing to promote) | No — ramp |
| Contaminated — old bug | `None` | `"none"` | valid | Yes — promotes k_vent_window | Yes — physics |
| Healed — bridge ran | promoted value | `"none"` (unchanged) | valid | Not needed (k_passive set) | Yes — bypass flag |
| Normal — HVAC obs | valid | `"low"`/`"medium"`/`"high"` | any | Not needed | Yes — normal path |

**Fallback (ramp interpolation):** When model confidence is `"none"` or `k_passive` is unavailable/non-negative, the legacy ramp path runs:

| Condition | Ramp Duration |
|---|---|
| No model data | Default 30-minute ramp |
| Model available (legacy path only) | `ramp_hours = temp_delta / rate`; minimum 15 minutes; computed by `_compute_ramp_hours()` |

`_compute_ramp_hours()` uses whichever rate applies to the transition direction (heating rate for rising ramps, cooling rate for falling ramps).

### 5d. Dynamic Target Band — `_compute_target_band_schedule()`

From Issue #119, the chart's "Target Band" overlay is no longer two static scalars. `get_chart_data()` calls `_compute_target_band_schedule()` once (pre-computed before the loop) to produce a time-series `[{ts, lower, upper}]` covering every forecast hour, and passes this as `target_band` in the API response.

**Function signature:** `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now, setback_modifier=0.0, thermal_model=None, classification=None) → list[{ts, lower, upper}]`

**Per-timestamp band logic:**

| Occupancy / time condition | lower | upper |
|---|---|---|
| Away — today only | `setback_heat + setback_modifier` | `setback_cool − setback_modifier` |
| Vacation — **all forecast days** | `setback_heat + setback_modifier − VACATION_SETBACK_EXTRA` | `setback_cool − setback_modifier + VACATION_SETBACK_EXTRA` |
| Home/guest — pre-wake (`h_n < wake_h`) | `sleep_heat` | `sleep_cool` |
| Home/guest — wake ramp (2h linear) | Interpolates `sleep_heat → comfort_heat` | Interpolates `sleep_cool → comfort_cool` |
| Home/guest — awake (`wake_h+2h ≤ h_n < sleep_h`) | `comfort_heat` | `comfort_cool` |
| Home/guest — sleep ramp (1h linear) | Interpolates `comfort_heat → sleep_heat` | Interpolates `comfort_cool → sleep_cool` |
| Home/guest — post-sleep (`h_n ≥ sleep_h+1h`) | `sleep_heat` | `sleep_cool` |
| Away — **future days** (tomorrow+) | Normal home/guest schedule (assumes return) | Same |

**`setback_modifier` parameter:** The trend-based offset from `DayClassification` (see §3). Positive values (cold front coming) narrow the setback; negative values (warm trend) widen it. Passing `setback_modifier` ensures the chart band and the automation engine use identical setback bounds on trend days.

**Vacation scope:** Vacation mode applies deep setback to **all** forecast days (today and future), not just today. This reflects that a vacationing household is away for the entire forecast window. Away mode applies setback to today only (assumes a return by tomorrow).

**Night-owl schedule normalization:** When `sleep_time < wake_time` (e.g., sleep=01:00, wake=09:00), the schedule wraps past midnight. The function normalises by adding 24 to `sleep_h` (making it e.g. 25) and computing `h_n = h + 24 if night_owl and h < wake_h else h` for each timestamp's local hour. This maps all timestamps onto a continuous `[wake_h, sleep_h]` number line regardless of the midnight boundary.

**Adaptive sleep temperatures (G1/G2):** When both `thermal_model` and `classification` are provided, `sleep_heat` and `sleep_cool` are derived from `compute_bedtime_setback(config, thermal_model, classification)` — the same function used by `automation.py`. This eliminates the three-implementation gap between chart band, physics prediction, and automation setpoints: all three now derive sleep temps from the same adaptive logic. When `thermal_model` or `classification` is `None`, the fallback values (`comfort_heat − DEFAULT_SETBACK_DEPTH_F`, `comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F`) are used.

**Notes:**
- `sleep_heat` and `sleep_cool` base fallbacks are `comfort_heat − 4°F` and `comfort_cool + 3°F` respectively, but are overridden when the user has explicitly configured sleep temperatures (Issue #101). Adaptive `compute_bedtime_setback()` output is used in preference to both when a thermal model is available.
- HVAC-off days (warm/mild) still display the full target band. The system actively monitors and will engage heating or cooling if indoor temperature wanders outside the target range.
- The chart layer was renamed from "Comfort Band" to "Target Band" in Issue #119 to reflect that the band now varies over time.
- `_build_predicted_indoor_future()` pre-computes the band schedule once via `_compute_target_band_schedule()` before iterating forecast hours (Issue #119 Phase 2 fix for B3 — eliminates redundant per-hour recomputation).

**Per-hour k selection — ventilation wiring (Issue #126 Phase 2C):** For forecast hours where `classification.windows_recommended=True` and `local_ts.time()` falls in `[window_open_time, window_close_time)`, the ODE uses `k_vent_window` as the effective decay rate instead of `k_passive`. `k_vent_window` is the **total** measured k during ventilated conditions (not an incremental addend) — so it replaces, not supplements, `k_passive`. Gate bridge guard: when `_k_passive_via_bridge=True` (k_passive was `None` and k_vent_window was already promoted to proxy k_passive for all hours), per-hour substitution does not fire — k_vent_window is already in play for the entire forecast and double-substitution would be incorrect. During sunny window-open hours, the combined ODE is `dT/dt = k_vent_window*(T_out − T_in) + k_solar*solar_factor`; for a thermally inert home (k_vent_window ≈ 0) this reduces to `dT/dt ≈ k_solar*solar_factor`, correctly predicting solar-driven warming even with windows open.

### 5e. Thermal Model v3 — Observation Types (Issue #121)

The thermal model collects observations from six parallel observation types, not just
HVAC heat/cool cycles. Multiple observation types can run concurrently in a
`_pending_observations` dict keyed by obs_type string.

| Type | Trigger | Measures | Min samples |
|------|---------|----------|-------------|
| `hvac_heat` | hvac_action=heating | k_active_heat, k_passive (via pre-heat buffer) | 10 post-heat |
| `hvac_cool` | hvac_action=cooling | k_active_cool | 10 post-heat |
| `passive_decay` | HVAC off, fan off, windows closed, \|ΔT\| ≥ 3°F | k_passive | 30 |
| `fan_only_decay` | Fan active, HVAC off, windows closed | k_vent | 15 |
| `ventilated_decay` | Any window open, HVAC off | k_vent_window | 20 |
| `solar_gain` | HVAC off, fan off, windows closed, T_in > T_out, daytime | k_solar | 20 |

**HVAC plateau guard**: reduced from 1.0°F to 0.3°F (`THERMAL_HVAC_MIN_DECAY_F`). The 1.0°F
guard rejected all observations on short-cycling thermostats (avg cycle < 1°F rise).

**ODE (v3)**: `dT/dt = (k_passive + k_vent_eff)*(T_out - T_in) + k_solar*solar_factor + Q_hvac`
where `k_vent_eff = k_vent` when ventilation is active, `solar_factor` = sinusoidal 0→1→0
over daylight hours (8–18 local), `Q_hvac = ±k_active` when HVAC is driving toward setpoint.

**Confidence grades**: `confidence_k_passive` is graded independently of `confidence_k_hvac`.
Physics prediction activates when either confidence is > "none", enabling prediction on
homes with passive-only observations (zero HVAC cycles recorded).

`confidence_k_solar` is graded from `observation_count_solar` (fixed in Issue #308 — was hardcoded `"none"`):

| Threshold | Grade |
|---|---|
| 0–19 observations | `"none"` |
| ≥ 20 observations | `"low"` |
| ≥ 50 observations | `"medium"` |
| ≥ 100 observations | `"high"` |

`confidence_k_solar` is exposed as an alias key in the dict returned by `get_thermal_model()`.

#### 5e-i. Sampling Cadence — Per-Type Decimation (Issue #122 H1)

The coordinator polls every 30 seconds. Sampling slow decay phenomena at poll rate yields
noise — inter-sample temperature change is dominated by sensor quantisation, not the
signal. A per-type wall-clock gate in `_sample_all_observations()` section A limits how
often a sample is appended to each observation's `samples` list:

| Type | Sample interval | Constant |
|------|----------------|----------|
| `hvac_heat` / `hvac_cool` active phase | Every poll (no gate) | — |
| `hvac_heat` / `hvac_cool` post-heat phase | 5 min | `THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S` |
| `passive_decay` | 5 min | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` |
| `fan_only_decay` | 2 min | `THERMAL_FAN_SAMPLE_INTERVAL_S` |
| `ventilated_decay` | 5 min | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` |
| `solar_gain` | 5 min | `THERMAL_SOLAR_SAMPLE_INTERVAL_S` |

The gate timestamp is stored as `"last_sample_time"` in the observation dict. HVAC
active-phase sampling is ungated — fast HVAC dynamics benefit from maximum resolution.
`fan_only_decay` uses a 2-minute interval because fan-assisted heat transfer is faster
than pure passive drift.

**Convergence**: A 6-hour overnight passive window at 5-min decimation yields ~72 samples
— vs. 720 noise-dominated samples at poll rate. The 30-sample minimum for `passive_decay`
requires roughly 2.5 hours of clean uninterrupted signal to commit.

#### 5e-ii. Rolling-Window Commits (Issue #122 H2)

Long observation windows are accurate but slow to yield a commit. Rolling commits break
each long passive/vent/solar observation into consecutive 30-minute slices. When
`THERMAL_ROLLING_WINDOW_MINUTES (30 min)` elapses since the observation started (or
since the last rolling commit), `_commit_rolling_window_obs()` fires:

1. Requires at least 3 samples in the window.
2. For `passive_decay` and `solar_gain`: requires total indoor ΔT ≥
   `THERMAL_ROLLING_MIN_DELTA_T_F (0.2°F)`. This guards against noise-fitting on
   near-flat data in short windows (< 10 samples).
3. For `fan_only_decay` and `ventilated_decay`: the ΔT guard is skipped
   (`skip_delta_guard=True`) because the signal guarantee is the indoor–outdoor
   differential (already checked by the observation's trigger condition), not the
   temperature trend.
4. All rolling commits use `force_grade="low"` (EWMA α = 0.05).
5. After commit, the observation is popped from `_pending_observations`. Section B of
   `_sample_all_observations()` restarts it on the next poll if conditions still hold.

**Convergence impact**: Rolling windows yield ~16 `passive_decay` commits per 8-hour
overnight window (480 min ÷ 30 min) vs. 1 commit per full-night window in v2. The model
reaches 5% accuracy in ~4 nights (α = 0.05) vs. ~60 nights before.

#### 5e-iii. Wall-Clock Abandon Timeout (Issue #122 H4)

`ventilated_decay` and `fan_only_decay` abandon after `THERMAL_DECAY_MAX_WINDOW_MINUTES
(60 min)` if rolling commit has not fired and the signal has not met the minimum ΔT
threshold. Abandon reason logged: `"max_window_elapsed_low_signal"`. This prevents
stale near-equilibrium observations from persisting when a window is left open or the
fan is running with indoor and outdoor temps nearly equal.

`passive_decay` and `solar_gain` do not have this timeout — rolling commits bound their
window length naturally.

#### 5e-iv. `_update_thermal_model_cache()` — E6 Parameter Routing Fix (Issue #122)

Each committed observation updates the EWMA cache via `learning._update_thermal_model_cache()`.
The `hvac_mode` field in the observation dict determines which cache field is updated:

| `hvac_mode` | Updates cache field | Count field |
|---|---|---|
| `"heat"` | `k_active_heat`, `k_passive` | `observation_count_heat` |
| `"cool"` | `k_active_cool`, `k_passive` | `observation_count_cool` |
| `"passive"` | `k_passive` only | `observation_count_passive` |
| `"fan_only"` | `k_vent` (from obs `k_passive` field) | `observation_count_fan_only` |
| `"ventilated"` | `k_vent_window` (from obs `k_passive` field); also `k_solar` when 2-param OLS fires (see §5e-v) | `observation_count_vent` |
| `"solar"` | `k_solar` (from obs `k_solar` field) | `observation_count_solar` |

**E6 fix**: Before Issue #122, the `elif mode == "passive"` branch incorrectly wrote
`k_p` to `cache["k_vent"]`. The fix removes that line — passive observations no longer
contaminate the ventilation parameter. Only `fan_only` observations update `k_vent`.

#### 5e-v. Adaptive 2-Param Ventilated OLS (Issue #126)

`ventilated_decay` observations optionally upgrade from 1-parameter OLS (solving only
`k_vent_window`) to a 2-parameter joint solve (`k_env_vent` + `k_solar`) when solar
conditions during the window provide enough variation to separate the two effects.

**Trigger condition:** At commit time, if
`max(solar_factor across samples) − min(solar_factor across samples) ≥ THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`,
`compute_k_env_solar(samples)` runs the 2×2 normal equations:

```
[Σδ²    Σδ·sf ] [k_env ] = [Σrate·δ ]
[Σδ·sf  Σsf²  ] [k_solar]   [Σrate·sf]
```

where `δ = T_out − T_in`, `sf = solar_factor`, `rate = ΔT/Δt` for each sample pair.

**Collinearity guard:** If `|det(A)| < 1e-12`, the solve is skipped and the standard
1-param OLS path runs instead. This protects against numerical instability when `δ` and
`sf` are nearly proportional (e.g., morning window observations where outdoor temperature
and solar position track together).

**Acceptance criteria:** The 2-param result is accepted only if:
- `k_env_vent` passes the same bounds check as `k_passive` (`[THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX]`)
- `k_solar ≥ 0` (solar must add heat, not remove it)
- R² of the 2-param fit ≥ `THERMAL_MIN_R_SQUARED (0.2)`

**On acceptance:** `k_vent_window` in the EWMA cache is updated with `k_env_vent`
(a cleaner ventilated estimate than the 1-param result, because solar contamination is
removed). `k_solar` in the EWMA cache is updated separately via the same EWMA mechanism.
**On rejection** (collinearity, bounds failure, or low R²): the standard 1-param OLS
result for `k_vent_window` is used and `k_solar` is not updated from this observation.

**`solar_factor` in samples:** From Issue #126, `solar_factor` is recorded in each
`ventilated_decay` sample dict at collection time (not computed at commit time). Old
sample dicts without a `solar_factor` key are treated as `0.0` — the 1-param fallback
fires because `sf_range` will be 0.0 < 0.30.

**Constant:** `THERMAL_SOLAR_FACTOR_MIN_RANGE = 0.30`

**Why adaptive (not a separate obs type):** Ventilated windows are often long-duration
open events. Splitting into separate obs types would require two concurrent windows that
start and stop on the same physical event, complicating the observation lifecycle.
Upgrading the existing `ventilated_decay` observation at commit time keeps the pipeline
simple — the 2-param path is a quality improvement, not a new signal collection mechanism.

**Thermal mass lag:** The clock-based `solar_factor` (sinusoidal, peaks at solar noon) is
an approximation. Real solar heat transfer lags the solar position by 30–90 minutes due
to thermal mass (walls, floors absorbing and re-radiating heat). This approximation is
acceptable because: (a) `k_solar` is used in predictions that integrate over hour-long
periods where lag averages out; (b) the EWMA smoothing (α = 0.05 at "low" grade) further
attenuates single-observation error; (c) a cloud-aware, lag-corrected solar model is
deferred to future scope.

**`_run_solar_phase_chart_log_fit()` — structured INFO logging (Issue #308):** This method
(`coordinator.py`) estimates `solar_phase_offset_h` from passive-daytime chart_log windows
(regime: HVAC off, fan off, windows closed, local hours 8–20). As of Issue #308 it emits
structured `INFO` log lines at three points useful for diagnosing solar phase offset
learning (Issue #185):

1. **Entry** — total chart_log entries available, date range scanned, and lookback window (2 days or 30 days for backfill).
2. **Window filtering** — count of passive-daytime windows found, or an "offset unchanged" message when zero qualify.
3. **EWMA update** — per-committed-window: observed offset, old→new EWMA value, and window size. Final summary: `N/M windows committed (K rejected)`.

Individual window rejections are logged at DEBUG level with the reject reason.

##### Two-tier fit scheduling (Issue #310)

`_run_solar_phase_chart_log_fit()` is invoked on two distinct schedules so the EWMA stays current without redundant computation.

1. **One-shot backfill** (`backfill=True`, lookback up to 30 days): runs once on fresh install via `_solar_phase_backfill`, gated by a `backfill_done` flag persisted in coordinator state. This captures the full available chart_log history and produces an initial `solar_phase_offset_h` estimate before the first daily cycle runs.
2. **Periodic daily re-fit** (`backfill=False`, lookback 2 days): gated by `_last_solar_phase_fit_date` (persisted in coordinator state). Runs at most once per calendar day, and only after the one-shot backfill has completed. Each daily run folds the two most recent days of chart_log windows into the EWMA, keeping the phase offset current as new observations accumulate.

The two-tier design avoids re-scanning months of history on every coordinator cycle while ensuring that a newly deployed instance learns a reasonable phase offset from its first day of data.

#### 5e-vi. HVAC Commit Path — Single-Point Estimator and Proxy-Aware Gating (Issue #130)

Issue #130 fixed HVAC observations producing zero commits despite 60 days of heat cycles.
The root causes were: (RC1) 10-sample post-heat minimum requiring 50 min — too long for
5–30 min cycles; (RC3) `outdoor=None` at state transitions blocking sample collection;
(RC4) bridge homes with `k_passive=None` blocking `k_active` computation; (RC5) no
backfill tool.

**Fixes applied:**

| Fix | Mechanism |
|---|---|
| D14: Lower post-heat minimum | `THERMAL_MIN_POST_HEAT_SAMPLES`: 10 → 4 |
| D15: Remove stabilization gate | `_check_hvac_stabilization()` commits as soon as min samples reached; no ±0.3°F stability wait |
| D16: Outdoor temp fallback | `_last_known_outdoor_f` caches the last non-None outdoor reading; used within a 30-min window when current reading is `None` |
| D17: k_vent_window proxy | `_commit_event_from_dict()` uses `k_vent_window` as k_passive when `k_passive=None` (bridge homes); marks grade `"low"` |

**Single-point `k_active` estimator (`compute_k_active_single_point()`):**

When `n_active < 2` (cycle is shorter than the 5-min sampling interval), OLS cannot fit a
heating rate. The single-point estimator uses exact HVAC on/off timestamps:

```
k_active = (T_peak − T_start) / elapsed_hours − k_passive × avg(T_in − T_out)
```

`elapsed_hours` comes from state-change timestamps, not sample spacing, so it reflects the
true HVAC-on duration. `post[0].ts` is used as the HVAC-off timestamp when `n_active=1`.

**Signal guard (`THERMAL_HVAC_MIN_SIGNAL_F = 0.5°F`):** If `|T_peak − T_start| < 0.5°F`,
the cycle is rejected as a setpoint-maintenance run — no learnable k_active information.

**Call path:** `_commit_event_from_dict()` first attempts OLS via `compute_k_active()`.
If OLS returns `None` (insufficient samples), it falls through to
`compute_k_active_single_point()`. When bridge proxy was used for k_passive, the obs dict
emits `k_passive=None` to prevent the proxy value from contaminating the k_passive EWMA
(D21).

**Proxy-aware `n_post` gating:** `_check_hvac_stabilization()` reads `k_vent_window` from
`thermal_model_cache` at commit time. When `k_vent_window` is available and negative
(proxy present), the `n_post` minimum drops from `THERMAL_MIN_POST_HEAT_SAMPLES` (4) to 1
and the plateau guard is bypassed. For all other homes, the thresholds are unchanged.

| Condition | `n_post` minimum | Plateau guard |
|---|---|---|
| No proxy (normal or fresh install) | 4 | Active — rejects if `peak − end < 0.3°F` |
| Proxy available (`k_vent_window < 0`) | 1 | Bypassed |

**`thermal_replay --hvac` mode:** `run_hvac_replay_ols()` in `tools/thermal_replay.py`
applies the same OLS → single-point fallback and proxy-aware gating to historical chart_log
data. Use for backfilling HVAC observations after deploying Issue #130 fixes.

```bash
python tools/thermal_replay.py --hvac --days 60 --dry-run   # inspect without writing
python tools/thermal_replay.py --hvac --days 60             # commit to learning DB
```

**Implementation references:**

| Component | Location |
|---|---|
| `compute_k_active_single_point()` | `learning.py` ~line 401 |
| Single-point fallthrough in `_commit_event_from_dict()` | `learning.py` ~line 1145 |
| Proxy-aware gate in `_check_hvac_stabilization()` | `coordinator.py` ~line 2951 |
| `run_hvac_replay_ols()` | `tools/thermal_replay.py` ~line 817 |

#### 5e-vii. Thermostat Swing — Deadband Auto-Detection (Issue #102)

**Formula:** `swing_f = abs(T_end - T_start) / 2`

**Bounds:**
| Parameter | Min | Max | Notes |
|---|---|---|---|
| `swing_heat_f` | 0.1°F | 5.0°F | `THERMAL_SWING_MIN_F` / `THERMAL_SWING_MAX_F` |
| `swing_cool_f` | 0.1°F | 5.0°F | Same bounds, independent EWMA |

**Minimum signal:** `abs(T_end - T_start) >= THERMAL_HVAC_MIN_SIGNAL_F` (0.5°F).
Cycles below this produce no swing observation.

**Unit conversion:** Swing is a temperature delta — use `convert_delta()` (multiply
by 5/9 for Celsius), never `from_fahrenheit()`. The +32 offset does not apply.

**Display rule:**
- `swing_heat_f is None` → show `±1.5°F (estimated)` in gray italic
- `swing_heat_f is not None` → show `±X.X°F` with no hint

**Constants:**
| Constant | Value | Purpose |
|---|---|---|
| `THERMAL_SWING_DEFAULT_F` | 1.5 | Default before any learning |
| `THERMAL_SWING_MIN_F` | 0.1 | Sanity lower bound |
| `THERMAL_SWING_MAX_F` | 5.0 | Sanity upper bound (rejects multi-cycle blur) |
| `THERMAL_SWING_CONF_LOW` | 1 | none → low threshold |
| `THERMAL_SWING_CONF_MEDIUM` | 3 | low → medium threshold |
| `THERMAL_SWING_CONF_HIGH` | 10 | medium → high threshold |

#### 5e-viii. Solar Phase Offset — Two-EWMA Architecture (Issue #312)

The solar phase offset (`solar_phase_offset_h`) corrects the `solar_factor` sinusoid so it peaks at the hour where heat actually reaches the interior rather than at a fixed 1 pm clock-noon. Two independent EWMAs learn this offset from different signal sources; a resolver selects the best available value at call time.

**Two-EWMA architecture:**

| EWMA | Cache key | Alpha | Source | Trust |
|---|---|---|---|---|
| Primary | `solar_phase_offset_h` | 0.10 (`THERMAL_SOLAR_PHASE_ALPHA`) | Passive-decay chart_log windows (`_run_solar_phase_chart_log_fit()`) | Higher — measures thermal response directly, no confound |
| Secondary | `solar_phase_offset_ac_h` | 0.07 (`THERMAL_SOLAR_PHASE_AC_ALPHA`) | AC duty cycle peak hour (`_run_ac_duty_solar_phase_fit()`) | Lower — AC cycling is an indirect proxy; alpha is slower to reflect this |

The two EWMAs never cross-update: `update_solar_phase_offset()` in `learning.py` writes only `solar_phase_offset_h`; `update_ac_duty_solar_phase_offset()` writes only `solar_phase_offset_ac_h`.

**Resolver — `_resolve_solar_phase_offset(cache)` (`learning.py`):**

Each EWMA stores a `last_obs_date` field (`solar_phase_offset_last_obs_date`, `solar_phase_offset_ac_last_obs_date`). A parameter is **stale** if its date is absent or older than `THERMAL_PARAM_STALE_DAYS` (90 days). Stale home-specific data is still preferred over a generic default — the default is only used when nothing has ever been learned.

```
1. primary = cache["solar_phase_offset_h"]
   if primary is not None AND fresh (within 90 days) → return primary   ← preferred

2. secondary = cache["solar_phase_offset_ac_h"]
   ac_obs = cache["solar_phase_offset_ac_obs_count"]
   if secondary is not None AND ac_obs >= 3 AND fresh → return secondary

3. if primary is not None (stale) → return primary                       ← best stale data

4. if secondary is not None AND ac_obs >= 3 (stale) → return secondary  ← next best stale

5. return THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT (2)                       ← nothing ever learned
```

The secondary requires at least 3 accepted observations (`THERMAL_SOLAR_PHASE_AC_MIN_OBS`) before it is trusted in either the fresh or stale tier. The default prior (`THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT = 2`, peak at 3 pm local) is only returned when both EWMAs have never received an accepted observation.

**Staleness principle (applies to all computed thermal parameters — see Issue #314):** A learned value is only "current" if it was recently observed. An EWMA frozen by seasonal inactivity is stale, but it still encodes home-specific geometry and is a better estimate than a generic prior. The resolver prefers fresh > stale > default, with primary (passive method) winning within each tier.

`get_thermal_model()` returns `solar_phase_offset_h` as the **resolved** value — call sites (ODE, chart) receive the best available estimate without needing to know which source was used. The raw secondary EWMA is also exposed as `solar_phase_offset_ac_h` for diagnostic inspection.

**AC duty cycle quality filter — `_is_ac_duty_solar_day(day_entries)` (`coordinator.py`):**

The AC duty method is only meaningful on days where the thermostat was cooling steadily in the midday window. Days that don't meet the quality criteria are rejected before estimation.

| Gate | Criterion | Constant | Reject code |
|---|---|---|---|
| 1 | At least one 11:00–18:00 chart_log entry has a `setpoint_cool` field | — | `ac_no_cool_setpoints` |
| 1b | All setpoints in [68, 80]°F | `THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN/MAX_F` | `ac_setpoint_out_of_range` |
| 2 | Setpoint spread across 11:00–18:00 < 1.5°F | `THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F` | `ac_setpoint_unstable` |
| 3 | ≥ 4 cool entries in 11:00–16:00 | `THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES` | `ac_insufficient_midday_activity` |
| 4 | At least one 11:00–16:00 entry has indoor > median setpoint | — | `ac_no_setpoint_breach` |

Gates are evaluated in order; the first failure returns `(False, reject_code)`. A day passing all gates returns `(True, "")`. The function is a pure module-level helper — no coordinator state.

**Estimation — `_estimate_ac_duty_solar_phase(day_entries)` (`coordinator.py`):**

1. For each hour in 11:00–16:00, compute `duty_fraction = cool_entries / total_entries`.
2. Identify `peak_hour = argmax(duty_fraction)`.
3. `offset = peak_hour − 13`, clamped to `[THERMAL_SOLAR_PHASE_OFFSET_MIN (0), THERMAL_SOLAR_PHASE_OFFSET_MAX (4)]`.

A `peak_hour` of 14 (2 pm) yields `offset = 1`; a peak at 16 (4 pm) yields `offset = 3`. Returns `None` if no cool entries exist in the window (should not occur after gate 3, but guards the return).

**Integration — `_run_ac_duty_solar_phase_fit()` (`coordinator.py`):**

Called once per coordinator update cycle (inside the `_run_solar_phase_chart_log_fit()` block). Iterates chart_log entries grouped by date. For each date, calls `_is_ac_duty_solar_day()` then `_estimate_ac_duty_solar_phase()`; on success calls `learning.update_ac_duty_solar_phase_offset(offset, date_str)`. Rejection reasons are logged at DEBUG level; accepted estimates at INFO.

**Constants summary:**

| Constant | Value | Purpose |
|---|---|---|
| `THERMAL_SOLAR_PHASE_AC_ALPHA` | 0.07 | Secondary EWMA smoothing factor |
| `THERMAL_SOLAR_PHASE_AC_MIN_OBS` | 3 | Minimum observations before secondary is trusted by resolver |
| `THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F` | 68.0 | Setpoint range lower bound |
| `THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F` | 80.0 | Setpoint range upper bound |
| `THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F` | 1.5 | Max allowed setpoint spread across 11:00–18:00 |
| `THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES` | 4 | Min cool entries in 11:00–16:00 to qualify |
| `THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT` | 2 | Default prior (resolves to 3 pm peak) |
| `THERMAL_SOLAR_PHASE_OFFSET_MIN` | 0 | Offset clamp lower bound |
| `THERMAL_SOLAR_PHASE_OFFSET_MAX` | 4 | Offset clamp upper bound (5 pm peak) |

**Test coverage:** `tests/test_solar_phase.py` — `TestAcDutySolarPhase` (quality filter reject paths, estimation, EWMA update, resolver priority).

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
| `handle_bedtime()` | Apply bedtime setback | **Skip** (away setback maintained by 30-min `apply_classification()` cycle) | Skip (vacation setback preserved) |
| `_set_temperature_for_mode()` (safety net) | Apply comfort | Redirect → `handle_occupancy_away()` | Redirect → `handle_occupancy_vacation()` |

The `_set_temperature_for_mode()` safety net catches all indirect callers (door/window resume, grace expiry, economizer deactivation) so comfort temps are never applied while away/vacation.

**Paused-by-door guard (Fix #339):** When `_paused_by_door=True`, `handle_occupancy_away()` and `handle_occupancy_vacation()` record `_occupancy_mode` but skip the setback band call and return early. HVAC stays off. The setback is applied when sensors close and the resume path runs `_set_temperature_for_mode()`, which the safety net above redirects to the appropriate occupancy handler. Event emitted: `occupancy_setback_suppressed_paused` with payload `{occupancy: "away"|"vacation", reason: "paused_by_door"}`.

**`handle_bedtime()` skip paths — HVAC mode off (mild/warm nights):** When the current day classification has `hvac_mode = "off"` (mild or warm day, no heating/cooling required), `handle_bedtime()` logs a skip and emits a `bedtime_setback_skipped` event. No setpoint change is made — the comfort floor for the following morning is protected by the 30-min `apply_classification()` guard in §6b rather than a bedtime setpoint.

**Structured skip events (Issue #151):** All skip paths emit `bedtime_setback_skipped` to the event log with a `reason` field:

| `reason` value | Trigger condition |
|---|---|
| `"occupancy"` | `_occupancy_mode` is `away` or `vacation` at bedtime |
| `"manual_override"` | `_manual_override_active` is set (Issue #204) — bedtime setback is skipped to respect the user's revealed preference rather than fighting their manual adjustment |
| `"hvac_off"` | Classification `hvac_mode` is not `heat` or `cool` (mild/warm night) |
| `"no_classification"` | No current classification available at bedtime time |

Fire paths emit `bedtime_setback` with `{mode, target_f, depth_f, adaptive, modifier}`. Both event types are visible in the AI investigator's structured event log.

**Occupancy and wakeup events (Issue #240):** The following events are emitted by occupancy handlers when a setpoint change is actually applied, making these actions visible in the dashboard timeline and AI activity report:

| Event type | Handler | Condition | Payload |
|---|---|---|---|
| `occupancy_setback` | `handle_occupancy_away()` | Cool or heat thermostat mode — setpoint applied | `{mode: "cool"\|"heat", target_f: float, occupancy: "away"}` |
| `occupancy_setback` | `handle_occupancy_vacation()` | Cool or heat thermostat mode — setpoint applied | `{mode: "cool"\|"heat", target_f: float, occupancy: "vacation"}` |
| `occupancy_comfort_restored` | `handle_occupancy_home()` | Classification `hvac_mode` is `heat` or `cool` | `{mode: "cool"\|"heat", target_f: float}` (comfort setpoint) |
| `morning_wakeup` | `handle_morning_wakeup()` | Classification `hvac_mode` is `heat` or `cool` | `{mode: "cool"\|"heat", target_f: float}` (comfort setpoint) |

No event is emitted when HVAC is `off` (mild/warm day) — no setpoint change occurs in those cases. All four event types are categorised as `source_label=automation` by `_event_source_label()` in `ai_skills_activity.py`. The skip path (HVAC off, occupancy away at wakeup) continues to emit `morning_wakeup_skipped` as before.

**DailyRecord setback fields (Issue #151):** `handle_bedtime()` writes the following fields to `DailyRecord` on every bedtime pass — fire or skip:

| Field | Type | Set when | Value |
|---|---|---|---|
| `setback_heat_applied_f` | `float \| None` | Fire path, heat mode | Applied heat setback setpoint (°F) |
| `setback_cool_applied_f` | `float \| None` | Fire path, cool mode | Applied cool setback setpoint (°F) |
| `setback_depth_f` | `float \| None` | Fire path | Depth of setback from comfort setpoint (°F) |
| `setback_was_adaptive` | `bool \| None` | Fire path | `True` when thermal model drove the depth; `False` for default |
| `setback_skipped_reason` | `str \| None` | Skip path | One of `"occupancy"`, `"manual_override"`, `"hvac_off"`, `"no_classification"` |

All five fields default to `None` at record creation. On a fire night, `setback_skipped_reason` stays `None`; on a skip night, all applied-value fields stay `None`. Accessible via `learning_db.py --daily` (see §Diagnostic Tools).

**Test coverage:** `tests/test_occupancy_automation.py` — 18 tests covering all cells above; `tests/test_bedtime_setback.py` — full fire/skip/field coverage.

---

> **DailyRecord Persistence Invariant (Issue #176)**
>
> `DailyRecord` counters accumulate throughout the day and are persisted to
> `climate_advisor_state.json`. When `_async_send_briefing()` creates an updated record
> after classification (e.g., after HA restart), it **MUST preserve all already-accumulated
> counters** from the existing same-day record before replacing it.
>
> Fields that must be preserved:
> `hvac_runtime_minutes`, `comfort_violations_minutes`, `manual_overrides`,
> `thermal_session_count`, `occupancy_away_minutes`, `windows_opened`,
> `window_open_actual_time`, `override_details`.
>
> **Violation:** creating a fresh `DailyRecord(...)` unconditionally resets all counters to
> zero, causing `hvac_runtime_today` to show `0.0` after a mid-day HA restart.
>
> **Fix pattern:** before constructing the new record, check whether `self._today_record`
> already exists for today's date, and carry forward all accumulated counter fields into
> the new `DailyRecord(...)` constructor call. Additionally, `_async_save_state()` must be
> called on every HVAC on→off transition (after `_flush_hvac_runtime()`) so that state is
> never more than one HVAC cycle stale at restart time.
>
> **Test coverage:** `tests/test_daily_record_accuracy.py` —
> `test_daily_record_survives_briefing_after_restart`

---

### 6b. Warm-Day Comfort-Floor Guard _(passive safety backstop — Issue #249)_

> **Issue #249 role change:** This guard is no longer the primary defense against the home falling below the comfort floor on warm/hot days. The comfort-band model (§6e) arms the thermostat with an explicit heat floor (`setback_heat` or `comfort_heat` depending on context) as part of every scheduled state update — the thermostat will heat the home back up without CA polling. §6b remains as a lightweight always-on safety net that fires if the band is somehow not in place or the floor is transiently breached during a transition.

When `apply_classification()` runs and the day type is `warm` or `hot` and the indoor temperature is below `comfort_heat`, the automation engine applies a comfort-floor guard to prevent the home from sitting below the comfort floor.

| Condition | Action | Event emitted |
|---|---|---|
| `day_type in (warm, hot)` AND `indoor_temp < comfort_heat` | Set HVAC to `heat`, target = `comfort_heat` (backstop) | `warm_day_comfort_gap` |
| `day_type in (warm, hot)` AND `indoor_temp >= comfort_heat` | Apply comfort band normally (§6e) | — |
| `day_type in (warm, hot)` AND indoor temp unavailable | Apply comfort band normally (fail-open) | — |

**Why this guard still exists (as backstop):** Even with the band armed, a mid-cycle transition (HA restart, manual mode change, thermostat reconnect) can briefly leave the home below the comfort floor before the next 30-minute cycle re-arms the band. §6b catches that window and fires a `warm_day_comfort_gap` event so the situation is visible in the event log.

**Primary defense (Issue #249):** The comfort-band model in §6e arms the heat floor on every `apply_classification()` call — `comfort_heat` while the occupant is home + awake (any day type), or the setback floor when away/asleep. The thermostat holds that floor autonomously between 30-minute cycles — no supervisor polling needed for normal operation. §6b activates only when the band has lapsed.

**Interaction with occupancy guards:** The comfort-floor heat command goes through `_set_temperature_for_mode()`, so occupancy-away and vacation redirection (§6a) still applies.

**Event frequency — `warm_day_state_confirmed` / `warm_day_setback_applied`:** `warm_day_state_confirmed` fires on every 30-minute coordinator update cycle while the thermostat is already in the correct warm-day state — not once per day. Sixty or more firings in 48 hours is expected on a sustained warm day; this is a heartbeat, not a loop or a bug. `warm_day_setback_applied` fires only when an actual setpoint or mode change is made, which is infrequent.

**Event frequency — `incident_detected`:** Emitted at most once per 30-min cycle per incident class (deduplicated within each call to `_detect_and_emit_incidents()`). The proactive variant (`setpoint_mode_inconsistency`) may fire at command time inside `_set_temperature()` rather than post-cycle, once per inconsistent command issued. See [Incident Classes](incident-classes.md) for the full list of classes and their detection timing.

**Test coverage:** `tests/test_warm_day_comfort_gap.py`

### 6c. Warm-Day ODE Ceiling Guard (Issue #136) _(passive safety backstop — Issue #249)_

> **Issue #249 role change:** This guard is no longer the primary defense against the home exceeding `comfort_cool` on warm/mild days. The comfort-band model (§6e) arms the thermostat with an explicit cool ceiling (`comfort_cool`) as part of every scheduled state update — the thermostat will cool the home back down without CA polling the ODE. §6c remains as a lightweight always-on safety net. In normal operation the ODE curve, built against the armed setpoint, predicts no breach — so the guard is naturally dormant. It activates only when the band has lapsed (HA restart, manual override, thermostat reconnect) or when outdoor conditions change sharply mid-cycle before the next 30-minute re-arm.

When the day classification is `warm` or `mild` and the thermal model has a calibrated `k_passive`, the automation engine evaluates a **ceiling guard** on every 30-minute coordinator cycle. The guard fires proactively to prevent indoor temperature from breaching `comfort_cool` in situations where the comfort band is not currently holding.

#### Purpose

The guard closes the "read-render split" gap: `_build_predicted_indoor_future()` feeds the chart every 30 min with an accurate indoor forecast, but prior to Issue #136 that forecast was never routed into `apply_classification()`. The ceiling guard routes it: if the ODE curve predicts a `comfort_cool` breach and free cooling cannot keep up, the guard sets HVAC to `cool` at `comfort_cool` before (or as soon as) the breach occurs.

With the comfort band armed (Issue #249), the ODE curve is constructed against the armed ceiling setpoint and therefore predicts no breach under normal conditions — the guard is dormant. It becomes active again if the band lapses for any reason.

#### Dormancy: when the guard defers to free cooling (3-condition — Issue #247)

The guard goes **dormant** (defers to natural ventilation) only when **all three** of these hold:

1. `outdoor <= indoor` — outdoor air can in principle cool the home, **and**
2. `self._natural_vent_active` — windows are actually open and nat-vent is running (not merely *eligible*), **and**
3. `indoor <= ceiling threshold` — indoor is still at/under the ceiling, so free cooling is keeping up.

If any condition fails, the guard **evaluates** (and fires if the breach scan confirms a breach):

- **indoor already exceeds the ceiling** — the #247 reactive case: solar/internal gains are out-pacing the breeze, so the guard escalates to AC **even though `outdoor < indoor`**. Free cooling stays the first remediation; AC fires only when ventilation is demonstrably losing.
- **nat-vent is NOT running** (windows closed, fan override) — the #215 case: do not defer to a ventilation that is not happening.
- **outdoor has risen above indoor** — the original #136/#218 path (airflow would add heat).

> **Regression note:** Issue #218 specified this 3-condition dormancy *plus* the escalation-on-fire that clears nat-vent, but the committed fix (`676daa4`) landed only the escalation half. The dormancy stayed one-condition (`outdoor <= indoor`), so on a day where outdoor stayed below indoor the guard never woke and the escalation code was unreachable — the home sat above the ceiling for hours (re-filed as #247). The escalation-on-fire is now reachable because the dormancy correctly lifts.

**`aggressive_savings` widens the ceiling threshold.** In normal mode the ceiling threshold is `comfort_cool`. In `aggressive_savings` mode it is `comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F` (2.0°F) — savings homes tolerate a small overshoot before paying for the compressor, but are still rescued from a real comfort failure once indoor exceeds that wider threshold.

#### `_ceiling_threshold()` is archetype-aware (Issue #392 Fix 1)

The ceiling threshold used in the dormancy check (condition 3 above) is computed by `_ceiling_threshold(comfort_cool)` in `automation.py`, not inlined. The helper returns a different answer depending on the configured fan archetype:

```python
def _ceiling_threshold(self, comfort_cool: float | None) -> float | None:
    fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
    if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return None
    if comfort_cool is None:
        return None
    aggressive = bool(self.config.get("aggressive_savings", False))
    return comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F if aggressive else comfort_cool
```

| fan_mode | Return value | Why |
|---|---|---|
| `FAN_MODE_HVAC` | `comfort_cool` (or `comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F` if `aggressive_savings`) | The HVAC blower and the compressor **coexist** — the comfort band stays armed the whole time nat-vent is active (§6e / Issue #249), so handing off to AC once indoor crosses the ceiling is safe and correct. Nothing fights: the thermostat itself decides whether the compressor needs to run. |
| `FAN_MODE_WHOLE_HOUSE` / `FAN_MODE_BOTH` | `None` | A whole-house fan (WHF) is **mutually exclusive** with the compressor by construction (`_activate_fan()` forces HVAC to `off` while a WHF session is active; see §9). A WHF is also physically guaranteed to keep converging toward outdoor temperature for as long as `outdoor < indoor` — the ceiling number says nothing about whether the WHF *will* succeed, only about how long it will take. So there is no ceiling-based handoff point for WHF: convergence is governed purely by outdoor/indoor direction, not by how far indoor has drifted above `comfort_cool`. |

The ODE ceiling guard's dormancy check (condition 3, above) treats `ceiling_threshold_val is None` as "ceiling condition satisfied" — i.e. for WHF, dormancy collapses to `outdoor <= indoor AND _natural_vent_active` (no ceiling term at all). For `FAN_MODE_HVAC`, dormancy still requires `indoor <= ceiling_threshold` exactly as before Issue #392.

**Why this had to change (Root Cause of Issue #392):** before this fix, the guard applied the same ceiling-based dormancy rule to both archetypes. For `FAN_MODE_WHOLE_HOUSE`, this meant that once indoor ticked one degree past `comfort_cool`, the guard would escalate to `cool` — which deactivates the WHF and forces HVAC to `cool` (per the mutual-exclusion contract) — even though outdoor was still comfortably below indoor and the WHF would have converged on its own. The very next reactivation check (any of the four gate sites in §17) would then see `outdoor < indoor` still holds and turn the WHF back on, which forces HVAC back to `off`, undoing the guard's `cool` command. That produced the `off→cool→off→cool` oscillation reported in #392 (repeating roughly every 5 minutes between 18:53 and 18:58). Making `_ceiling_threshold()` archetype-aware removes the false ceiling trigger for WHF entirely — see §17 for the matching change to the four reactivation gate sites, and "Structural WHF/AC Mutual Exclusion" below (§9, Issue #392 Fix 1b) for the structural guard that also closes a related but separate gap (mutual exclusion not being enforced everywhere HVAC mode is written).

**Test coverage (Issue #392):** `tests/test_nat_vent_activation.py`, `tests/test_fan_control.py`, `tests/test_whole_house_fan_hvac_suppression.py` — exact function names pending as of this doc pass; see those files directly for current coverage of archetype-aware ceiling behavior.

**On escalation the guard clears nat-vent** (Issue #218 part 2): if `_natural_vent_active` is true when the guard fires, it deactivates the fan, sets `_natural_vent_active = False`, and emits `nat_vent_ceiling_escalation` before switching to `cool` — so free cooling does not fight the compressor.

#### Guard conditions

| Condition | Action |
|---|---|
| `k_passive is None` OR `k_passive >= 0` | Skip — no calibrated passive model |
| `confidence_k_passive == "none"` AND NOT bridge home | Skip — model not yet trustworthy |
| Occupancy away or vacation | Skip — handled by upstream occupancy guards (§6a) |
| `predicted_indoor` is empty or None | Skip — no ODE curve available (fresh install, no physics gate) |
| Outdoor temp unavailable or missing | Skip |
| `outdoor <= indoor` **AND** `_natural_vent_active` **AND** `indoor <= ceiling threshold` | Dormant — free cooling is actually viable; guard defers to nat-vent (see 3-condition dormancy below) |
| `_find_ceiling_breach_time()` returns None | Dormant — no breach predicted above threshold |
| Bridge home (`k_passive_via_bridge=True`) | Apply `+CEILING_BRIDGE_TOLERANCE_F (1.0°F)` tolerance; guard fires at `comfort_cool + 1.0°F` |
| `k_active_cool` not learned (None) | Guard fires with `CEILING_PRECOOL_FALLBACK_MIN = 120` min lead time |
| All conditions met | Evaluate lead time; fire if breach is within window |

#### `_find_ceiling_breach_time()` — module-level helper in `coordinator.py`

Scans `predicted_indoor` (a list of `{"ts": ISO-string, "temp": float}` dicts from the ODE curve) for the first entry where `temp > comfort_cool + tolerance`. Returns the `datetime` of that entry, or `None` if no entry exceeds the threshold or the curve is empty.

```
signature: _find_ceiling_breach_time(predicted_indoor, comfort_cool, tolerance=0.0) → datetime | None
```

The guard inlines this scan inside `automation.py`'s `apply_classification()` to avoid a circular import between `automation.py` and `coordinator.py`. The standalone function in `coordinator.py` is used by `tests/test_prediction.py` and the morning briefing path.

#### Lead time formula

When the breach timestamp is found, the guard computes how far in advance to start cooling:

```
if k_active_cool is not None and abs(k_active_cool) > 0:
    lead_time_min = ((comfort_cool − current_indoor) / abs(k_active_cool)) × 60 × 1.3
else:
    lead_time_min = CEILING_PRECOOL_FALLBACK_MIN  # 120 min

lead_time_min = clamp(30, 240)
```

The `1.3×` safety margin ensures cooling begins early enough even on hotter-than-modeled days. The clamp floor (30 min) prevents firing immediately on a trivially small delta; the clamp ceiling (240 min) prevents over-committing 4+ hours in advance.

**`k_active_cool = None` is the normal case** for any home in its first cooling season (including non-bridge homes that have never recorded a cooling cycle). The 120-minute fallback is the common path, not an edge case.

#### Fire condition

```
if hours_to_breach <= lead_time_min / 60:
    → set HVAC to "cool", target = comfort_cool
    → emit "ceiling_guard_fired" event
```

HVAC is set to `cool` at `comfort_cool` (not below — this is ceiling defense, not pre-cooling below comfort). The target deliberately avoids the `-2°F` offset used for hot-day pre-conditioning (§4).

#### Weather-change resilience (stateless design)

The guard is fully stateless — no `_ceiling_precool_scheduled` flag. On each 30-min cycle, `apply_classification()` recomputes the ODE curve from fresh forecast data and re-scans for breach. Consequences:

- **Forecast improves** (cold front arrives, outdoor temperature drops): `_find_ceiling_breach_time()` returns `None` → guard goes dormant automatically on the next cycle, no cancellation logic needed.
- **Forecast worsens** (heat dome arrives): breach crosses into the lead time window → guard fires on the cycle when it first qualifies.
- **HVAC already cooling** (guard fired on a prior cycle): warm-day classification (`hvac_mode="off"`) will naturally stop cooling on the next cycle once indoor drops below `comfort_cool`, because the comfort-floor guard (§6b) will not re-heat at that point.

#### Bridge home behavior

Bridge homes use `k_vent_window` as a proxy for `k_passive`. The `k_passive_via_bridge=True` flag causes the guard to apply `CEILING_BRIDGE_TOLERANCE_F = 1.0°F` tolerance, requiring the predicted curve to exceed `comfort_cool + 1.0°F` before the breach is recorded. This accounts for the proxy being measured under ventilated conditions, which is less accurate for the closed-window heat-approach phase.

#### Constants

| Constant | Value | Purpose |
|---|---|---|
| `CEILING_PRECOOL_FALLBACK_MIN` | `120` | Lead time (minutes) when `k_active_cool` is not learned |
| `CEILING_BRIDGE_TOLERANCE_F` | `1.0` | Extra °F threshold for bridge homes |
| `CEILING_ESCALATION_SAVINGS_MARGIN_F` | `2.0` | Overshoot tolerated above `comfort_cool` before escalating in `aggressive_savings` mode (Issue #247) |

All three are defined in `const.py`.

#### Interaction with §6b comfort-floor guard

The ceiling guard runs **after** the comfort-floor guard in `apply_classification()`. The comfort-floor guard runs inside the `hvac_mode == "off"` branch; the ceiling guard is a separate block also gated by `classification.hvac_mode == "off"`, so it evaluates regardless of whether the floor guard fired.

In practice the two guards do not conflict: if indoor is below `comfort_heat` (floor guard fires), indoor is well under `comfort_cool`, so `_find_ceiling_breach_time()` finds no breach above the ceiling and the ceiling guard is dormant via that row (regardless of the 3-condition dormancy). A home simultaneously below the comfort floor and predicted to breach the ceiling is a degenerate condition that resolves naturally — the floor guard heats, the next cycle re-evaluates both guards with updated temperatures.

#### Emitted event

`ceiling_guard_fired` — payload: `{breach_time: ISO, hours_to_breach: float, lead_time_min: int}`. Visible in the Daily Record's event list. Used by the morning briefing to determine pre-cool start time for the warm-day narrative (§Part 2 of the plan).

**Test coverage:** `tests/test_warm_day_comfort_gap.py` — `TestCeilingDefenseActive`, `TestCeilingPreCoolFallback`, `TestCeilingWeatherChange`, `TestCeilingBridgeTolerance`, `TestCeilingDefenseManualOverride`. `tests/test_prediction.py` — `TestFindCeilingBreachTime`.

---

### 6d. MILD Day Dynamic Window Close Time (Fix C, Issue #147)

Prior to Issue #147, MILD day window scheduling used hardcoded `time(10, 0)` (open) and `time(17, 0)` (close) in `classifier.py`. These values were magic literals that could not be overridden by the thermal model, even on days when the ODE could predict the actual indoor–outdoor crossover time.

#### Before Fix C

```python
# classifier.py (pre-v0.3.46) — lines 118–119
self.window_open_time = time(10, 0)   # always 10am
self.window_close_time = time(17, 0)  # always 5pm
```

These literals were correct as a starting guess but systematically incorrect for any home whose indoor–outdoor crossover does not fall at 5pm.

#### After Fix C

**Constants moved to `const.py`:**

```python
MILD_WINDOW_OPEN_HOUR = 10    # MILD-day window open fallback (was hardcoded in classifier.py)
MILD_WINDOW_CLOSE_HOUR = 17   # MILD-day window close fallback
```

**`classifier.py` now uses the constants:**

```python
self.window_open_time = time(MILD_WINDOW_OPEN_HOUR, 0)
self.window_close_time = time(MILD_WINDOW_CLOSE_HOUR, 0)
```

**`briefing.py` applies ODE timing when available:**

The `_derive_warm_day_events()` function (which computes `nat_vent_cutoff` and `ceiling_breach_time` from the predicted indoor and outdoor curves) is extracted into a shared helper `_derive_natural_vent_events(predicted_indoor_future, predicted_outdoor_future, comfort_cool, k_active_cool)`. This helper is called from the MILD day briefing path as well as the warm day path.

When the ODE is available (thermal model calibrated, physics gate eligible):
- MILD day window close time = `nat_vent_cutoff` (the hour when outdoor temp ≥ indoor − 1°F)
- Fallback when ODE unavailable = `time(MILD_WINDOW_CLOSE_HOUR, 0)` (5pm)

#### Impact Cascade from Solar Phase Offset Correction

The following cascade applies to both warm and MILD days when `solar_phase_offset_h` is correctly learned:

1. `solar_phase_offset_h` corrects `_solar_factor` → ODE models solar input peaking at 3–5pm instead of 1pm
2. ODE predicts indoor rise more slowly through the morning (less solar input before 3pm)
3. `nat_vent_cutoff` (the hour when outdoor ≥ indoor − 1°F) shifts **~1–2 hours later** → windows stay open longer, more free cooling is captured
4. `ceiling_breach_time` (the hour when indoor > `comfort_cool`) also shifts later → AC starts later
5. `precool_start_time` shifts with it → no wasted early AC run while natural ventilation still has capacity
6. **Net effect:** extended natural ventilation window, reduced AC runtime, improved energy efficiency

#### Decision Table

| Condition | MILD day open time | MILD day close time | Source |
|---|---|---|---|
| ODE unavailable (fresh install, no physics gate) | `time(MILD_WINDOW_OPEN_HOUR, 0)` | `time(MILD_WINDOW_CLOSE_HOUR, 0)` | `const.py` constants |
| ODE available, `nat_vent_cutoff` computable | `time(MILD_WINDOW_OPEN_HOUR, 0)` | `nat_vent_cutoff` (dynamic, ~12–17 depending on solar offset) | ODE curve |
| ODE available, `nat_vent_cutoff` returns None (outdoor always > indoor) | `time(MILD_WINDOW_OPEN_HOUR, 0)` | `time(MILD_WINDOW_CLOSE_HOUR, 0)` | Fallback |

The open time is always `MILD_WINDOW_OPEN_HOUR` (10am). Only the close time is dynamic.

#### Constants

| Constant | Value | File | Notes |
|---|---|---|---|
| `MILD_WINDOW_OPEN_HOUR` | `10` | `const.py` | Was hardcoded literal in `classifier.py:118` |
| `MILD_WINDOW_CLOSE_HOUR` | `17` | `const.py` | Was hardcoded literal in `classifier.py:119` |

**Test coverage:** `tests/test_solar_phase.py` — `TestMildDayDynamicScheduling`:
- `test_mild_day_uses_const_fallback_when_no_ode`
- `test_mild_day_close_time_uses_ode_crossover`
- `test_mild_day_constants_in_const_py`

---

### 6e. Comfort-Band Programming (Issue #249)

The home is held inside the comfort band continuously by the thermostat itself — recurring afternoon ceiling drift (Issues #136/#218/#247) becomes structurally impossible because the ceiling setpoint is always armed, not re-armed reactively 30 minutes later.

#### The One-Decision / One-Actuation Model

Every scheduled state handler (classification apply, bedtime, morning wakeup, occupancy change) does two things and only two things:

1. **Decide the band** — call `select_comfort_band(...)` to produce a `ComfortBand(floor, ceiling, active, reason)`.
2. **Actuate the band** — call `_apply_comfort_band(band)` to emit the right command shape for the thermostat's capabilities.

There is no `off` sentinel, no off+setback divergence, and no per-handler HVAC-mode branching. The thermostat's own deadband holds the home inside `[floor, ceiling]` between 30-minute cycles; CA's role is to keep the band programmed, not to supervise the thermostat every cycle.

#### `select_comfort_band` — Band-Edge Rules

`select_comfort_band(classification, config, *, occupancy_mode, in_sleep_window, aggressive_savings) → ComfortBand`

`ComfortBand(floor, ceiling, active, reason)` where `active ∈ {"ceiling", "floor"}`.

**Occupied + awake = the full comfort band.** While the occupant is home/guest and awake, the band is `[comfort_heat, comfort_cool]` on **any** day type — the "lazy posture" the thermostat runs itself with: it pre-heats the cold morning up to `comfort_heat` and cools the warm afternoon down to `comfort_cool`. Both edges are held at comfort; suppression to a setback edge happens **only** when away or asleep. The **`active`** field (`"ceiling"` on warm/hot/mild days, `"floor"` on cool/cold days) does **not** change the band for a dual thermostat — it only tells `_apply_comfort_band` which single edge a single-mode device should defend.

| Context | floor | ceiling | active | Notes |
|---|---|---|---|---|
| Home/guest — any day type (awake) | `comfort_heat` | `comfort_cool` | `"floor"` if heat day else `"ceiling"` | Full comfort band; thermostat pre-heats the morning and cools the afternoon |
| Home/guest — `aggressive_savings=True` | `comfort_heat − CEILING_ESCALATION_SAVINGS_MARGIN_F` | `comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F` | as above | BOTH edges widened so the system runs less |
| Home/guest — `hot` day, pre-cool **not yet achieved** (`_pre_condition_achieved=False`) | `comfort_heat` | `comfort_cool + pre_condition_target` (e.g. 73°F when `comfort_cool=75`) | `"ceiling"` | Classifier's negative offset lowers the ceiling until the target is reached |
| Home/guest — `hot` day, pre-cool **achieved** (`_pre_condition_achieved=True`) | `comfort_heat` | `comfort_cool` (e.g. 75°F) | `"ceiling"` | Normal ceiling restored; offset skipped for the rest of the day; flag resets daily |
| Sleep window (any day type) | `sleep_heat` | `sleep_cool` | `"floor"` (cool/cold) or `"ceiling"` (warm/hot) | Configured `sleep_heat`/`sleep_cool` band |
| Away occupancy | `setback_heat` | `setback_cool` | `"ceiling"` | Setback band — suppression only applies when nobody is home |
| Vacation occupancy | `setback_heat − VACATION_SETBACK_EXTRA` | `setback_cool + VACATION_SETBACK_EXTRA` | `"ceiling"` | Deep-setback band |

**`aggressive_savings` edge widening:** widens **both** comfort edges by `CEILING_ESCALATION_SAVINGS_MARGIN_F` (2.0°F) — `floor − margin`, `ceiling + margin` — so the system tolerates a wider band before heating or cooling. Setback and sleep bands are unaffected.

**Single-mode devices:** a cool-only thermostat defends the ceiling (it has no heat to give); a heat-only thermostat defends the floor. For these, `active` selects which comfort edge is armed; the other edge is simply not this device's job. A dual (`heat_cool`) thermostat holds both edges at comfort with one command.

#### `_apply_comfort_band` — Command Shapes

`_apply_comfort_band(band)` reads `self._get_thermostat_capabilities()` and emits exactly one service call (or none if the device cannot serve the active edge):

| Thermostat capability | Command shape |
|---|---|
| Dual (`heat_cool`) capable | `_set_hvac_mode("heat_cool")` (if mode changed) + `_set_temperature_dual(band.floor, band.ceiling)` — both edges sent every call; the unchanged side is reiterated automatically |
| Cool-capable, `active = "ceiling"` | `_set_hvac_mode("cool")` (if mode changed) + `_set_temperature(band.ceiling)` |
| Heat-capable, `active = "floor"` | `_set_hvac_mode("heat")` (if mode changed) + `_set_temperature(band.floor)` |
| Device cannot serve the active edge (e.g. heat-only thermostat on a warm day) | No-op — skip this cycle (defensive; not a fallback path) |

Mode changes are issued only when the thermostat is not already in the target mode — the existing idempotent `_set_hvac_mode` setter (line ~1258) enforces this. Dry-run mode is respected throughout.

**Emitted event:** `comfort_band_applied` — payload: `{floor, ceiling, active, mode, reason}`. Every call to `_apply_comfort_band` that results in a service call emits this event. Visible in the Daily Record's event list and the AI activity report.

**Bedtime / occupancy payloads updated:** `bedtime_setback`, `morning_wakeup`, `occupancy_setback` event payloads now also carry `floor/ceiling/active/mode` so the timeline shows the full band context, not just a single setpoint.

#### Nat-Vent and Economizer with the Band Armed

Natural ventilation and the economizer **no longer set `hvac_mode=off`** when they activate (Issue #249 Design §4). They manage only the fan; the comfort band remains armed throughout:

- **Nat-vent active (windows open, outdoor cooler than indoor):** fan on, `_natural_vent_active = True`, band re-armed via `_apply_nat_vent_hvac_state()` (see below). The thermostat self-arbitrates: if the breeze keeps the home below the ceiling, the compressor idles for free. If the breeze fails and indoor rises above `comfort_cool`, the thermostat cools without waiting for the next CA 30-minute cycle.
- **Economizer (both phases):** fan on (or HVAC fan mode), band unchanged. The band holds `comfort_cool`, so the economizer never sets the HVAC mode/setpoint (Issue #264) — cool-down assists with the fan while the band cools; maintain holds it via ventilation.
- **Escalation:** when the ODE ceiling guard (§6c) fires, nat-vent is cleared (`_natural_vent_active = False`) and a `nat_vent_ceiling_escalation` event is emitted — the band was already armed at the cool ceiling, so "escalation" means allowing the compressor to run rather than re-programming the setpoint.

**Why no more HVAC off on nat-vent:** Turning HVAC off on nat-vent activation disarmed the floor. If outdoor conditions changed mid-night (cold snap), CA would not re-heat until the next 30-minute cycle noticed the floor breach — up to 30 minutes of the home sitting below the comfort floor. With the band always armed, the thermostat heats immediately.

#### `_apply_nat_vent_hvac_state()` — Band Arming on Nat-Vent Activate (Fix #338)

`_apply_nat_vent_hvac_state()` is called at every nat-vent activation site — initial activation, re-activation from paused state, and on every 30-minute `apply_classification()` cycle while nat-vent is active — to ensure the correct band is armed alongside the running fan.

| Fan archetype | `aggressive_savings` | Sleep window? | Band armed | Rationale |
|---|---|---|---|---|
| `FAN_MODE_WHOLE_HOUSE` or `DISABLED` | any | any | No-op | HVAC already suppressed by fan activation path; no band to arm |
| `FAN_MODE_HVAC` only | `False` | **Yes** | No setpoint call — emits `nat_vent_ac_assist_armed` only | Sleep band applied by the subsequent `select_comfort_band(in_sleep_window=True)` call in `apply_classification()`; avoids redundant thermostat write at daytime comfort ceiling immediately overwritten by sleep ceiling (Issue #341) |
| `FAN_MODE_HVAC` only | `False` | No | Full comfort band at `[comfort_heat, comfort_cool]` ceiling | AC assists if the breeze cannot hold the ceiling; floor is re-armed on the next 30-min `apply_classification()` cycle |
| `FAN_MODE_HVAC` only | `True` | any | Floor-only: `heat` mode @ `comfort_heat`; ceiling disarmed | Running the compressor through open windows defeats the savings the user configured; occupant accepts ceiling drift if breeze fails |

**Sleep window deference (Issue #341):** When nat-vent is active during the sleep window and `aggressive_savings=False`, `_apply_nat_vent_hvac_state()` emits `nat_vent_ac_assist_armed` (so the status card and activity report show nat-vent active) but skips `_apply_comfort_band()`. The `select_comfort_band(in_sleep_window=True)` call immediately following in `apply_classification()` programs the thermostat with the sleep band (`sleep_heat`/`sleep_cool`). Without this guard, two conflicting setpoints were written every 30-min cycle all night: the daytime comfort ceiling first, then the sleep ceiling immediately after. The sleep ceiling won (applied last), but the thermostat received redundant writes and the activity report showed confusing dual entries.

**Path B fix (re-activation from paused state):** Before Fix #338, when nat-vent re-activated from a paused state (all conditions met again after the 300 s lockout), `_apply_nat_vent_hvac_state()` was not called on that path. The band was not re-armed until the next 30-minute `apply_classification()` cycle — a window of up to 30 minutes during which the thermostat ran with no CA-programmed ceiling. Fix #338 calls `_apply_nat_vent_hvac_state()` in `check_natural_vent_conditions()` at the re-activate node (§12b in the flowchart).

**Sensor-close fix (warm/mild days):** When all sensors close while nat-vent is active on a warm or mild day, `handle_all_doors_windows_closed()` re-arms the full comfort band immediately. Previously the warm/mild path skipped the re-arm because the `if c.hvac_mode in ("heat", "cool")` check failed for the classifier's `"off"` label — the thermostat ran without an armed ceiling for up to 30 minutes until the next `apply_classification()` cycle.

#### Scheduled Handlers That Use the Band

All scheduled state handlers now route through `select_comfort_band` + `_apply_comfort_band`. The old per-handler divergent off/heat/cool/setback bodies are replaced:

| Handler | Band context |
|---|---|
| `apply_classification()` (30-min cycle) | Daytime band, or sleep band when `_in_sleep_window()` matches |
| `handle_bedtime()` | Sleep band (`sleep_heat` / `sleep_cool` / adaptive) |
| `handle_morning_wakeup()` | Comfort band (home/guest) |
| `handle_occupancy_away()` | Setback band |
| `handle_occupancy_vacation()` | Deep-setback band |
| `_apply_current_scheduled_state()` | Comfort band for current time context |

#### Interaction with §6b and §6c

With the band armed, both the comfort-floor guard (§6b) and the ODE ceiling guard (§6c) are naturally dormant under normal conditions — the thermostat holds both edges between CA cycles. Both guards remain in place as lightweight always-on safety nets that activate if the band lapses (HA restart, manual override recovery, thermostat reconnect). Neither guard is gated or disabled; they simply find no condition to act on when the band is programmed.

#### Constants

| Constant | Value | Purpose |
|---|---|---|
| `CEILING_ESCALATION_SAVINGS_MARGIN_F` | `2.0°F` | Ceiling tolerance above `comfort_cool` for `aggressive_savings` mode |
| `VACATION_SETBACK_EXTRA` | `3°F` | Extra depth beyond normal away setback for vacation bands |

**Test coverage:** `tests/test_thermostat_program.py` (`select_comfort_band` band-edge rules across all occupancy / sleep / aggressive cases; `_apply_comfort_band` dual/cool/heat/no-op command shapes, idempotent mode, dry-run); `tests/test_warm_day_setback.py::TestWarmDayBandArming` + `tests/test_warm_day_comfort_gap.py` (warm-day band arming); `tests/test_occupancy_setback_mode.py`, `tests/test_occupancy_automation.py`, `tests/test_bedtime_setback.py` (handler band integration); `tests/test_window_hvac_interaction.py`, `tests/test_door_window.py`, `tests/test_fan_control.py`, `tests/test_economizer.py` (nat-vent/economizer band-stays-armed); `tests/test_production_harness.py` + `tools/simulations/golden/cold_morning_warm_day_no_breach.json`, `…/startup_indoor_below_heat_floor_warm_day.json` and the `p3_*` pending scenarios (end-to-end band arming on the real engine).

---

## 7. Window Recommendations

Window advice is set by the classifier at classification time, based on `day_type` and forecast lows.

| Day Type | Windows Recommended? | Open Time | Close Time | Condition |
|---|---|---|---|---|
| `hot` | Not a traditional recommendation — window *opportunities* only | 6:00 AM | 9:00 AM | Morning opportunity: `today_low <= 80` |
| `hot` | Evening opportunity | 5:00 PM | Midnight (00:00) | Evening opportunity: `tomorrow_low <= 80` |
| `warm` | Yes (if condition met) | 6:00 AM | 10:00 AM | `today_low <= comfort_cool - ECONOMIZER_TEMP_DELTA` = `today_low <= 72°F` (defaults) |
| `mild` | Always yes | 10:00 AM (`MILD_WINDOW_OPEN_HOUR`) | 5:00 PM (`MILD_WINDOW_CLOSE_HOUR`) or `nat_vent_cutoff` when ODE available | No condition — always recommended |
| `cool` | No | — | — | — |
| `cold` | No | — | — | — |

**Warm-day window condition formula:** `today_low <= DEFAULT_COMFORT_COOL - ECONOMIZER_TEMP_DELTA` = `75 - 3 = 72°F` at defaults. Constant: `WARM_WINDOW_OPEN_HOUR = 6`, `WARM_WINDOW_CLOSE_HOUR = 10`.

**MILD-day window times (v0.3.46+):** Open time is always `MILD_WINDOW_OPEN_HOUR = 10` (10:00 AM). Close time uses `nat_vent_cutoff` when the ODE is calibrated, otherwise falls back to `MILD_WINDOW_CLOSE_HOUR = 17` (5:00 PM). See [§6d. MILD Day Dynamic Window Close Time](#6d-mild-day-dynamic-window-close-time-fix-c-issue-147).

---

## 8. Economizer (Window Cooling on Hot Days)

The economizer uses open windows on hot days to make the band's cooling cheaper. Under the #249 band
model it is **fan-assist only**: the comfort band (§6e) holds `comfort_cool`, so the economizer no
longer sets the HVAC mode or setpoint (Issue #264) — it runs the fan to pull cool outdoor air through
the open window. It never overrides the band; there is no separate economizer on/off toggle (it is
gated purely by the eligibility conditions below).

### Eligibility

All of the following must be true simultaneously:

| Condition | Formula / Value |
|---|---|
| Day type | `day_type == hot` |
| Windows open | `windows_physically_open == True` |
| **Free-cooling direction** | **`outdoor_temp < indoor_temp`** (Issue #327) — outdoor air must be cooler than indoor; if outdoor ≥ indoor the fan would heat the house rather than cool it |
| Outdoor temp ceiling | `outdoor_temp <= comfort_cool + ECONOMIZER_TEMP_DELTA` = `outdoor_temp <= 78°F` (defaults) |
| Time window | 6:00–9:00 AM **or** 5:00 PM–midnight |

The free-cooling-direction guard (Issue #327) mirrors the identical guard already required by nat-vent activation (§17). It prevents evening activation on hot days when outdoor temperatures remain above indoor well into the evening — a scenario where the economizer would work against comfort rather than assist it.

### Phase Behavior

| Mode | aggressive_savings | Phase | Condition | Action |
|---|---|---|---|---|
| Normal | `False` | Phase 1: cool-down | `indoor_temp > comfort_cool` | **Activate the fan only** — the #249 band already holds `comfort_cool`; the economizer pulls cool outdoor air through the open window to assist the band's cooling. It does **not** set the HVAC mode/setpoint (Issue #264 — that would flip the `heat_cool` band to single `cool`). |
| Normal | `False` | Phase 2: maintain | `indoor_temp <= comfort_cool` | Activate the fan; the band stays armed (no `hvac_mode=off` — Issue #249) |
| Savings | `True` | Maintain only (skip Phase 1) | Any eligible condition | Activate the fan; band stays armed; no AC assist (savings relies on ventilation) |

When the economizer deactivates (conditions no longer met), the fan is turned off; the comfort band continues to hold the thermostat — no HVAC mode change is issued (Issues #249/#264).

---

## 9. Fan Control

Fans activate during natural ventilation and during the economizer (both phases — cool-down assists the band's cooling, maintain holds it; Issue #264). Fan behavior is controlled by the `fan_mode` config setting.

| fan_mode value | Activate action | Deactivate action |
|---|---|---|
| `disabled` | No action | No action |
| `whole_house_fan` | `turn_on` the configured `fan_entity` (using the entity's own domain — `fan` or `switch`) | `turn_off` the configured `fan_entity` |
| `hvac_fan` | `climate.set_fan_mode` → `"on"` on the thermostat entity | `climate.set_fan_mode` → `"auto"` on the thermostat entity |
| `both` | Both `whole_house_fan` and `hvac_fan` actions | Both deactivate actions |

### Fan Archetype Behavioral Contract (Issue #277)

`FAN_MODE_HVAC` and `FAN_MODE_WHOLE_HOUSE` have different behavioral roles. These contracts were implicit before Issue #277; they are now explicit.

#### `FAN_MODE_HVAC` — HVAC Blower / Air Circulation

The HVAC fan circulates indoor air through the duct system. It is an integral part of the thermostat and does not exchange air with the outdoors.

| Behavior | Detail |
|---|---|
| On activation | `climate.set_fan_mode(on)` issued; comfort band **stays armed**; HVAC mode unchanged; thermostat self-arbitrates (compressor runs if needed) |
| On deactivation | `climate.set_fan_mode(auto)` issued; comfort band unchanged |
| Stops when windows close? | **No** — unless `_natural_vent_active = True` at the time all sensors close. Fan-only circulation is independent of window state; only the nat-vent path stops the fan on sensor-close. |
| HVAC mode captured? | No — `_pre_fan_hvac_mode` is not set |

#### `FAN_MODE_WHOLE_HOUSE` — Separate Exhaust / Air Exchange Fan

The whole-house fan is a dedicated appliance (e.g., `fan.*` or `switch.*` entity) that pulls outdoor air through the house. Running it with active heating or cooling wastes energy or fights the thermostat.

| Behavior | Detail |
|---|---|
| On activation | Fan entity turned on; **HVAC set to `off`**; current thermostat mode captured in `_pre_fan_hvac_mode` |
| On deactivation | Fan entity turned off; HVAC mode restored from `_pre_fan_hvac_mode` (then `_pre_fan_hvac_mode` cleared) |
| Stops when windows close? | **Yes** — when ALL monitored sensors close, the fan deactivates and HVAC is restored, regardless of `_natural_vent_active` value |
| HVAC mode captured? | Yes — `_pre_fan_hvac_mode: str \| None` holds the thermostat mode at activation time (e.g., `"heat_cool"`, `"cool"`) |

#### `FAN_MODE_BOTH`

Each component (HVAC fan + whole-house fan) follows its own archetype contract above. `_pre_fan_hvac_mode` is still set, because the whole-house fan component requires HVAC suppression.

### Structural WHF/AC Mutual Exclusion — `_whf_owns_hvac()` Choke-Point Guard (Issue #392 Fix 1b)

**Can the whole-house fan and the compressor ever both be commanded on at the same time? No — this is now a structural guarantee, not a per-caller convention.**

Before Issue #392, mutual exclusion was enforced only by convention inside `_activate_fan()`/`_deactivate_fan()` themselves (see the behavioral contract table above). Nothing stopped any of the ~13 other `_set_hvac_mode()` call sites, or the several `_apply_comfort_band()` call sites, from writing an active HVAC mode while a WHF session owned the thermostat. This was a real, confirmed gap: `apply_classification()`'s normal (non-`aggressive_savings`) fall-through called `_apply_comfort_band()` → `_set_temperature(..., mode="cool")` on every 30-minute cycle even while `_natural_vent_active` was `True` under `FAN_MODE_WHOLE_HOUSE` — re-arming the thermostat to `cool` every cycle while the WHF was physically running, fighting the fan CA itself had just turned on.

**The fix: one guard at the single choke point every HVAC write already passes through**, rather than patching each caller individually (per this project's "trust internal invariants, single choke point" philosophy).

```python
def _whf_owns_hvac(self) -> bool:
    fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
    return fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) and self._pre_fan_hvac_mode is not None
```

`_whf_owns_hvac()` is `True` only when both hold: the configured archetype includes a whole-house fan, **and** a suppression session is currently active (`_pre_fan_hvac_mode is not None` — the same flag `_activate_fan()`/`_deactivate_fan()` already use to track an active suppression). It deliberately does not use `_natural_vent_active`, because that flag is also `True` for `FAN_MODE_HVAC` nat-vent sessions, where HVAC is *not* suppressed and writes must be allowed through.

Both HVAC-writing functions check this at their very top, before any service call:

```python
# inside _set_hvac_mode(mode, *, reason) and _set_temperature(temperature, *, reason, mode)
if mode != "off" and self._whf_owns_hvac():
    _LOGGER.warning("HVAC write blocked — whole-house fan owns thermostat (%s)", reason)
    if self._emit_event_callback:
        self._emit_event_callback("hvac_write_blocked_whf_active", {"attempted_mode": mode, "reason": reason})
    return
```

Key properties:
- **`mode == "off"` is never blocked** — the guard only intercepts attempts to arm an *active* mode (`heat`, `cool`, `heat_cool`) while WHF owns the thermostat. Turning HVAC off is always allowed (it's what a WHF session wants anyway).
- **Silent drops are made visible.** A blocked write logs a `WARNING` and emits `hvac_write_blocked_whf_active` (payload: `attempted_mode`, `reason`) so the Activity Log shows the interception rather than the write simply vanishing — per this project's Observability Requirements.
- **`apply_classification()` also short-circuits before reaching the guard.** For `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH`, the nat-vent branch (`if self._natural_vent_active:`) returns immediately after `_apply_nat_vent_hvac_state()` — the same early-return pattern already used for `aggressive_savings=True` — so the classification cycle does not even attempt (and log) a band-arm the choke-point guard would silently drop, and does not waste a cycle computing `select_comfort_band()` or running the ODE ceiling guard while WHF owns the thermostat. `FAN_MODE_HVAC` keeps falling through to the comfort-band write exactly as before, because fan and compressor coexist for that archetype (see §6c).

**This closes Root Cause #2 of Issue #392 directly.** Because both writer functions share this one choke point, no future caller — however it decides to call `_set_hvac_mode()` or `_set_temperature()` — can bypass WHF/AC mutual exclusion. The answer to "can WHF and AC ever both be on" is now enforced at exactly one place, not re-derived correctly (or incorrectly) at every call site.

**Follow-up direction (not yet implemented):** `_whf_owns_hvac()` is deliberately named and doc-commented in the code as the seed of a future `FanSession.may_run_hvac()` method. The Issue #392 shaping analysis found that `_natural_vent_active`, `_fan_active`, `_pre_fan_hvac_mode`, and `_fan_override_active` are one concept ("a fan/HVAC-suppression session with an owner and rules") fractured across four loose attributes with no single owner. A `FanSession` class that owns this state and exposes `activate()`/`deactivate()` (idempotent by construction) and `may_run_hvac(mode) -> bool` is tracked as a **separate, deferred follow-up issue** — it is not implemented as part of Issue #392. `_whf_owns_hvac()` and the idempotency guards in §9f below are the small, safe cuts taken now that are consistent with that future direction, without taking on the risk of a full extraction in a bugfix PR.

**Test coverage:** `tests/test_whole_house_fan_hvac_suppression.py`, `tests/test_fan_control.py` — exact function names pending as of this doc pass; both files carry the current coverage for this guard.

### 9a. Fan State Tracking

The coordinator maintains six internal fields to manage fan state across activate/deactivate calls and detect user overrides:

| Field | Type | Purpose |
|---|---|---|
| `_fan_active` | `bool` | Whether the integration currently considers the fan on |
| `_fan_on_since` | `datetime \| None` | Timestamp of when `_activate_fan()` last turned the fan on |
| `_fan_override_active` | `bool` | Whether a user manual fan override is in effect |
| `_fan_override_time` | `datetime \| None` | Timestamp of when the fan override was detected |
| `_fan_command_pending` | `bool` | Set to `True` immediately before the integration issues a fan command; cleared immediately after |
| `_fan_command_time` | `datetime \| None` | Timestamp recorded at the start of every `_activate_fan()` and `_deactivate_fan()` call; used by `_is_recent_fan_command()` as a timestamp-based secondary guard |
| `_pre_fan_hvac_mode` | `str \| None` | **`FAN_MODE_WHOLE_HOUSE` only.** Captures the thermostat's HVAC mode immediately before fan activation (e.g., `"heat_cool"`, `"cool"`). Restored to the thermostat on deactivation, then cleared to `None`. `None` when no whole-house fan session is active or when using `FAN_MODE_HVAC`. Persisted in state across HA restarts so HVAC restoration survives a restart during a fan session. |

**`_activate_fan()`** sets `_fan_command_time = dt_util.now()` and `_fan_command_pending = True`, issues the fan-on service call, then sets `_fan_active = True` and records `_fan_on_since`. If `_fan_override_active` is `True` at activation time, the call is skipped so the integration does not fight the user's manual setting. For `FAN_MODE_WHOLE_HOUSE` (or `both`), the current thermostat HVAC mode is captured in `_pre_fan_hvac_mode` and HVAC is set to `off` before the fan is turned on.

**`_deactivate_fan()`** follows the same pattern in reverse: sets `_fan_command_time = dt_util.now()` and `_fan_command_pending = True`, issues the fan-off service call, then clears `_fan_active` and `_fan_on_since`. Override state is not checked on deactivation — the intent is always to stop the fan when the economizer or transition logic calls for it. For `FAN_MODE_WHOLE_HOUSE` (or `both`), HVAC mode is restored from `_pre_fan_hvac_mode` and that field is cleared to `None`.

### 9b. Fan Override Detection

Fan override detection runs in two places:

1. **`_async_fan_entity_changed()`** — a state-change listener registered on the `fan_entity` (for `fan_mode == whole_house_fan` or `both`). When the entity state changes, the listener checks whether `_fan_command_pending` is set. If the flag is clear, the state change was user-initiated, not integration-initiated, and a fan override is recorded: `_fan_override_active = True`, `_fan_override_time = utcnow()`.

2. **`_async_thermostat_changed()`** — the existing thermostat state listener is extended to also inspect the thermostat's `fan_mode` attribute (for `fan_mode == hvac_fan` or `both`). If the fan_mode attribute changes while `_fan_command_pending` is clear **and** `_is_recent_fan_command(30.0)` returns `False`, a fan override is recorded using the same fields. The 30-second window is required because cloud-connected thermostats can echo the integration's own `climate.set_fan_mode` call seconds after `_fan_command_pending` has already been cleared (Issue #239).

The same guard applies in **`_async_fan_entity_changed()`** (belt-and-suspenders): `_fan_command_pending` is checked first; `_is_recent_fan_command(30.0)` is checked as the fallback.

#### Compound command-pending guard in `_async_thermostat_changed()` (Issue #205/206)

`_async_thermostat_changed()` contains two override-detection paths: the **normal path** (checks `hvac_mode` / `hvac_action` for HVAC changes) and the **pause-path** (checks for thermostat state changes while `_paused_by_door` is `True`). Both paths share the same suppression guard — before acting on any state change as a user override, the listener checks whether the change was automation-issued by testing:

```python
if self._hvac_command_pending or self._fan_command_pending or self._temp_command_pending:
    return  # change was automation-issued; ignore
```

All three flags must be tested together. Testing only `_hvac_command_pending` is incorrect because **automation sequences frequently call `_deactivate_fan()` before `_set_hvac_mode()`** (for example, natural ventilation exit). In that sequence:

1. `_deactivate_fan()` sets `_fan_command_pending = True` and issues the fan-off service call.
2. The thermostat state listener fires while `_fan_command_pending` is `True` but `_hvac_command_pending` is still `False`.
3. If only `_hvac_command_pending` is checked, the guard is bypassed — the listener misidentifies the automation's own fan-off as a user manual override and starts an unwanted grace period.

The fix (Issue #206) expands the guard at both the pause-path and normal-path detection sites to `_hvac_command_pending OR _fan_command_pending OR _temp_command_pending`. If **any** of the three flags is `True`, the state change is treated as automation-issued and suppressed.

**`_is_recent_hvac_command(threshold_seconds=3.0)`** is a secondary guard that inspects `_hvac_command_time` to catch race conditions where the HVAC flag was already cleared before the listener fired.

**`_is_expected_confirmation` (Issue #269 Bug A):** A third suppression layer for the `fan_mode` attribute-change path specifically. Cloud thermostats (e.g., Nest, Ecobee via cloud polling) sometimes echo a `fan_mode` attribute change as a delayed side-effect of an HVAC mode transition, arriving 30–120 seconds after the original command — outside the 30-second `_is_recent_hvac_command` window. When `_is_expected_confirmation` is `True`, the `fan_mode` change guard suppresses false override detection for up to 120 seconds after the last HVAC command.

**`_is_recent_fan_command(threshold_seconds=30.0)` (Issue #239):** A fourth suppression layer for direct fan service calls. `climate.set_fan_mode` calls do not update `_hvac_command_time`, so `_is_recent_hvac_command()` never fires for fan-mode echoes. This guard reads `_fan_command_time` (set at the start of `_activate_fan()` and `_deactivate_fan()`) and suppresses false overrides within 30 seconds of any fan command.

| Guard | Type | Applies to | Window | Purpose |
|---|---|---|---|---|
| `_hvac_command_pending OR _fan_command_pending OR _temp_command_pending` | Flag check (synchronous) | All command types | Until cleared | Primary: suppresses both paths during any automation-issued command |
| `_is_recent_hvac_command(threshold_seconds=30.0)` | Timestamp check | HVAC mode / setpoint changes | 30 s | Secondary: catches races where the HVAC flag cleared before the HA event arrived |
| `_is_expected_confirmation` | Boolean flag | Fan_mode attribute changes from HVAC mode transitions | 120 s | Tertiary: suppresses delayed fan_mode echoes from HVAC mode changes on cloud thermostats |
| `_is_recent_fan_command(threshold_seconds=30.0)` | Timestamp check | Fan mode changes (`climate.set_fan_mode`) | 30 s | Quaternary: suppresses fan echo races where `_fan_command_pending` cleared before the HA event arrived |

#### `_set_hvac_mode("off")` fan_command_time Guard (Issue #277 Bug A1)

`_set_hvac_mode("off")` includes an internal `set_fan_mode(auto)` assertion that resets the thermostat's fan mode as part of switching HVAC off. This fan-mode call produces a delayed echo on cloud thermostats — the same class of echo suppressed by `_is_recent_fan_command()` elsewhere.

Before Issue #277, this path did not set `_fan_command_time`, so the echo arrived outside the 30-second `_is_recent_fan_command()` window and was misdetected as a user manual fan override, triggering an unwanted grace period.

**Fix:** `_set_hvac_mode("off")` now sets `self._fan_command_time = dt_util.now()` immediately before the `set_fan_mode(auto)` service call. This stamps the command time into the same timestamp the Quaternary guard reads, extending echo suppression to 30 seconds from the HVAC-off command.

**Why here (not in `_activate_fan`/`_deactivate_fan`):** The `set_fan_mode(auto)` inside `_set_hvac_mode("off")` is not a fan activation/deactivation — it is a cleanup step bundled with the HVAC-mode command. It is therefore not routed through `_activate_fan()` or `_deactivate_fan()`, and those helpers' existing `_fan_command_time` stamps do not cover it.

#### Setpoint/Fan Override Mutual Exclusion (Issue #277 Bug B)

A single thermostat event can carry both a setpoint attribute change and a `fan_mode` attribute change simultaneously. Before Issue #277, both Block 2 (setpoint-override detection) and Block 3 (fan-mode override detection) in `_async_thermostat_changed()` evaluated independently — a single physical user action could trigger two simultaneous overrides (setpoint + fan), each starting its own grace timer.

**Fix:** A local boolean `_setpoint_override_detected` is initialized to `False` at the start of the function, before Block 2. If Block 2 fires (a setpoint override is detected and recorded), it sets `_setpoint_override_detected = True`. Block 3's fan-override condition is guarded by `and not _setpoint_override_detected`:

```python
_setpoint_override_detected = False  # initialized before Block 2

# Block 2 — setpoint detection
if <setpoint changed by user>:
    handle_manual_override(...)
    _setpoint_override_detected = True

# Block 3 — fan_mode detection
if <fan_mode changed> and not _setpoint_override_detected:
    handle_fan_manual_override(...)
```

**Invariant:** one thermostat event → at most one override type recorded. If a setpoint change and a fan_mode change arrive in the same event, only the setpoint override fires; the fan_mode change is treated as a correlated side-effect, not a separate user action.

#### Fan Override Detection Diagnostic Logging (Issue #277 Bug H)

When `handle_fan_manual_override()` fires from `_async_thermostat_changed()`, the INFO-level log line now includes the following fields to make false-positive investigations self-contained without requiring a debug log level:

| Field | Meaning |
|---|---|
| `old_fan_mode` | The thermostat's `fan_mode` attribute before the change |
| `new_fan_mode` | The thermostat's `fan_mode` attribute after the change |
| `fan_cmd` age (seconds) | `(now − _fan_command_time).total_seconds()` — time since the last fan command; `None` if `_fan_command_time` is unset |
| `hvac_cmd` age (seconds) | `(now − _hvac_command_time).total_seconds()` — time since the last HVAC command; `None` if unset |
| `expected_confirmation` | Current value of `_is_expected_confirmation` at the moment the override is recorded |

These values make it possible to determine, from the log alone, whether the override was a real user action or a delayed echo that arrived just outside a suppression window.

#### Mode Override Detection — `_last_commanded_hvac_mode` (Issue #269 Bug C)

The normal-path override detection in `_async_thermostat_changed()` compares the thermostat's reported `hvac_mode` against the expected mode. Prior to Issue #269, that comparison was always against `classification.hvac_mode`. For dual-setpoint thermostats, CA commands `heat_cool` mode (§6e), but `classification.hvac_mode` may be `"cool"` or `"heat"`. A user switching from `heat_cool` back to `cool` would evaluate as `"cool" != "cool"` = `False` and go undetected.

The fix replaces the comparison target with `ae._last_commanded_hvac_mode or classification.hvac_mode`:
- When CA has issued a mode command, `_last_commanded_hvac_mode` holds the actual mode sent to the thermostat (e.g., `"heat_cool"`).
- If no command has been issued in this session, it falls back to `classification.hvac_mode`.

This ensures mode overrides are correctly detected regardless of whether the thermostat is single- or dual-setpoint capable.

#### Dual Setpoint Override Detection — `heat_cool` Mode (Issue #269 Bug D)

Setpoint override detection reads the thermostat's temperature attributes to determine whether the user has manually changed a setpoint. When the thermostat is in `heat_cool` mode, `temperature` (the single-setpoint attribute) is `None` — only `target_temp_low` and `target_temp_high` are populated.

The fix gates attribute selection on the current thermostat mode:

| Thermostat mode | Attribute read for setpoint check |
|---|---|
| `heat_cool` | `target_temp_low` and `target_temp_high` |
| `heat`, `cool`, `off`, other | `temperature` (single-setpoint attribute) |

The grace-period trigger in the same block also now compares against `ae._last_commanded_hvac_mode` rather than `classification.hvac_mode`, consistent with the Bug C fix above.

#### `hvac_mode` in Coordinator Data (Issue #269 Bug B)

`hvac_mode` — the thermostat's current operating mode string (`"heat_cool"`, `"cool"`, `"heat"`, `"off"`) — is now included in the coordinator's data dict returned by `_async_update_data()`. `_detect_and_emit_incidents()` reads it from `coordinator.data` to populate incident records with the actual thermostat mode at detection time, rather than deriving it indirectly from other attributes.

**Test coverage:** `tests/test_override_automation_boundary.py` — compound guard invariant.

Fan override is **separate** from HVAC override. The two override states are tracked independently and do not interfere with each other. Fan override uses the same grace period duration as manual HVAC override (`DEFAULT_MANUAL_GRACE_SECONDS`), but the timers run independently.

Fan override is **cleared** at transition points where the integration takes deliberate control of the fan (bedtime, morning wakeup — see Section 9c).

### 9c. Fan Behavior at Transitions

| Transition | Fan action | Override cleared? |
|---|---|---|
| Bedtime | `_deactivate_fan()` called; economizer also deactivated | Yes — `_fan_override_active` reset to `False` |
| Morning wakeup | `_deactivate_fan()` called | Yes — `_fan_override_active` reset to `False` |

At bedtime, both the fan and the economizer are explicitly shut down before the bedtime setpoints are applied. This ensures the overnight period starts with a clean fan state regardless of what the economizer was doing during the evening window. At morning wakeup, the fan is deactivated before comfort temperatures are restored, preventing carryover of an economizer fan session into the occupied-home daytime period.

Clearing the override flag at these transitions means the integration will not skip fan activation during the next economizer cycle just because the user had manually adjusted the fan during the previous day.

### 9c-i. Fan-ON and Fan-OFF Decision Table (Issue #359)

This table enumerates the six key fan lifecycle scenarios, including the new `on_fan_turned_off()` handler and `fan_cancel` event type introduced in Issue #359.

| Scenario | Trigger | CA decision | Flags / state change | Event emitted | Test ref |
|---|---|---|---|---|---|
| Fan-ON + nat-vent eligible | User turns fan on; `outdoor < indoor`, sensors open, gate passes | Adopt as nat-vent — do NOT set override | `_fan_active = True`, `_natural_vent_active = True`, `_fan_override_active` stays `False` | `fan_activated` (nat-vent adoption) | `test_fan_control.py` |
| Fan-ON + nat-vent ineligible | User turns fan on; conditions gate does not pass | Manual override — start grace timer | `_fan_override_active = True`, `_fan_override_time = now()` | `fan_manual_override` | `test_fan_control.py` |
| Fan-OFF (user) | User physically turns the fan off (fan_mode → auto) | `on_fan_turned_off()`: clear fan flags, start fan-off grace — **no** `_fan_override_active` set | `_fan_active = False`, `_natural_vent_active = False`, `_fan_override_active = False`; fan-off grace timer starts | `fan_cancel` | `test_fan_cancel.py` |
| Fan-OFF + ecobee setpoint echo | Ecobee or cloud thermostat echoes setpoint change within 5 s of fan-off | Setpoint suppressed; re-assertion fires after 5 s delay | `_setpoint_reassert_pending = True`; scheduled callback re-applies commanded setpoint | _(none — suppression is silent)_ | `test_fan_cancel.py` |
| Post-grace reconciliation | Fan-off grace period expires | `reconcile_fan_on_startup()` called; re-evaluate physical state | Adopt fan as nat-vent (eligible) or confirm fan is off (ineligible) | `fan_activated` or _(no event if off)_ | `test_fan_cancel.py` |
| Periodic backstop (`_async_update_data()`) | 30-min coordinator poll fires while fan is `"running (untracked)"` and no override or grace active | Same reconciliation path — adopt-on or turn-off | `_fan_active` and `_natural_vent_active` updated accordingly | `fan_activated` or `fan_deactivated` | `test_fan_cancel.py` |

**Key semantic distinction for fan-off grace vs fan-manual-override grace:**
The `fan_off` grace (started by `on_fan_turned_off()`) gates nat-vent **re-activation** — CA backs off from immediately restarting the fan the user just stopped. The `fan_manual_override` grace (started when the user turns a fan on) gates CA **interference** with a fan the user is actively running. The two grace types have inverted blocking semantics. See `docs/grace-periods-spec.md` for the full grace period state machine.

### 9c-ii. WHF Feedback Mode (Issue #361)

`fan_state_feedback` (bool, default `False`) applies **only to the whole house fan** (`fan_entity`).
It has no effect when `fan_mode=hvac_fan` — the HVAC fan is controlled via the thermostat's own
`fan_mode` attribute; there is no separate entity to observe. The Activity Record warning banner and
AI context note only appear when `fan_mode` is `whole_house_fan` or `both` AND `fan_entity` is set.

`fan_state_feedback` controls whether CA reads physical WHF motor state or operates in command-only mode.

| fan_state_feedback | _fan_active (CA wants ON) | grace active | Action |
|---|---|---|---|
| True | True | No | Read physical state via `_get_fan_physical_state()`; command ON if off |
| True | False | No | Read physical state; command OFF if unexpectedly on |
| False | True | No | Command ON idempotently (skip state read); update `_last_commanded_fan_state` |
| False | False | No | Command OFF idempotently (skip state read); update `_last_commanded_fan_state` |
| False | True | Yes | No command — grace gates re-activation even without feedback |
| False | False | Yes | No command — grace prevents turn-on |

**Idempotency**: Commands are only re-issued when `_fan_active` (desired) diverges from
`_last_commanded_fan_state` (last issued command). This prevents command churn on every 30-min cycle.

**Override detection**: `_async_fan_entity_changed()` is suppressed when `fan_state_feedback=False`
(it only fires on CA's own command echo, not on physical user overrides). Wall-switch overrides are
undetectable without a state sensor.

**`_compute_fan_status()`** reads thermostat climate entity attributes (HVAC fan) — NOT the WHF entity.
WHF operational status is tracked separately via coordinator data fields:
- `whf_mode`: `"command-only"` | `"state-feedback"` | `"disabled"`
- `whf_last_commanded`: `"on"` | `"off"` | `None`
- `whf_desired`: `True` | `False` | `None`

**Auto-flip**: When `fan_state_entity` is configured in the options flow, `fan_state_feedback`
is auto-suggested as `True` (user can override).

### 9d. Fan Status Sensor Values

The `sensor.climate_advisor_fan_status` entity exposes one of six state strings:

| Sensor state | Meaning |
|---|---|
| `disabled` | Fan control is not configured (`fan_mode = disabled`) |
| `inactive` | Fan is off; integration is in control |
| `active` | Fan is on; integration activated it (nat-vent or economizer) |
| `running (manual override)` | Fan is physically on under manual override. Two sub-cases: (a) `_fan_active=True` (CA-owned flag set) — CA has a record of activating it; (b) `_fan_active=False` but physical state is on — user-owned run, CA recorded the override but did not adopt it as nat-vent. Both sub-cases report the same sensor value. *(Issue #365)* |
| `off (manual override)` | `_fan_override_active=True` and `_fan_active=False` and physical state is off (or fan_mode is not WHF/BOTH). Override still in effect but the fan has been turned off before grace expired. *(Issue #365)* |
| `running (untracked)` | Fan is physically running but `_fan_active=False`. Detection path depends on fan mode: **HVAC/Both** — thermostat reports `fan_mode=on` or `hvac_action=fan`; **WHF/Both** — `_get_fan_physical_state()` reads `fan_state_entity` (Type 2) or `fan_entity` (Type 1). Typical after HA restart or user-initiated run from thermostat/wall switch. Returns `"inactive"` instead when `fan_state_feedback=False` (command-only mode, no physical feedback sensor). *(WHF fallback added Issue #363.)* |

The sensor also exposes these attributes:
- `fan_runtime_minutes` — minutes since the integration last activated the fan (0.0 when inactive or in override)
- `fan_override_since` — ISO timestamp of when the manual override was detected (`null` when no override is active)
- `fan_running` — boolean; `true` when the fan is physically running regardless of who controls it

**HVAC-off + fan-on (fan-only circulation):** When the economizer enters the maintain phase, HVAC mode is set to `off` but `climate.set_fan_mode: on` is called separately. This is the intended "fan-only circulation" mode — most thermostats support running the fan for air circulation independently of heating or cooling. A `DEBUG`-level log entry is emitted whenever the integration activates the HVAC fan while the thermostat reports `hvac_mode = off`.

**`running (untracked)` after Issues #327 and #347:** `"running (untracked)"` is expected only as a brief transient in two cases: (1) between HA startup and the completion of `_do_startup_coalesce`; (2) between when the thermostat reports `hvac_action="fan"` mid-session and when `_async_thermostat_changed` calls `reconcile_fan_on_startup` to resolve it. In both cases any fan still running is either adopted as CA nat-vent or turned off — there is no persistent untracked limbo. A `"running (untracked)"` state that persists beyond these moments signals a coordinator setup failure (case 1) or a code-path regression (case 2). See `Fan reconcile:` log lines.

### 9e. Thermostatic Fan Loop and Startup Reconciliation (Issue #327)

#### The Principle: a Running Fan Always Has an Owner

Prior to Issue #327, four code paths could leave a fan running indefinitely with no CA owner and no shutdown mechanism:

1. `_compute_fan_status()` returned `"running (untracked)"` but no code path acted on it — the string was used only to suppress unrelated warnings.
2. Every shutdown path was gated on ownership (`_deactivate_fan()` requires `_fan_active=True`; nat-vent exit requires `_natural_vent_active=True`), so an unowned fan could never be turned off.
3. `restore_state()` on restart preserved `_fan_override_active=True` without rescheduling the grace-period expiry timer, leaving the override permanent and both `_activate_fan()` and `_deactivate_fan()` permanently skipped.
4. The only fast-loop temperature check ran on nat-vent only; the outdoor sensor had no state listener, so an outdoor temperature rise was invisible until the next 30-minute coordinator poll.

The occupant experienced this as: a fan running through the night while outdoor air was warmer than indoor — actively heating the house — with no automatic correction.

**Issue #327 enforces the invariant:** while the fan feature is enabled, a running fan is always one of:
- **CA nat-vent** — activated by `_activate_fan()`, held by the fast thermostatic loop (§9e below), exits the loop on `outdoor ≥ indoor`, comfort floor, or target reached.
- **Timed manual override** — detected by `_async_fan_entity_changed()` or `_async_thermostat_changed()`, reclaimed when the grace timer expires **or** on the next HA restart.
- **Off** — the default state when neither condition holds.

There is no fourth state. Any post-coalesce `fan_mode="on"` or fan-entity change that CA did not command is detected as a manual override (§9b) → timed, not indefinite. A post-coalesce `hvac_action="fan"` (thermostat-autonomous fan-on between AC cycles) is reconciled by `reconcile_fan_on_startup` via the post-startup detection path (Issue #347) → adopt-on or turn-off, never indefinite limbo.

#### A. Restart = Clean Fan Slate

`restore_state()` now clears `_fan_override_active` and `_fan_override_time` on restart, matching the clean-slate treatment of HVAC override/grace state (§11). Fan ownership is fully reconsidered by the coalesce reconciliation step rather than reconstructed from stale persisted flags.

`_fan_active` and `_pre_fan_hvac_mode` are still preserved as hints for reconciliation, but their values do not gate any action — the reconcile step re-derives the correct decision from the live thermostat state.

#### B. Startup Coalesce: `reconcile_fan_on_startup`

After the existing nat-vent / `apply_classification` logic in `_do_startup_coalesce`, a dedicated fan reconciliation step reads the thermostat's live `fan_mode` / `hvac_action` and decides:

| Physical fan running? | Nat-vent eligible? | Decision | Action |
|---|---|---|---|
| No | — | **no-fan** | No action; state flags already cleared by (A) |
| Yes | Yes (`outdoor < indoor`, gate passes, sensors open) | **adopt-on** | `_fan_active = True`, `_natural_vent_active = True`; fast thermostatic loop started |
| Yes | No | **turn-off** | `_deactivate_fan()` or `set_fan_mode("auto")` (FAN_MODE_HVAC) / fan `turn_off` + HVAC restore (FAN_MODE_WHOLE_HOUSE) |

The 5-minute `_first_run` coalesce window already suppresses override detection (coordinator's `_async_thermostat_changed` override guard), so the turn-off command is not misread as a user manual action.

**Observability (startup validation):** the reconcile step emits one INFO line at the end of `_do_startup_coalesce`:

```
Fan reconcile: thermostat fan_mode=<x> hvac_action=<y> nat_vent_eligible=<bool> decision=<adopt-on|turn-off|no-fan> archetype=<mode>
```

This is the primary grep target for post-deploy validation: `python tools/ha_logs.py --filter "Fan reconcile"`. It confirms that the new behavior ran and what decision was made for the current physical state.

**Listener registration observability:** at coordinator setup, one INFO line is emitted:

```
Fan control: watching indoor=<entity> outdoor=<entity> thermostat=<entity> for thermostatic re-eval
```

#### C. Thermostatic Fast Loop: `fan_thermostat_check`

`fan_thermostat_check(indoor, outdoor, trigger)` on `AutomationEngine` is the fast decision point for any CA-owned running fan. It generalizes the existing `nat_vent_temperature_check` — which ran only for nat-vent sessions — to cover any fan that `_fan_active=True`.

**Exit conditions evaluated on every call (priority order):**

| Priority | Condition | Action |
|---|---|---|
| 1 | `outdoor >= indoor` (using existing 1°F hysteresis for re-activation, equality kills) | Fan off; emit `nat_vent_outdoor_rise_exit` if nat-vent session; otherwise deactivate cleanly |
| 2 | `indoor <= comfort_heat` (comfort floor) | Exit nat-vent session; restore heat mode at `comfort_heat` |
| 3 | `outdoor > comfort_cool + nat_vent_delta` (ceiling exceeded) | Fan off; enter paused state |

If no exit condition fires, the fan continues running. The check is cheap and idempotent — frequent calls are safe.

**Trigger sources (all three active whenever the fan is CA-owned and running):**

| Source | Mechanism | Registered in |
|---|---|---|
| Indoor temperature change via thermostat | Existing `_async_thermostat_changed` dispatch → `fan_thermostat_check(trigger="indoor")` | coordinator.py (existing seam, extended) |
| Indoor temperature change via dedicated sensor | New state listener on `indoor_temp_entity` → `fan_thermostat_check(trigger="indoor")` | coordinator.py (new listener added by Issue #327) |
| Outdoor temperature change | New state listener on `outdoor_temp_entity` → `fan_thermostat_check(trigger="outdoor")` | coordinator.py (new listener — outdoor had no listener before Issue #327) |
| Backstop timer | Self-rescheduling timer started in `_activate_fan()`, cancelled in `_deactivate_fan()` and `cleanup()`; reuses the `_fan_min_cycle_cancel` pattern | automation.py |

The backstop timer catches sensors that update slowly or infrequently. The trigger name is passed through to observability logging.

**Observability (per-check):** `DEBUG` on every call:

```
Fan thermostat check: trigger=<indoor|outdoor|tick|timer> indoor=<t> outdoor=<t> active=<bool> decision=<keep|stop:reason>
```

#### D. Economizer Free-Cooling-Direction Guard

`check_window_cooling_opportunity()` (§8) now includes `outdoor < indoor` as an explicit eligibility condition, mirroring the guard already present in nat-vent activation (§17). This prevents the economizer from starting the fan on a hot evening when outdoor air is warmer than indoor — a condition that actively heats the house instead of cooling it.

The guard is a strict precondition: if `outdoor >= indoor`, the fan is not activated regardless of whether the time-window and temperature-ceiling conditions are met.

#### E. Manual Override = Timed, Not Indefinite

With (A) restart clearing `_fan_override_active` and (B) coalesce reconciling the physical state, every post-restart fan-on that CA did not command is fresh — detected as a new manual override by `_async_fan_entity_changed()` or the `fan_mode` block of `_async_thermostat_changed()`, and reclaimed when the grace timer expires. There is no path from a user action to a permanent, unreclaimed override.

**Override lifecycle observability (INFO):**
- Override set: logged by `handle_fan_manual_override()` with `old_fan_mode`, `new_fan_mode`, `fan_cmd` age, `hvac_cmd` age, `expected_confirmation` (§9b Fan Override Detection Diagnostic Logging).
- Grace expiry reclaim: logged by `_on_grace_expired` / `clear_fan_override`.
- Restart clean-slate: logged by `restore_state()` — `"Fan override cleared on restart (clean slate)"` when `_fan_override_active` was `True` at restore time.

#### Interaction with §11 Clean-Slate Restart Policy

The fan clean-slate introduced in (A) is consistent with §11: `_fan_override_active` joins `_manual_override_active`, `_grace_active`, and `_override_confirm_pending` as fields that are always cleared on restart. The coalesce step (B) performs the same role for fan state that the `_first_run` startup override check (§11 Startup Override Logic) performs for HVAC state — it re-derives the correct ownership decision from live conditions rather than trusting stale persisted flags.

| Field | Preserved across restart? | Notes |
|---|---|---|
| `_fan_active` | **Hint only** | Cleared or overwritten by coalesce reconciliation; does not gate any action on its own |
| `_fan_override_active` | **No** (Issue #327) | Cleared on restart — clean slate; coalesce re-derives from live state |
| `_fan_override_time` | **No** (Issue #327) | Cleared on restart |
| `_pre_fan_hvac_mode` | Yes | Still preserved — needed if coalesce decides to turn off a whole-house fan and restore the HVAC mode |
| `_natural_vent_active` | No | Cleared on restart (was already the case); coalesce adopt-on re-sets it |

### 9f. Idempotency Guards and the `_fan_running` Property (Issue #392 Fix 1c / Fix 1e)

**Occupant-facing symptom this fixes:** during the 18:53–19:04 burst reported in Issue #392, the Activity Log showed what looked like several different automation decisions "fighting" every few minutes — the user could not tell whether the system was actually deliberating or just re-logging the same decision repeatedly.

**Root cause:** `_activate_fan()` and `_deactivate_fan()` had no check for "is the fan already in the state I'm about to put it in." Four independent (re)activation gate sites (§17) can each independently conclude "conditions are met" within the same few seconds — a grace-expiry timer callback, a sensor-open debounce callback, the 30-minute classification cycle, and the ODE ceiling guard all evaluate their own trigger conditions with no coordination. Before this fix, every one of them that reached the same conclusion re-ran the *entire* activation/deactivation sequence: re-capturing `_pre_fan_hvac_mode` from whatever the thermostat showed at that instant (possibly already stale from a sibling handler's change moments earlier), reissuing the physical service call, and emitting a fresh `fan_activated`/`fan_deactivated` event — even when the fan was already in the target state from a decision made two seconds prior.

**Fix — idempotency guard at the top of both functions**, after the existing `FAN_MODE_DISABLED` and `_fan_override_active` checks, before any state mutation:

```python
# in _activate_fan()
if self._fan_active:
    _LOGGER.debug("_activate_fan: already active — no-op (%s)", reason)
    return
```
```python
# in _deactivate_fan()
if not self._fan_active:
    _LOGGER.debug("_deactivate_fan: already inactive — no-op (%s)", reason)
    return
```

Effect: the first caller to legitimately flip the fan state performs the work and logs the event (INFO/WARNING + Activity Log entry). Every other handler that reaches the same conclusion moments later finds nothing left to do and produces only a `DEBUG`-level line — traceable in the logs, but not a duplicate Activity Log row. Combined with the archetype-aware ceiling fix (§6c) and the choke-point guard (above), which make the *decision itself* stable, this makes the *execution* stable too: one real state transition per actual change, not one log line per handler that happened to fire in the same window.

**`_fan_running` property (Fix 1e — shaping cut):** a related but separate smell was the recurring pattern `self._fan_active or self._natural_vent_active` appearing inline at multiple call sites (e.g. `nat_vent_temperature_check()`) to answer "is CA's fan on right now" — needing to OR two fields together to answer one question is evidence the two flags are one concept fractured into two names. Collapsed into a derived property:

```python
@property
def _fan_running(self) -> bool:
    return self._fan_active or self._natural_vent_active
```

Purely a readability/correctness-by-construction cut (no behavior change) — every inline `_fan_active or _natural_vent_active` OR was replaced with this property. Like `_whf_owns_hvac()`, it is a small stepping stone toward the deferred `FanSession` extraction (see the follow-up note in the Structural WHF/AC Mutual Exclusion subsection above), not the extraction itself.

**Test coverage:** `tests/test_fan_control.py` — exact function names pending as of this doc pass.

### 9g. `_decision_lock` — Serializing the Six Automation Entry Points (Issue #392 Fix 3)

The `__init__` code comment for `self._decision_lock` in `automation.py` points readers here (§9g) for the deadlock-avoidance analysis below.

**Occupant-facing symptom this fixes:** the same 18:53–19:04 burst also showed decisions from genuinely different trigger sources interleaving within the same few seconds. Fixes in §6c, above (choke-point guard), and §9f (idempotency) make each *individual* decision correct and stop *redundant* re-execution — but neither one, by itself, prevents two independently-triggered handlers from reading and writing shared engine state (`_natural_vent_active`, `_fan_active`, `_pre_fan_hvac_mode`, `_paused_by_door`) while the other is mid-flight. Python's `asyncio` is single-threaded but not atomic across `await` points, so handler B can start acting on state handler A is in the middle of changing.

**The six automation decision-pass entry points**, each independently triggerable by a different event source (HA state-change listener, `async_call_later` timer callback, or the coordinator's periodic `_async_update_data()`):

| # | Method | Trigger source |
|---|---|---|
| 1 | `apply_classification()` | Coordinator's 30-minute classification cycle |
| 2 | `handle_door_window_open()` | Coordinator callback after sensor-open debounce |
| 3 | `handle_all_doors_windows_closed()` | Coordinator callback after all sensors close |
| 4 | `check_natural_vent_conditions()` | Called by the coordinator on every `_async_update_data()` |
| 5 | `_re_pause_for_open_sensor()` | Triggered via `hass.async_create_task(...)` from the grace-expiry callback |
| 6 | `nat_vent_temperature_check()` | Periodic re-evaluation while nat-vent is active or paused |

**Fix:** `self._decision_lock = asyncio.Lock()` is created once in `__init__`. Each of the six methods above wraps its entire body in `async with self._decision_lock:` — a second trigger firing while a decision pass is already in progress waits for the lock instead of interleaving and racing on shared state:

```python
async def apply_classification(self, classification, predicted_indoor=None, indoor_temp=None) -> None:
    async with self._decision_lock:
        ...  # entire method body
```

**Deadlock-avoidance pre-check (required before wrapping):** `asyncio.Lock` is not reentrant — if any of the six methods called another of the six directly within the same call stack, wrapping both with the same lock would deadlock. Before implementing, the code was searched for direct cross-calls among the six; **none were found** — no method in this list calls another method in this list synchronously in its own body. Because of that, a plain `async with self._decision_lock:` wrap around each method's existing body was sufficient; no `_impl` extraction (splitting each method into a locked wrapper plus an unlocked `_impl` twin for internal cross-calls) was needed for this PR. If a future change introduces a direct call between any two of these six methods, that pre-check must be repeated and the `_impl` pattern applied before merging.

**What this does NOT change:** this lock does not introduce new automation behavior. The semantic fixes (§6c archetype-aware ceiling, above choke-point guard, §9f idempotency) already make each individual decision correct and idempotent; the lock ensures those correct decisions are evaluated one at a time against a consistent snapshot of engine state, instead of several handlers reading/writing overlapping state concurrently. In a well-behaved system where the semantic fixes hold, the lock should rarely be contended — its purpose is to make the *absence* of interleaving-driven chaos structurally guaranteed rather than incidentally true.

**Test coverage:** `tests/test_nat_vent_activation.py::TestDecisionLockConcurrency::test_two_entry_points_do_not_interleave` — `asyncio.gather()` invokes two of the six entry points "concurrently" against a shared engine instance instrumented to record enter/exit order, asserting non-overlapping execution.

#### Holder tracking — diagnosing a stuck lock (Issue #396)

This lock shipped with WARNING-level logging for the *contended-and-blocked* case
(`hvac_write_blocked_whf_active`, §9 above) but nothing for "a method is waiting on this lock and
it isn't coming back" — the exact failure mode that caused startup coalescing to hang indefinitely
in production shortly after this lock was deployed (root cause not yet confirmed as of this doc
pass; see Issue #396). That gap is closed by `_decision_pass()`, an async context manager every one
of the six entry points now goes through instead of a bare `async with self._decision_lock:`:

```python
async def apply_classification(self, ...) -> None:
    async with self._decision_pass("apply_classification"):
        ...  # entire method body, unchanged
```

`_decision_pass()`:
- Logs (DEBUG) when a method starts waiting on an already-held lock, naming the current holder.
- Sets `self._decision_lock_holder` (method name) and `self._decision_lock_held_since` (timestamp)
  immediately after acquiring — cleared in a `finally` immediately before release, so this is
  accurate even on exception paths.
- Logs (DEBUG) the wait duration on acquire and the hold duration on release.

`_decision_lock_holder` / `_decision_lock_held_since` are also surfaced on the coordinator status
API (`coordinator.py`, alongside `startup_coalesce_active`) as `decision_lock_holder` /
`decision_lock_held_seconds`, so a stuck lock is visible from the dashboard — "waiting on
`check_natural_vent_conditions`, held 340s" — instead of a generic "waiting for coalescing" with no
further detail. This is purely additive observability; it does not change which method acquires the
lock or when.

**Root cause confirmed NOT the lock (Issue #396 resolution):** deploying this instrumentation and
querying `decision_lock_holder` live on a stuck instance showed it was `null` — nothing was holding
the lock. The actual cause: the coalesce check in `_async_update_data()` lives entirely inside `if
forecast:`, so it never runs at all while the weather entity is `unavailable` after a restart
(`_get_forecast()` returns falsy, `_current_classification` stays `None`). `_compute_automation_status()`
now distinguishes this: if `_startup_timer_fired` is `True` (the 5-minute suppression window has
elapsed) but `_current_classification` is still `None`, the status returns `"starting — waiting for
weather data"` instead of the generic `"starting — initializing"` — so this specific failure mode is
diagnosable from the status card alone next time, without needing the lock instrumentation at all.
See `_compute_automation_status()` in `coordinator.py`.

**Test coverage:** `tests/test_nat_vent_activation.py::TestDecisionLockHolderTracking` — holder set
during a pass and cleared after, cleared even when the pass body raises, and a second (waiting) pass
can see the first pass's holder name while blocked.

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
| Command-pending flags (`_hvac_command_pending`, `_fan_command_pending`, `_temp_command_pending`) | Each flag is set `True` immediately before the integration issues the corresponding service call and cleared after it completes. `_async_thermostat_changed()` checks **all three** flags: if any is `True`, the state change is treated as automation-issued and both the pause-path and normal-path override detection are suppressed. This compound check is required because automation sequences (e.g., nat vent exit) call `_deactivate_fan()` before `_set_hvac_mode()` — the fan command sets `_fan_command_pending` but `_hvac_command_pending` is still `False`. Checking only `_hvac_command_pending` bypasses the guard. `_hvac_command_time` records the timestamp of the last HVAC command for the secondary `_is_recent_hvac_command()` timestamp guard. See §9b for the full guard specification. |

---

## 11. Grace Periods

| Type | Trigger | Default Duration | Configurable? | Effect | Notify on Expiry (default) |
|---|---|---|---|---|---|
| Manual | User overrides thermostat — mode change **or setpoint-only change** (v0.3.55+, Issue #197) — or clicks "Resume HVAC (override pause)" | `1800s` (30 min) | Yes — `CONF_MANUAL_GRACE_PERIOD` | Blocks door/window sensor from re-pausing HVAC; classification skips HVAC mode changes | Yes (`CONF_MANUAL_GRACE_NOTIFY = True`, Issue #282). Message: "Your manual thermostat override has expired. Climate Advisor has resumed automated control." |
| Automation | Climate Advisor resumes HVAC after all sensors close | `300s` (5 min) | Yes — `CONF_AUTOMATION_GRACE_PERIOD` | Blocks door/window sensor from immediately re-pausing HVAC | Yes (`CONF_AUTOMATION_GRACE_NOTIFY = True`) |

Only one grace timer of each type is active at a time; starting a new one cancels the previous.

**Grace expiry sensor re-check:** When either grace period expires, the system re-checks whether any monitored contact sensor is currently open. If one or more sensors are still open, HVAC is re-paused immediately (`_paused_by_door = True`, HVAC set to `off`) rather than restoring normal automation. This prevents the safety issue of running HVAC with a door or window open after the grace window closes.

### Clean-Slate Override State on HA Restart (Issue #282 / #306)

CA always starts in full clean-slate automation mode after an HA restart. `restore_state()` does **not** restore override, grace, or pause state. All three categories are intentionally kept out of `get_serializable_state()` (override/grace since Issue #282; pause state since Issue #306).

**What is preserved across restarts:**

| Field | Preserved? | Notes |
|---|---|---|
| `_paused_by_door` | **No** | Cleared on restart (Issue #306). Open sensors are re-detected quickly via the state-change listener (entity transitions from `None` → `"on"` when HA reconnects); HVAC re-arms briefly and re-pauses after the configured debounce (default 5 min). |
| `_pre_pause_mode` | **No** | Cleared on restart (Issue #306). Re-captured when the re-detected open sensor triggers a fresh pause. |
| `_fan_active` | **Hint only** (Issue #327) | Preserved as a hint; overwritten or disregarded by the coalesce reconcile step (§9e) — does not gate any action on its own after restart |
| `_fan_override_active` | **No** (Issue #327) | Cleared on restart — clean slate. Coalesce re-derives fan ownership from live state. Previously preserved, which caused permanent override lockout when no grace-expiry timer was rescheduled. |
| `_fan_override_time` | **No** (Issue #327) | Cleared on restart |
| `_pre_fan_hvac_mode` | Yes | HVAC mode captured before whole-house fan activation; still needed for HVAC restoration if coalesce decides to turn off a whole-house fan |
| `_last_action_time` / `_last_action_reason` | Yes | Last automation action metadata |
| `_occupancy_mode` | Yes | Current occupancy state |
| `_manual_override_active` | **No** | Cleared on restart — clean slate |
| `_grace_active` / `_grace_end_time` | **No** | Cleared on restart — clean slate |
| `_override_confirm_pending` | **No** | Cleared on restart — clean slate |
| `_manual_override_mode` / `_manual_override_time` | **No** | Cleared on restart |

**Why pause state is not restored (Issue #306):** Persisting `_paused_by_door` risks leaving CA paused indefinitely if cloud services (weather, thermostat) reconnect slowly — the sensor may not fire a state-change callback unless it transitions away from `None`. Re-detecting via the normal `None → "on"` listener path is more reliable than trusting stale persisted state. The sensor entity registers quickly after HA startup, but the re-pause takes the configured debounce (default 5 min) before `handle_door_window_open()` fires. During that window HVAC briefly re-arms — a small trade-off that is strictly better than sitting paused indefinitely on a hot day. This matches the existing clean-slate policy for manual overrides and grace periods.

**Settling window:** `restore_state()` sets `_first_run = True`. The coordinator's `_async_update_data()` delays the first full automation evaluation by 5 minutes (`_first_run` guard) to let the thermostat and HA state settle before CA takes any HVAC action. This replaces the role previously played by persisted override state in preventing false automations after restart.

### Startup Override Logic

On first data update after startup, Climate Advisor checks whether the HVAC's current mode matches the day classification's recommended mode before setting a manual override:

| HVAC state | Classification recommends | Result |
|---|---|---|
| `off` / `unavailable` / `unknown` | any | No override set |
| `heat` | `heat` | No override — modes match |
| `heat` | `cool` or `off` | Manual override set — respects current state |
| `cool` | `cool` | No override — modes match |
| `cool` | `heat` or `off` | Manual override set — respects current state |

This prevents unnecessary override lockouts after a Home Assistant restart when the HVAC is already in the mode that Climate Advisor would have set anyway. See Issue #42. This check runs after the 5-minute `_first_run` settling window.

### PATH B Notification — Transient Thermostat Adjustment (Issue #200)

When the thermostat self-reverts to the expected mode within the `CONF_OVERRIDE_CONFIRM_PERIOD` confirmation window (PATH B — `override_self_resolved`), a user notification is sent:

> "Brief thermostat adjustment detected — treated as transient. Climate Advisor continues normal operation."

This informs the occupant that a brief thermostat blip was observed but was not treated as an intentional override. No grace period starts; normal automation resumes immediately. Notification is sent only when a notify service is configured.

For the full confirmation-window state machine (PATH A vs PATH B), see [Grace Periods Spec — Override Confirmation Delay](grace-periods-spec.md#override-confirmation-delay).

### Second Override During Active Grace (Issue #201)

If the user changes the thermostat to a **different mode** while `_manual_override_active = True` (i.e., a grace period is already running), the engine treats this as a new, distinct override:

1. The current override and grace timer are cleared via `clear_manual_override()`.
2. A new 10-minute `CONF_OVERRIDE_CONFIRM_PERIOD` confirmation window starts for the new mode.
3. If the new mode is still divergent after the confirmation window (PATH A), a fresh 30-minute grace period begins.

This prevents the scenario where an occupant makes two sequential manual adjustments and the second one is silently ignored because grace from the first is still active. The net effect is that the latest user intent always wins: CA monitors the newest mode change with a fresh confirmation window.

**Invariant:** only one confirmation window is active at a time. Starting a new window cancels the previous one (via `clear_manual_override()` then `start_override_confirmation()`).

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
| `DEFAULT_SLEEP_HEAT` | `66` | °F | Bedtime heating target (default: `comfort_heat − 4°F`); overrides adaptive depth when `sleep_heat` is explicitly configured (#101) |
| `DEFAULT_SLEEP_COOL` | `78` | °F | Bedtime cooling target (default: `comfort_cool + 3°F`); overrides adaptive depth when `sleep_cool` is explicitly configured (#101) |
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
| `DEFAULT_OVERRIDE_CONFIRM_SECONDS` | `600` | seconds (10 min) | Debounce window between detecting a thermostat mode change and formally accepting it as a manual override. During this window `apply_classification()` is blocked. Transient glitches (thermostat restart, HA echo) that resolve within the window are discarded without starting a grace period. Set to 0 to confirm overrides immediately. See [Grace Periods Spec — Override Confirmation Delay](grace-periods-spec.md#override-confirmation-delay). |
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
| `THERMAL_SAMPLE_INTERVAL_SECONDS` | `60` | seconds | Active-phase HVAC sampling cadence (ungated; all polls recorded) |
| `THERMAL_PRE_HEAT_BUFFER_MINUTES` | `15` | minutes | Rolling pre-HVAC sample window included in k_passive regression |
| `THERMAL_MAX_ACTIVE_SAMPLES` | `120` | samples | Cap on active-phase samples (2 hours at 60s cadence) |
| `THERMAL_MAX_POST_HEAT_SAMPLES` | `45` | samples | Cap on post-heat samples (45 min at 60s cadence) |
| `THERMAL_MIN_R_SQUARED` | `0.2` | — | Minimum R² for k_passive OLS regression to accept an observation |
| `THERMAL_MIN_POST_HEAT_SAMPLES` | `4` | samples | Minimum post-heat samples required before committing an HVAC observation (Issue #130 D14: lowered from 10; enables short 5–30 min cycles) |
| `THERMAL_HVAC_MIN_SIGNAL_F` | `0.5` | °F | Minimum `|T_peak − T_start|` for a heating/cooling cycle to be treated as meaningful signal. Below this the cycle is a setpoint-maintenance run and is rejected (Issue #130 D23) |
| `THERMAL_K_PASSIVE_MIN` | `-0.5` | hr⁻¹ | Sanity lower bound for k_passive (very leaky envelope) |
| `THERMAL_K_PASSIVE_MAX` | `-0.001` | hr⁻¹ | Sanity upper bound for k_passive (very well insulated) |
| `THERMAL_K_ACTIVE_HEAT_MIN` | `0.5` | °F/hr | Minimum credible HVAC heating contribution |
| `THERMAL_K_ACTIVE_HEAT_MAX` | `15.0` | °F/hr | Maximum credible HVAC heating contribution |
| `THERMAL_K_ACTIVE_COOL_MIN` | `-15.0` | °F/hr | Maximum credible HVAC cooling contribution (magnitude) |
| `THERMAL_K_ACTIVE_COOL_MAX` | `-0.5` | °F/hr | Minimum credible HVAC cooling contribution (magnitude) |
| `THERMAL_DECAY_MAX_WINDOW_MINUTES` | `60` | minutes | Wall-clock limit before `ventilated_decay` / `fan_only_decay` abandon (H4) |
| `THERMAL_ROLLING_WINDOW_MINUTES` | `30` | minutes | Rolling commit+restart interval for all four non-HVAC decay types (H2) |
| `THERMAL_ROLLING_MIN_DELTA_T_F` | `0.2` | °F | Minimum total indoor ΔT to commit a short rolling window (H2 ΔT guard) |
| `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | `300` | seconds (5 min) | Sample gate for `passive_decay` and `ventilated_decay` (H1) |
| `THERMAL_FAN_SAMPLE_INTERVAL_S` | `120` | seconds (2 min) | Sample gate for `fan_only_decay` — faster than passive dynamics (H1) |
| `THERMAL_SOLAR_SAMPLE_INTERVAL_S` | `300` | seconds (5 min) | Sample gate for `solar_gain` (H1) |
| `THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S` | `300` | seconds (5 min) | Sample gate for HVAC post-heat phase — passive dynamics (H1) |
| `THERMAL_SOLAR_FACTOR_MIN_RANGE` | `0.30` | — | Minimum solar_factor range (max−min) across ventilated_decay samples to trigger 2-param OLS (Issue #126) |

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
| `_fan_command_time` | `None` | UTC timestamp of the most recent `_activate_fan()` / `_deactivate_fan()` call; read by `_is_recent_fan_command()` |
| `_pre_fan_hvac_mode` | `None` | HVAC mode captured before whole-house fan activation; restored on deactivation (`FAN_MODE_WHOLE_HOUSE` / `both` only) |

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

When all conditions are met: the comfort band **stays armed** (HVAC is **not** set to `off` — Issue #249; the thermostat self-arbitrates with the open window), the fan is activated (per the configured `fan_mode`), and `_natural_vent_active` is set to `True`. Activation is gated on **fan configuration + temperature, not occupancy** — a configured fan is the user's opt-in to fan-assisted ventilation, so nat-vent runs for free cooling home or away (#231 handles the comfort-ceiling exit so an empty home is not over-cooled); a user opts out of nat-vent by not configuring a fan.

### Exit Hierarchy

Exit conditions are evaluated in priority order on every continuous-monitoring check (`check_natural_vent_conditions()`). The highest-priority matching condition wins.

| Priority | Trigger | Condition | Action | Event emitted | Notes |
|---|---|---|---|---|---|
| 1 | All monitored sensors close | — | Exit nat vent; resume HVAC from current classification | — | — |
| 2 | `indoor_temp ≤ comfort_heat` (daytime) or `indoor_temp ≤ sleep_heat − hysteresis` (sleep window) | — | Exit; restore heat mode at the applicable floor temperature | `nat_vent_comfort_floor_exit` | Sleep-window variant: `sleep_heat − hysteresis` is one step below the cycling-off threshold, so the session ends only after the fan has already paused — see Fan Cycling section below |
| 3 | `outdoor_temp ≥ indoor_temp` | — | Exit to paused state; fan off; start hysteresis lockout timer | `nat_vent_outdoor_rise_exit` | — |
| 4 | `outdoor_temp > comfort_cool + nat_vent_delta` | — | Exit to paused state; fan off | — | — |

**Priority 1 (sensor closes)** always wins. When the physical path for airflow is closed, nat vent ends immediately regardless of outdoor temperature comparisons.

**Priority 2 (comfort floor)** restores heat rather than simply pausing. During the daytime, the exit fires at `comfort_heat`; during the sleep window it fires at `sleep_heat − hysteresis` (one step below the cycling-off threshold). In both cases the right action is to restore heat, not wait for outdoor conditions to change. The sleep-window threshold is deliberately set below the cycling-off point so the fan can complete a graceful pause at `sleep_heat` before the session ends.

**Priority 3 (outdoor warms above indoor)** starts a hysteresis lockout timer (see Re-activation section below). Without this lockout, the system would oscillate at thermal equilibrium: outdoor rises above indoor → exit → cooling resumes → outdoor drops below indoor → re-activate → repeat.

### Re-activation from Pause

When nat vent has exited due to an outdoor-warm event (Priority 2 above), re-activation requires all three of the following simultaneously:

| Condition | Value | Rationale |
|---|---|---|
| `outdoor_temp < indoor_temp - 1.0°F` | 1°F hysteresis band | Prevents immediate re-activation when temperatures are nearly equal; outdoor must be meaningfully cooler |
| Time elapsed since last outdoor-warm exit ≥ 300 seconds | 5-minute lockout | Prevents oscillation when outdoor and indoor temperatures are at near-equilibrium; gives thermal conditions time to settle |
| `outdoor_temp < comfort_cool + nat_vent_delta` | Ceiling still valid | Ensures outdoor air is still within the useful temperature range |

If all three conditions are met, nat vent re-activates: HVAC remains off, fan turns on, `_natural_vent_active` is set back to `True`.

#### Archetype-aware reactivation gate (Issue #392 Fix 1) — cross-reference §6c and §9

The re-activation condition table above is the primary, direction/floor/ceiling-delta gate. As of Issue #392, all four call sites that (re)activate nat-vent additionally require the **archetype-aware ceiling condition** from §6c — `self._ceiling_threshold(comfort_cool) is None OR indoor <= ceiling_threshold` — before proceeding, mirroring the ODE ceiling guard's own dormancy check so the guard and the reactivation gates can no longer disagree with each other. For `FAN_MODE_HVAC`, this blocks reactivation once indoor is already past the ceiling (same behavior as before Issue #392). For `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH`, `_ceiling_threshold()` returns `None`, so this condition is always satisfied and reactivation is governed purely by the direction/floor/ceiling-delta gate above — a WHF keeps running (or resumes) whenever outdoor is still cooler than indoor, regardless of how far indoor has drifted above `comfort_cool`. See §9 (Structural WHF/AC Mutual Exclusion) for why this is safe: mutual exclusion with the compressor is enforced structurally, not by this gate.

The four call sites, all in `automation.py`:

| # | Function | Role |
|---|---|---|
| 1 | `handle_door_window_open()` | Sensor-open debounce callback — initial nat-vent activation |
| 2 | `check_natural_vent_conditions()` | Grace re-entry branch — reactivation after a grace period |
| 3 | `nat_vent_temperature_check()` | Paused-state reactivation (variable named `_comfort_ceiling_ok` here, distinct from an existing `_ceiling_ok` in the same function that means outdoor-vs-nat-vent-delta ceiling, not the comfort-cool ceiling) |
| 4 | `_re_pause_for_open_sensor()` | Re-pause-time reactivation check after grace expires with a sensor still open. Also now calls `_apply_nat_vent_hvac_state()` after `_activate_fan()`, matching the other three sites — this was previously the one site that skipped that call, which was an inconsistency independent of the ceiling logic, fixed alongside it. |

All four are wrapped in `self._decision_lock` (§9g) as part of Issue #392 Fix 3, so a reactivation decision from any one of them cannot interleave with a decision from `apply_classification()` or another of the six locked entry points.

**`apply_classification()` short-circuits for WHF (Issue #392 Fix 1b)** — see §9 (Structural WHF/AC Mutual Exclusion) for the full mechanism. In summary: when `_natural_vent_active` is `True` and `aggressive_savings` is `False` (the default), `apply_classification()` used to fall through to `_apply_comfort_band()` regardless of fan archetype, re-arming `cool` on the thermostat every 30-minute cycle even while a WHF session was actively suppressing HVAC. It now returns immediately after `_apply_nat_vent_hvac_state()` when `fan_mode` is `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH`, so the classification cycle never attempts a comfort-band write that the choke-point guard would otherwise silently block. `FAN_MODE_HVAC` keeps falling through unchanged, since fan and compressor coexist for that archetype.

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

### Fan Cycling Within an Active Session (Issues #321, #374)

Once `_natural_vent_active = True`, the fan does not simply stay on until the session ends. Instead, the engine targets a context-dependent temperature and cycles the fan on and off using a hysteresis band to prevent rapid toggling. The target and thresholds differ between the daytime and sleep windows.

**Target and threshold table** (constant `NAT_VENT_HYSTERESIS_F = 1.0°F`):

| Context | `nat_vent_target` | Fan cycles OFF (`off_threshold`) | Fan cycles ON (`on_threshold`) | Hard-exit floor |
|---|---|---|---|---|
| Daytime | `(comfort_heat + comfort_cool) / 2` | `target − hysteresis` | `target + hysteresis` | `comfort_heat` |
| Sleep window | `sleep_heat + hysteresis` | `sleep_heat` (= `target − hysteresis`) | `sleep_heat + 2 × hysteresis` (= `target + hysteresis`) | `sleep_heat − hysteresis` |

*Sleep-window note:* The sleep target is the sleep floor plus one hysteresis step, so the fan cools the home to `sleep_heat` (cycling off there) and then maintains it by re-activating at `sleep_heat + 2 × hysteresis`. The hard-exit threshold (`sleep_heat − hysteresis`) sits one step below the cycling-off point, so the session ends only if indoor temperature falls past `sleep_heat` — i.e., after the fan has already paused.

**Fan cycles off (indoor ≤ off_threshold):**
- `_fan_active` is set to `False`; fan deactivated.
- `_natural_vent_active` remains `True` — the session is still active.
- `fan_status` sensor reports `"nat-vent (session active, fan idle)"`.
- The comfort band stays armed throughout; the thermostat continues to self-arbitrate.

**Fan cycles on again (indoor ≥ on_threshold):**
- Fan reactivates if `outdoor_temp < indoor_temp` (directional check still applies).
- The on_threshold guard prevents re-activation the moment the fan turns off (1°F dead band).

**Hard exit (session ends) — takes priority over cycling:**
The exit hierarchy (§17 Exit Hierarchy above) is evaluated before the cycling logic. Priority 2 fires first if indoor drops to the applicable floor, ending the session (`_natural_vent_active = False`) and restoring heat mode. Fan cycling cannot keep the session alive past the hard-exit floor.

**Daytime example** (comfort band [68°F, 74°F], target = 71°F):
1. Indoor = 73°F → fan on, session active.
2. Indoor falls to 70°F (= off_threshold) → fan cycles off, session stays active.
3. Indoor drifts back to 72°F (= on_threshold) → fan cycles on again.
4. Indoor falls to 68°F (= comfort_heat = hard-exit floor) → hard exit; heat mode restored.

**Sleep-window example** (sleep band [65°F, 72°F], `sleep_heat=65`, `hysteresis=1°F`):
1. Indoor = 73°F (above on_threshold 67°F) → fan on, session active.
2. Indoor falls to 65°F (= off_threshold = sleep_heat) → fan cycles off, session stays active.
3. Indoor drifts back to 67°F (= on_threshold) → fan cycles on again.
4. Indoor falls to 64°F (= sleep_heat − hysteresis = hard-exit floor) → hard exit; heat mode restored.

**Fan event `fan_device` field (Issue #374):** All fan-related events — `nat_vent_fan_on`, `nat_vent_fan_off`, `fan_activated`, `fan_deactivated`, `nat_vent_bedtime_continue` — carry a `fan_device` field indicating which hardware was activated: `"whf"`, `"hvac_fan"`, `"both"`, or `"none"`.

**Removed event:** `nat_vent_sleep_ceiling_reached` is no longer emitted. The Priority 0 exit that fired when `indoor_temp ≤ sleep_cool` during the sleep window has been removed. The session now persists through the sleep window, cycling the fan to maintain the sleep floor.

**`fan_status` sensor values** (complete list, including the value added in Issue #374):

| Value | Meaning |
|---|---|
| `"active"` | CA commanded the fan on (nat vent or HVAC fan-only mode); physical state confirmed for WHF |
| `"active (unconfirmed)"` | CA flag `_fan_active=True` but WHF physical state reads off — stale flag after manual stop; WARNING logged |
| `"nat-vent (session active, fan idle)"` | Nat-vent session alive but fan has cycled off (indoor at or below off_threshold) |
| `"running (manual override)"` | Fan is running; CA's `_fan_override_active` flag is set |
| `"running (untracked)"` | Thermostat reports fan running but CA's `_fan_active=False` — typical after HA restart or user-initiated fan run |
| `"inactive"` | Fan is off and CA has no record of activating it |
| `"off (manual override)"` | Override still in effect but physical fan is off (`_fan_override_active=True AND _fan_active=False`) |
| `"disabled"` | Fan control feature is turned off in configuration |

**Test coverage:** `tests/test_nat_vent_thermostat.py`; golden scenario `nat_vent_thermostat_cycling` (Issue #321). Sleep-window cycling behavior added in Issue #374.

### Phase 2 Note

Trajectory-aware look-ahead — using the thermal model and short-range outdoor temperature forecast to project the activation window into the future — is deferred to Issue #116.

---

## 18. Automation Logic Table

This is the definitive reference for expected system behavior across all classification contexts and sensor/user events. Every cell describes what the automation engine does when a given event fires in a given classification context.

### Classification Contexts

| Code | Day Type | HVAC Mode / Band | windows_recommended | Window Period |
|------|----------|-----------|---------------------|---------------|
| C1 | Hot | cool | False | N/A |
| C2 | Warm | band `[comfort_heat, comfort_cool]` ¹ | True | In period (6–10 AM) |
| C3 | Warm | band `[comfort_heat, comfort_cool]` ¹ | True | Outside period |
| C4 | Warm | band `[comfort_heat, comfort_cool]` ¹ | False | N/A (today_low too high) |
| C5 | Mild | band `[comfort_heat, comfort_cool]` ¹ | True | In period (10 AM – 5 PM) |
| C6 | Cool | heat | False | N/A |
| C7 | Cold | heat | False | N/A |

¹ Issue #249: warm/mild days arm a comfort band rather than setting `hvac_mode=off`. The band values shown are for home/guest occupancy; setback bands apply when away/vacation. See [§6e Comfort-Band Programming](#6e-comfort-band-programming-issue-249).

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
| E8 | HA restart — coalesce reconciliation fires (Issue #327) |

### Expected Outcomes

| | E1: Sensor Open | E2: All Closed | E3: Grace+Open | E4: Override | E5: Fan Change | E6: Class Change | E7: Resume | E8: Restart Reconcile |
|---|---|---|---|---|---|---|---|---|
| C1 (hot/cool) | Pause HVAC→off, notify | Resume to cool, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace; thermostatic loop exits fan if `outdoor ≥ indoor` (§9e) | Re-apply classification | Resume cool, manual grace | Fan adopt-on (nat-vent eligible) or turn-off (not eligible); `Fan reconcile:` logged |
| **C2 (warm/band/win=T/in)** | **No pause** (planned window); reactivation gated by archetype-aware ceiling (§6c/§17 — WHF: direction-only; HVAC-fan: blocked once indoor > ceiling) | No-op (not paused) | **No re-pause** (planned); same archetype-aware ceiling gate applies | N/A (not paused) | Fan on, band stays armed for `FAN_MODE_HVAC`; for WHF, `apply_classification()` short-circuits before the band write (Issue #392 Fix 1b, §9) | Re-apply band `[comfort_heat, comfort_cool]` (`FAN_MODE_HVAC` only — WHF short-circuits per §9); §6b backstop fires if indoor < comfort_heat | N/A (not paused) | Fan adopt-on (nat-vent eligible) or turn-off; band re-armed by coalesce |
| C3 (warm/band/win=T/out) | No pause (band armed, not paused); same archetype-aware ceiling gate | No-op | N/A | N/A | Fan on, band stays armed for `FAN_MODE_HVAC`; WHF short-circuits per §9 | Re-apply band (`FAN_MODE_HVAC` only); §6b backstop fires if indoor < comfort_heat | N/A | Fan turn-off (outside window period → not nat-vent eligible) or no-fan |
| C4 (warm/band/win=F) | No pause (band armed, not paused) | No-op | N/A | N/A | Band stays armed | Re-apply band; §6b backstop fires if indoor < comfort_heat | N/A | Fan turn-off if physically running (no sensors open → not nat-vent eligible) |
| **C5 (mild/band/win=T/in)** | **No pause** (planned window); same archetype-aware ceiling gate | No-op | **No re-pause** (planned); same gate | N/A | Fan on, band stays armed for `FAN_MODE_HVAC`; WHF short-circuits per §9 | Re-apply band `[comfort_heat, comfort_cool]` (`FAN_MODE_HVAC` only) | N/A | Fan adopt-on (nat-vent eligible) or turn-off; band re-armed |
| C6 (cool/heat) | Pause HVAC→off, notify | Resume to heat, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace; thermostatic loop exits fan if `outdoor ≥ indoor` | Re-apply | Resume heat, manual grace | Fan turn-off (heat day → not nat-vent eligible) or no-fan |
| C7 (cold/heat) | Pause HVAC→off, notify | Resume to heat, auto grace | Re-pause, notify | Clear pause, manual grace | Fan override grace; thermostatic loop exits fan if `outdoor ≥ indoor` | Re-apply | Resume heat, manual grace | Fan turn-off (cold day → not nat-vent eligible) or no-fan |

**Bolded cells** have corresponding test coverage in `tests/test_windows_recommended_integration.py`.

**Comfort-band model (Issue #249, §6e):** In C2–C5 contexts (warm/mild days), `apply_classification()` now programs a comfort band rather than setting `hvac_mode=off`. The band arms the thermostat with both a floor and a ceiling; the thermostat self-arbitrates between them. Nat-vent and economizer activate the fan only — the band remains armed throughout, so free cooling stays free and the compressor engages only if the breeze can't hold the ceiling.

**Comfort-floor guard (§6b — passive backstop):** In C2, C3, and C4 contexts, the band floor (`comfort_heat` while home + awake; `setback_heat` away/asleep) keeps the home from falling below the floor autonomously. The `warm_day_comfort_gap` event and §6b heat-up path remain as a safety backstop for situations where the band has lapsed (HA restart, thermostat reconnect). Test coverage: `tests/test_warm_day_comfort_gap.py`.

**Thermostatic fan loop (Issue #327, §9e):** In all C1–C7 contexts, once the fan is CA-owned and running, `fan_thermostat_check()` re-evaluates on every indoor or outdoor temperature change. The fan is turned off immediately when `outdoor ≥ indoor` — it does not wait for the next 30-minute coordinator poll. See §9e for the full exit hierarchy and trigger-source table.

**Restart reconciliation (E8, Issue #327, §9e):** `_fan_override_active` is always cleared on restart; `_do_startup_coalesce` decides adopt-on, turn-off, or no-fan based on live thermostat state. E8 applies uniformly to all contexts — the decision depends on current physical conditions, not the day classification.

**Archetype-aware nat-vent ceiling and structural WHF/AC exclusion (Issue #392):** In C2/C3/C5, E1/E3 (reactivation) now consistently apply the archetype-aware ceiling threshold from §6c/§17 across all four reactivation gate sites (`handle_door_window_open()`, `check_natural_vent_conditions()`, `nat_vent_temperature_check()`, `_re_pause_for_open_sensor()`) — `FAN_MODE_HVAC` blocks reactivation once indoor exceeds `comfort_cool` (unchanged from before #392); `FAN_MODE_WHOLE_HOUSE`/`FAN_MODE_BOTH` reactivates purely on outdoor/indoor direction. In E5/E6, `apply_classification()` now short-circuits before the comfort-band write when a WHF session owns the thermostat (§9), and the `_whf_owns_hvac()` choke-point guard in `_set_hvac_mode()`/`_set_temperature()` (§9) makes WHF/AC mutual exclusion structural for every cell in this table, not just the ones exercised by nat-vent. All six automation entry points relevant to this table (`apply_classification`, `handle_door_window_open`, `handle_all_doors_windows_closed`, `check_natural_vent_conditions`, `_re_pause_for_open_sensor`, `nat_vent_temperature_check`) are additionally serialized by `self._decision_lock` (§9g) so that concurrent E1/E3/E5/E6 triggers cannot interleave on shared engine state.

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
| C2×E6 (band armed) | test_warm_day_comfort_gap.py | TestWarmDayBandArmingReplacesComfortGap — band `[comfort_heat, comfort_cool]` armed; §6b backstop only if band lapses |
| C4×E6 (band armed) | test_warm_day_setback.py | TestWarmDayBandArming::test_warm_day_dual_thermostat_sets_dual_setpoints |
| C2×E5 / C3×E5 / C5×E5 (band stays armed on nat-vent) | test_window_hvac_interaction.py, test_door_window.py | Band remains armed when fan activates; no `hvac_mode=off` issued |
| C2×E6 / C5×E6 (band applied on re-classification) | test_thermostat_program.py, test_production_harness.py | `apply_classification` arms band `[comfort_heat, comfort_cool]` (occupied+awake, any day type) |
| All×E8 (coalesce: turn-off, no nat-vent) | _(test-ref pending)_ | restart clears `_fan_override_active`; coalesce turns off fan when nat-vent not eligible |
| All×E8 (coalesce: adopt-on) | _(test-ref pending)_ | coalesce adopts running fan as CA nat-vent when conditions hold; `_natural_vent_active=True` |
| C1×E5 / C6×E5 / C7×E5 (thermostatic exit: `outdoor ≥ indoor`) | _(test-ref pending)_ | `fan_thermostat_check` turns fan off on `outdoor ≥ indoor` before next 30-min poll |
| D×E5 (economizer: `outdoor ≥ indoor` blocked) | _(test-ref pending)_ | economizer `check_window_cooling_opportunity` rejects activation when `outdoor ≥ indoor` |
| C2/C3/C5×E1/E3 (archetype-aware ceiling, `FAN_MODE_HVAC` blocks reactivation past ceiling) | test_nat_vent_activation.py, test_fan_control.py | Issue #392 Fix 1 — function names pending as of this doc pass; see files directly |
| C2/C3/C5×E1/E3 (archetype-aware ceiling, `FAN_MODE_WHOLE_HOUSE` reactivates direction-only past ceiling, incl. #392 repro sequence) | test_nat_vent_activation.py, test_whole_house_fan_hvac_suppression.py | Issue #392 Fix 1 — function names pending as of this doc pass; see files directly |
| C2/C3/C5×E5/E6 (`_whf_owns_hvac()` choke-point guard blocks active-mode writes; `apply_classification()` WHF short-circuit) | test_whole_house_fan_hvac_suppression.py, test_fan_control.py | Issue #392 Fix 1b — function names pending as of this doc pass |
| All×E1/E2/E3/E5/E6 (idempotent `_activate_fan()`/`_deactivate_fan()`; no duplicate `fan_activated`/`fan_deactivated` on redundant calls) | test_fan_control.py | Issue #392 Fix 1c — function names pending as of this doc pass |
| All×E1/E2/E3/E5/E6 (`_decision_lock` serializes the six entry points; no interleaved execution under `asyncio.gather()`) | test_nat_vent_activation.py | Issue #392 Fix 3 — function names pending as of this doc pass |
| Fan archetype activity-log labels (`fan_activated`/`fan_deactivated`/`fan_manual_override`/`fan_cancel` render `fan_device`) | test_activity_renderers.py | Issue #392 Fix 2 — function names pending as of this doc pass |

---

## 19. Chart Activity Bar Invariants

The temperature forecast chart displays four activity bars fed by `ChartStateLog.append()` in `coordinator.py`. All four append call sites must use these helper methods — do not substitute raw thermostat state strings.

| Bar | Field name | Required source | Frontend color |
|---|---|---|---|
| HVAC | `hvac` | `_read_chart_hvac_action()` | `"heating"` → red; `"cooling"` → blue; `"fan"` → green; others → no segment |
| Fan | `fan` | `_fan_is_running()` | `true` → green |
| Windows Recommended | `windows_recommended` | `bool(self._current_classification.windows_recommended) if self._current_classification else False` | `true` → amber |
| Windows Open | `windows_open` | `self._any_sensor_open()` | `true` → green |

**Critical invariants:**
- The `hvac` field MUST be the thermostat's `hvac_action` attribute string (`"heating"`, `"cooling"`, `"fan"`, `"idle"`, `"off"`) — never the `hvac_mode` state (`"heat"`, `"cool"`). Mode strings produce invisible segments.
- Use `_read_chart_hvac_action()` at every append site. It encapsulates the #109 fan→heating/cooling remap (only applies when `fan_mode` is auto).
- Use `_fan_is_running()` for the `fan` field — never `_fan_active` directly. The helper includes ground-truth thermostat fallback for untracked fan runs.

**Four append sites in coordinator.py:**
1. Classification change event (event-driven)
2. 30-minute poll (periodic)
3. Manual override event (event-driven)
4. HVAC action transition event (event-driven)

All four sites are covered by tests in `tests/test_coordinator_chart.py`.

---

## 20. Chart Log Write Guards

### Bug A — pred_indoor gated on indoor_temp availability

`pred_indoor` and `pred_outdoor` are only written to the chart log when
`indoor_temp` (the actual sensor/climate-entity read for that coordinator tick)
is also available. If the thermostat is in `unknown` or `unavailable` state —
as occurs during an HA restart — both `indoor` and `pred_indoor` are null for
that tick. This prevents restart artifacts from permanently corrupting the
predicted indoor trend line (`histPredIndoorPts` on the dashboard chart).

The guard lives in `_async_update_data()`:

```python
if _pred_in and _now_h < len(_pred_in) and indoor_temp is not None:
    _pred_indoor_val = _pred_in[_now_h]["temp"]
```

A `DEBUG`-level log is emitted when `indoor_temp` is `None` so the skip is
visible in HA logs without cluttering normal operation.

### Bug B — plausible indoor temperature range filter

Indoor temperatures read from the thermostat or a dedicated sensor entity are
validated against a physical plausibility range defined by module-level
constants:

| Constant | Value | Meaning |
|---|---|---|
| `_MIN_PLAUSIBLE_INDOOR_F` | 40.0 °F | Below this the reading is treated as a sensor glitch |
| `_MAX_PLAUSIBLE_INDOOR_F` | 110.0 °F | Above this the reading is treated as a sensor glitch |

Values outside this range are logged at `WARNING` level and cause
`_get_indoor_temp()` to return `None` rather than propagating the bad reading
into the chart log. The most common trigger is a thermostat that briefly echoes
its new setpoint into `current_temperature` during a setpoint-only transition;
if the 30-minute coordinator tick fires at that moment, the out-of-range value
would otherwise appear as a permanent spike on the actual indoor line.

The range check applies to both the `TEMP_SOURCE_SENSOR` /
`TEMP_SOURCE_INPUT_NUMBER` branch and the `TEMP_SOURCE_CLIMATE_FALLBACK`
branch of `_get_indoor_temp()`.

### Test coverage

| Test | File |
|---|---|
| `test_pred_indoor_not_written_when_indoor_temp_none` | `tests/test_coordinator_chart.py` |
| `test_pred_indoor_written_when_indoor_temp_available` | `tests/test_coordinator_chart.py` |
| `test_indoor_temp_range_check_rejects_extreme_low` | `tests/test_coordinator_chart.py` |
| `test_indoor_temp_range_check_rejects_extreme_high` | `tests/test_coordinator_chart.py` |
| `test_indoor_temp_range_check_accepts_normal` | `tests/test_coordinator_chart.py` |

---

## 21. Thermal Learning Health

### 21.1 Overview

The thermal learning engine uses OLS regression and quality gates to ensure only reliable observations update the model. Prior to Issue #124, rejections were logged as warnings with no persistent audit trail, making it impossible to distinguish "correctly rejecting noise" from "not learning anything" without SSH access. Issue #124 adds structured rejection events and a `learning_health` surface so the model's decision process is auditable without log access.

No OLS math, automation behavior, or thermal thresholds changed in Issue #124. The only behavioral difference is that `_abandon_observation()` now logs at `INFO` level (downgraded from `WARNING`) because rejections are expected steady-state behavior, not anomalies.

### 21.2 Rejection Reason Codes

Six `REJECT_*` constants in `const.py` identify every point where an observation can be discarded. Each constant is also stored as the `reason_code` field in the `ThermalRejectionEvent` emitted at that point.

| Constant | Value | When fired |
|---|---|---|
| `REJECT_TOO_FEW_SAMPLES` | `"too_few_samples"` | Sample count < required minimum before OLS runs |
| `REJECT_SMALL_DELTA` | `"small_delta"` | Total indoor ΔT below `THERMAL_ROLLING_MIN_DELTA_T_F` (0.2°F) |
| `REJECT_OLS_BAD_FIT` | `"ols_bad_fit"` | OLS R² < `THERMAL_MIN_R_SQUARED` (0.2) |
| `REJECT_OLS_WRONG_SIGN` | `"ols_wrong_sign"` | OLS produced a positive k_passive (physics violation) |
| `REJECT_OLS_BOUNDS` | `"ols_bounds"` | k_passive outside `[THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX]` = `[-0.5, -0.001]` hr⁻¹ |
| `REJECT_ABANDONED` | `"abandoned"` | Observation abandoned before OLS could run (e.g., HVAC mode change, wall-clock timeout) |

### 21.3 `ThermalRejectionEvent` Fields

`ThermalRejectionEvent` is a `TypedDict` defined in `learning.py`. An instance is emitted at every rejection point and appended to the per-obs-type rejection log.

| Field | Type | Description |
|---|---|---|
| `obs_type` | `str` | Observation type that was rejected (e.g., `"passive_decay"`) |
| `reason_code` | `str` | One of the `REJECT_*` constants |
| `n_samples` | `int` | Sample count at rejection time |
| `n_required` | `int` | Minimum required for this observation type |
| `r_squared` | `float \| None` | R² achieved; `None` when OLS never ran (e.g., `too_few_samples`, `abandoned`) |
| `r_squared_required` | `float \| None` | R² floor (`THERMAL_MIN_R_SQUARED = 0.2`); `None` when OLS never ran |
| `delta_t_f` | `float \| None` | Observed indoor ΔT in °F at rejection time |
| `delta_t_required` | `float \| None` | Required ΔT floor (`THERMAL_ROLLING_MIN_DELTA_T_F = 0.2°F`) |
| `elapsed_minutes` | `int \| None` | Wall-clock duration of the observation in minutes |
| `timestamp` | `str` | ISO 8601 datetime of the rejection |

### 21.4 `compute_k_passive()` 3-Tuple Return

`compute_k_passive()` in `learning.py` previously returned a 2-tuple `(k_passive, r_squared)` — returning `(None, 0.0)` for five distinct failure modes with no way for the caller to distinguish them. Issue #124 extends the return to a 3-tuple `(k_passive, r_squared, reason_code)`:

| Failure path | k_passive | r_squared | reason_code |
|---|---|---|---|
| Too few samples (< min + 1) | `None` | `0.0` | `REJECT_TOO_FEW_SAMPLES` |
| Too few valid rate/delta pairs | `None` | `0.0` | `REJECT_TOO_FEW_SAMPLES` |
| No variation (sum_d2 == 0) | `None` | `0.0` | `REJECT_SMALL_DELTA` |
| k_passive outside bounds | `None` | `0.0` | `REJECT_OLS_BOUNDS` |
| R² < minimum | `None` | r_squared | `REJECT_OLS_BAD_FIT` |
| Success | k_passive | r_squared | `None` |

All callers in `coordinator.py` unpack the 3-tuple and use the `reason_code` to populate the `ThermalRejectionEvent` before calling `_abandon_observation()`.

### 21.5 `THERMAL_MIN_DECAY_SAMPLES` Alignment Contract

`THERMAL_MIN_DECAY_SAMPLES = 4` is the single source of truth for OLS sample-pair floors on rolling decay observations.

The coordinator pre-gates on `THERMAL_MIN_DECAY_SAMPLES + 1 = 5` pairs before calling OLS. This guarantees that at least 4 pairs are available for rate-pair construction inside `compute_k_passive()`. The inner function's own floor check (`_min_s = THERMAL_MIN_DECAY_SAMPLES`) is therefore never reached unless the outer gate logic is bypassed.

`THERMAL_MIN_POST_HEAT_SAMPLES = 10` governs HVAC post-heat events and is a separate, independent constant. Do not change either constant independently — the `+1` offset between the outer gate and the inner floor is intentional and must be preserved.

### 21.6 `learning_health` Dict in `get_thermal_model()`

`get_thermal_model()` returns a `learning_health` key containing per-obs-type health summaries aggregated from the coordinator's `_rejection_log`:

```
learning_health: {
    obs_type: {
        "attempts":   int,          # total observation starts (committed + all rejections)
        "committed":  int,          # successful commits to LearningState
        "rejections": {
            "too_few_samples": int,
            "small_delta":     int,
            "ols_bad_fit":     int,
            "ols_wrong_sign":  int,
            "ols_bounds":      int,
            "abandoned":       int,
        },
        "last_rejection": ThermalRejectionEvent | None,
    }
    for obs_type in [
        "hvac_heat", "hvac_cool", "passive_decay",
        "fan_only_decay", "ventilated_decay", "solar_gain"
    ]
}
```

The coordinator builds this dict from `self._rejection_log` and passes it to `get_thermal_model()`, which includes it verbatim in the returned dict.

### 21.7 Persistence

- `self._rejection_log: dict[str, list[dict]]` is stored on the coordinator instance, keyed by obs_type.
- Each per-obs-type list is capped at **100 entries** (oldest evicted first when the cap is reached).
- Maximum total stored: **600 entries** across 6 obs types.
- Persisted across HA restarts via `LearningState.rejection_log`. The cap is enforced on load to guard against file corruption.

### 21.8 Sensor Attribute Exposure

`ClimateAdvisorComplianceSensor.extra_state_attributes` exposes a `thermal_learning_health` key. In compliance with the security rule against exposing raw behavior data in attributes, only summary counts and the last rejection reason code are exposed — not the full `ThermalRejectionEvent` dicts:

```
thermal_learning_health: {
    obs_type: {
        "attempts":              int,
        "committed":             int,
        "rejections":            {reason_code: int, ...},
        "last_rejection_reason": str | None,
    }
}
```

### 21.9 `tools/thermal_health.py` Usage

Standalone CLI tool. Requires `HA_URL` and `HA_TOKEN` environment variables (same pattern as `tools/validate.py`). Prints two sections.

**Section 1 — Historical Aggregates** (existing): reads `thermal_learning_health` from the compliance sensor attribute via HA REST API. Shows per-obs-type rejection counts and last rejection reason.

```
Thermal Learning Health Report
═══════════════════════════════
obs_type            attempts  committed  rejections  last rejection
─────────────────────────────────────────────────────────────────
passive_decay       12        3          9           too_few_samples (n=3/5)
hvac_heat           8         5          3           ols_bad_fit (R²=0.08/0.20)
hvac_cool           6         4          2           abandoned
fan_only_decay      2         0          2           too_few_samples (n=2/5)
ventilated_decay    0         0          0           —
solar_gain          1         0          1           small_delta (ΔT=0.1°F/0.2°F)
```

**Section 2 — Current Observations** (added in Issue #125): reads `thermal_pipeline` from `GET /api/climate_advisor/automation_state`. Shows a live table of every observation currently accumulating samples. Fields are sourced from `_build_thermal_pipeline_summary()` in `coordinator.py`.

```
Current Observations
--------------------
obs_type            status      elapsed   samples  last_smp  indoor           outdoor   delta
ventilated_decay    monitoring  164.3 min 6        2.1 min   71.8-72.1°F      69.0°F    0.3°F

(Rejection log entries: ventilated_decay=5)
```

If the debug-state endpoint is unreachable or returns no `thermal_pipeline` key, the tool prints a warning and skips Section 2 — it does not abort.

No new secrets or external dependencies. Run from the project root after setting env vars.

### 21.10 Test Coverage

| Test | File |
|---|---|
| `compute_k_passive()` 3-tuple reason codes (all 5 failure paths + success) | `tests/test_thermal_rejection.py` |
| Rejection log accumulation on abandon | `tests/test_thermal_rejection.py` |
| Rejection log per-type cap at 100 entries | `tests/test_thermal_rejection.py` |
| Rejection log persisted in `LearningState` and reloaded on startup | `tests/test_thermal_rejection.py` |
| `learning_health` present in `get_thermal_model()` with correct counts | `tests/test_thermal_rejection.py` |
| `last_rejection` populated after a rejection event | `tests/test_thermal_rejection.py` |
| `thermal_learning_health` in compliance sensor attributes | `tests/test_thermal_rejection.py` |
| Sensor attribute exposes counts/summary only (not raw events) | `tests/test_thermal_rejection.py` |

### 21.11 Log Taxonomy (Issue #125)

Issue #125 adds structured log lines at key points in the observation lifecycle. The following table documents every new log line, its level, source method, and field semantics.

| Log line format | Level | Source method | What it means |
|---|---|---|---|
| `Thermal rolling window: obs_type=<T> n=<N> elapsed=<E>min indoor=[<lo>..<hi>] (ΔT=<dt>°F) outdoor=<out>` | INFO | `_commit_rolling_window_obs()` | Fires immediately before every rolling-window commit attempt, including ones that will be rejected. `n` = sample count; `ΔT` = max−min indoor temp across samples; `outdoor` = last sample's outdoor temp or `?` if unavailable. |
| `Thermal pipeline: <N> pending observations active` | INFO | `_async_update_data()` | Emitted once per coordinator update cycle when at least one pending observation exists. Confirms the pipeline is alive without requiring full debug-state output. |
| `Thermal event commit failed (<T>): k_passive rejected (R²=<r2>, n=<N>, indoor_ΔT=<dt>°F) code=<code>` | INFO | `_commit_event_from_dict()` | Rejection of a decay observation after OLS. `indoor_ΔT` is the max−min span across all sample indoor temps. `code` is one of the `REJECT_*` constants. |
| `Thermal obs abandoned [type=<T> reason=<code> n=<N>/<req> dt=<dt>°F/? elapsed=<E>m]` | INFO | `_abandon_observation()` | Fires whenever an observation is discarded before commit. `elapsed` is now always populated from `obs["start_time"]` — the `?` value that appeared in Issue #124 logs no longer occurs. |
| `compute_k_passive: wrong sign k_p=<v> (must be < 0) n=<N>` | DEBUG | `compute_k_passive()` | OLS returned a positive k_passive — a physics violation. The observation is rejected with `REJECT_OLS_WRONG_SIGN`. |
| `compute_k_passive: out of bounds k_p=<v> (must be in [<min>, <max>]) n=<N>` | DEBUG | `compute_k_passive()` | OLS result is outside the `[THERMAL_K_PASSIVE_MIN, THERMAL_K_PASSIVE_MAX]` interval. Rejected with `REJECT_OLS_BOUNDS`. |

**Reading the rolling-window line during a flat-temperature episode:**

When indoor temperature is stable (HVAC holding setpoint, mild outdoor conditions), a sequence like this is normal and expected:

```
Thermal rolling window: obs_type=ventilated_decay n=6 elapsed=5.0min indoor=[72.0..72.0] (ΔT=0.00°F) outdoor=69.0
Thermal event commit failed (ventilated_decay): k_passive rejected (R²=0.000, n=6, indoor_ΔT=0.00°F) code=ols_bad_fit
Thermal obs abandoned [type=ventilated_decay reason=ols_bad_fit n=6/4 dt=0.00°F/? elapsed=35m]
```

`R²=0.000` with `indoor_ΔT=0.00°F` means the indoor temperature was effectively flat — there was no temperature excursion for OLS to fit. This is **not a bug**. The learning engine correctly refuses to extract a thermal decay rate from flat data; fitting a slope to a flat line would produce a meaningless or unstable k_passive. This condition occurs whenever indoor and outdoor temperatures are within 2–3°F of each other, or when HVAC is actively cycling to maintain a stable setpoint. Resolution: wait for a natural temperature excursion — a warm afternoon, a morning pre-heat, or an overnight cooldown — to provide the ≥ 0.2°F indoor ΔT the quality gate requires.

### 21.12 `thermal_pipeline` Key in `/api/climate_advisor/automation_state`

Issue #125 adds a `thermal_pipeline` key to the debug-state API response. This key is built on every call by `_build_thermal_pipeline_summary()` in `coordinator.py` and reflects the live state of all pending observations at the moment of the request.

**Response shape:**

```json
{
  "thermal_pipeline": {
    "pending": [
      {
        "obs_type": "ventilated_decay",
        "status": "monitoring",
        "elapsed_minutes": 164.3,
        "sample_count": 6,
        "last_sample_age_minutes": 2.1,
        "indoor_range_f": [71.8, 72.1],
        "indoor_delta_f": 0.3,
        "outdoor_f": 69.0
      }
    ],
    "rejection_log_counts": {
      "ventilated_decay": 5
    }
  }
}
```

**Field semantics:**

| Field | Type | Description |
|---|---|---|
| `pending` | `list` | One entry per obs_type currently in `_pending_observations`. Empty list when no observations are active. |
| `pending[].obs_type` | `str` | Observation type key (e.g., `"passive_decay"`, `"ventilated_decay"`) |
| `pending[].status` | `str` | Raw `status` field from the pending observation dict (e.g., `"monitoring"`) |
| `pending[].elapsed_minutes` | `float \| null` | Minutes since observation started; `null` if `start_time` is absent or unparseable |
| `pending[].sample_count` | `int` | Number of samples accumulated so far |
| `pending[].last_sample_age_minutes` | `float \| null` | Minutes since the most recent sample; `null` if `last_sample_time` is absent |
| `pending[].indoor_range_f` | `[float, float] \| null` | `[min, max]` of indoor temps across all samples; `null` if no samples have `indoor_temp_f` |
| `pending[].indoor_delta_f` | `float \| null` | `max − min` indoor temp; `null` if no samples |
| `pending[].outdoor_f` | `float \| null` | Outdoor temp from the last sample; falls back to coordinator's `_last_outdoor_temp`; `null` if neither is available |
| `rejection_log_counts` | `dict[str, int]` | Per-obs-type count of entries in `_rejection_log`. Mirrors the same data visible in `learning_health` but scoped to raw counts only, for quick triage without parsing `ThermalRejectionEvent` dicts. |

---

_Last Updated: 2026-06-12_
