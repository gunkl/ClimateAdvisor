# Climate Advisor — Learning Engine Design

## Purpose

The learning engine is what makes Climate Advisor more than a fancy scheduler. It observes the gap between what was recommended and what actually happened, and uses that gap to suggest improvements. The long-term goal is a system that adapts to the household's real behavior rather than insisting on idealized behavior.

## Core Principle

**If you give people a behavior to perform and they rarely do it, calculate a better alternative that can be performed and ask the user if they'd like to switch.**

## Data Collection

Every day, a `DailyRecord` is saved with:

### What Was Recommended
- Day type and trend
- Whether windows were recommended (and suggested open/close times)
- Recommended HVAC mode

### What Actually Happened
- Whether windows were opened (and actual times)
- Total HVAC runtime in minutes
- Time spent away from home
- Number of door/window pause events
- Number of manual thermostat overrides

### Outcomes
- Average indoor temperature
- Minutes spent outside comfort range (comfort violations)
- Estimated energy cost (future: from utility rate integration)

### Suggestion Tracking
- Was a suggestion sent today?
- User response: accepted, dismissed, or ignored

## Storage

- JSON file in HA config directory (`climate_advisor_learning.json`)
- Rolling 90-day window (older records pruned automatically)
- Persists across HA restarts

## Suggestion Generation

Suggestions are generated when `generate_suggestions()` is called (typically during the daily briefing). Requires minimum 14 days of data.

### Pattern: Low Window Compliance

**Detection:** Of days where windows were recommended, compliance < 30%.
**Suggestion:** "Would you like Climate Advisor to stop suggesting window actions and instead rely on HVAC with optimized schedules?"
**If accepted:** `disable_window_recommendations = True` — the briefing stops suggesting window actions, and the automation engine uses HVAC for those day types instead.
**Trade-off:** Slightly higher energy use, zero human effort required.

### Pattern: Frequent Manual Overrides

**Detection:** More than 10 manual thermostat adjustments in 14 days.
**Suggestion:** "Would you like Climate Advisor to analyze the override patterns and suggest new setpoints?"
**If accepted:** System flags for setpoint analysis (future: actually analyze the override direction and magnitude to suggest specific new setpoints).
**Trade-off:** May result in slightly different comfort baseline.

### Pattern: High Runtime on Mild/Warm Days

**Detection:** Average HVAC runtime > 120 minutes on mild/warm day types.
**Suggestion:** "Would you like to add more door/window sensors, or adjust the setpoints for mild days?"
**Root cause:** Usually indicates doors/windows being left open without sensors, or setpoints that are too aggressive for the house's thermal characteristics.

### Pattern: Short Departures

**Detection:** More than 5 departures of 30–45 minutes in 14 days.
**Suggestion:** "Would you like to shorten the setback delay from 15 minutes to 5 minutes, or skip setback for departures under 1 hour?"
**Insight:** The current 15-minute delay means the setback barely takes effect before the person returns, wasting the recovery energy. Options: faster setback (saves more) or skip setback (saves the recovery penalty).

### Pattern: Comfort Violations

**Detection:** Indoor temp outside comfort range for > 30 minutes on 5+ of the last 14 days.
**Suggestion:** "Would you like to reduce the setback aggressiveness, or start the morning warm-up earlier?"
**If accepted:** Setback modifier increases by 2°F (less aggressive), and/or morning pre-heat starts 15 minutes earlier.

### Pattern: Frequent Door Pauses

**Detection:** More than 20 HVAC pause events from door/window sensors in 14 days.
**Suggestion:** "If a specific door is the main culprit, would you like to extend the pause delay for that door, or exclude it from monitoring?"
**Future enhancement:** Track which specific sensor triggers most pauses and name it in the suggestion.

## Suggestion Lifecycle

1. **Generated:** During daily briefing generation, based on pattern analysis
2. **Delivered:** Appended to the daily briefing email/notification
3. **Response:** User can accept, dismiss, or ignore
   - **Accept:** Config changes are applied, recorded in settings_history
   - **Dismiss:** Suggestion key added to dismissed list, won't reappear for SUGGESTION_COOLDOWN_DAYS (7)
   - **Ignore:** Suggestion reappears next day until acted on or pattern resolves

## Thermal Model

The thermal model characterises how the house envelope and HVAC system move indoor temperature over time. Observations accumulate across HVAC sessions and are used to compute adaptive bedtime setback depth, pre-heat start time, and the physics-based predicted temperature curve shown in the dashboard.

### Physics Model (Issue #114 — v2 Architecture)

The model is a two-parameter first-order ODE:

```
dT/dt = k_passive * (T_indoor - T_outdoor) + Q
```

where:
- `k_passive` (hr⁻¹, always negative) — envelope decay rate; describes how fast the house drifts toward outdoor temperature without HVAC
- `Q` = `k_active` when HVAC is running, 0 when HVAC is off
- `k_active_heat` (°F/hr, positive) — net HVAC heating contribution above envelope exchange
- `k_active_cool` (°F/hr, negative) — net HVAC cooling contribution

**Analytical ODE solution** used for prediction and simulation:

```
T(t+dt) = T_outdoor + (T - T_outdoor) * exp(k_p * dt) + (Q/k_p) * (exp(k_p * dt) - 1)
```

**Why this replaces the scalar rate model:** The old architecture computed a single `end_temp – start_temp` delta at the moment HVAC stopped. Because most thermostats have a 15–30 second sensor update lag after the heating/cooling action ends, `start_temp == end_temp` in almost every observation, yielding `rate = 0°F/hr` and causing the observation to be dropped. The new architecture never takes a single-point delta — it builds the model from the full post-heat decay curve and the active-phase samples together.

### `PendingThermalEvent` State Machine

The coordinator maintains an in-progress observation window as a state machine stored in `LearningState.pending_thermal_event`. The coordinator methods managing it are all on `ClimateAdvisorCoordinator`.

```
idle
  │
  │  hvac_action becomes "heating" or "cooling"
  ▼
active
  │  samples collected every 60s; pre-heat buffer also sampled every 60s (15-min rolling window)
  │
  │  hvac_action leaves "heating"/"cooling"
  ▼
post_heat
  │  samples collected every 60s for up to 45 min (THERMAL_POST_HEAT_TIMEOUT_MINUTES)
  │
  ├──[stabilized: |ΔT| < 0.3°F over 5 consecutive minutes]──► commit
  │
  └──[timeout: 45 min elapsed without stabilization]──► abandon
```

**State machine methods:**

| Method | Role |
|---|---|
| `_start_thermal_event(session_mode)` | Enters `active` state; initialises event dict |
| `_sample_thermal_event()` | Appends a sample during the active phase |
| `_end_active_phase()` | Transitions from `active` to `post_heat` |
| `_check_stabilization()` | Evaluates stabilization criterion; triggers commit or continues |
| `_commit_thermal_event()` | Extracts k_passive/k_active and calls `learning.commit_thermal_event()` |
| `_abandon_thermal_event(reason)` | Discards event; logs WARNING with reason |
| `_update_pre_heat_buffer()` | Maintains the 15-min rolling pre-heat sample buffer |

**Pre-heat buffer:** Sampled every 60 seconds regardless of HVAC state. Holds at most 15 entries (THERMAL_PRE_HEAT_BUFFER_MINUTES). Pre-heat samples are included in the OLS regression for `k_passive` to provide a richer baseline before the heating/cooling run began.

**HVAC mode classification:** The session mode (`heat`, `cool`, `heat_cool`, `fan_only`) is determined by the first `hvac_action` observed during the active phase. `fan_only` sessions contribute only `k_passive` — no `k_active` is extracted.

### `ThermalObservation` Dataclass (v2)

| Field | Type | Purpose |
|---|---|---|
| `timestamp` | `datetime` | UTC time the observation was committed |
| `mode` | `str` | `"heat"`, `"cool"`, `"heat_cool"`, or `"fan_only"` |
| `k_passive` | `float` | Envelope decay rate (hr⁻¹, negative) from OLS regression |
| `k_active` | `float \| None` | HVAC contribution (°F/hr); `None` for `fan_only` |
| `confidence_grade` | `str` | `"high"`, `"medium"`, or `"low"` — governs EWMA alpha |
| `r_squared_passive` | `float` | R² of the post-heat OLS regression |
| `r_squared_active` | `float \| None` | R² of the active-phase regression |
| `n_post_samples` | `int` | Post-heat sample count used in regression |
| `outdoor_temp_f` | `float \| None` | Mean outdoor temperature during the event |

### Parameter Extraction

`k_passive` is estimated from the post-heat decay curve (plus pre-heat buffer) using OLS regression:

```
rate_i = k_passive * delta_i
```

where `rate_i = ΔT/Δt` (°F/hr) and `delta_i = T_indoor - T_outdoor` for each sample pair. Minimum requirements: 10 post-heat samples, R² ≥ 0.2 (`THERMAL_MIN_R_SQUARED`).

`k_active` is extracted from active-phase samples:

```
k_active_i = rate_i - k_passive * delta_i
```

The mean of all per-sample estimates is taken. Out-of-bounds values are rejected using sanity bounds (see §Constants below).

**Confidence grades and EWMA alpha:**

| Grade | k_passive condition | k_active condition | EWMA alpha |
|---|---|---|---|
| `"high"` | R² ≥ 0.7 and ≥ 10 post samples | R² ≥ 0.7 | 0.30 |
| `"medium"` | R² ≥ 0.4 and ≥ 5 post samples | R² ≥ 0.4 | 0.15 |
| `"low"` | R² ≥ 0.2 (minimum) | any passing sanity bounds | 0.05 |
| rejected | R² < 0.2 or < 10 post samples | — | observation discarded |

### `record_thermal_observation(obs: dict) -> None`

Called by `learning.commit_thermal_event()` after parameter extraction. Appends the observation to the rolling history (capped at 90 entries) and updates `thermal_model_cache` via EWMA using the observation's `confidence_grade`. State is saved to disk immediately after each commit — HA restarts mid-day do not lose accumulated observations.

### Predicted Indoor Temperature — Band Schedule Alignment (Issue #119)

`_build_predicted_indoor_future()` in `coordinator.py` accepts a `classification` parameter alongside `thermal_model`. When both are provided:

- The target band schedule is computed once via `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now, setback_modifier, thermal_model, classification)` before iterating forecast hours (pre-computed, not per-hour — Issue #119 B3 fix).
- Sleep temperatures in the band are derived from `compute_bedtime_setback(config, thermal_model, classification)`, the same function used by `automation.py`. Chart band, physics prediction, and automation engine all use identical adaptive sleep setpoints when a model is available.
- Occupancy mode is propagated: away today → setback setpoints for today's hours only; vacation → deep setback for all forecast days.

When `thermal_model` or `classification` is absent, the band falls back to static sleep-temp defaults and the ramp interpolation path is used for the indoor prediction curve.

### `get_thermal_model() -> dict`

Returns the current accumulated thermal model from `thermal_model_cache`.

**Output dict structure:**

```python
{
    # v2 physics parameters
    "k_passive": float | None,              # envelope decay rate (hr⁻¹, negative)
    "k_active_heat": float | None,          # HVAC heating contribution (°F/hr, positive)
    "k_active_cool": float | None,          # HVAC cooling contribution (°F/hr, negative)
    "confidence": str,                      # "none" | "low" | "medium" | "high"
    # legacy compatibility fields
    "heating_rate_f_per_hour": float | None,  # = abs(k_active_heat), rounded to 2dp
    "cooling_rate_f_per_hour": float | None,  # = abs(k_active_cool), rounded to 2dp
    "observation_count_heat": int,
    "observation_count_cool": int,
}
```

`confidence` is derived from the count of heat+cool observations: `"none"` (< 3 of either mode), `"low"` (3–9), `"medium"` (10–19), `"high"` (20+).

The legacy `heating_rate_f_per_hour` and `cooling_rate_f_per_hour` fields are kept for backward compatibility with `compute_bedtime_setback()` and `_compute_ramp_hours()`.

### `LearningState` New Fields

| Field | Type | Description |
|---|---|---|
| `pending_thermal_event` | `dict \| None` | Serialised in-progress event; persisted so a mid-event HA restart can recover the event |
| `thermal_model_cache` | `dict \| None` | EWMA-accumulated `k_passive`, `k_active_heat`, `k_active_cool` |

**Startup recovery:** `recover_pending_event_on_startup()` reads `pending_thermal_event` from state. If the event is in `post_heat` phase, it resumes monitoring. If it is in `active` phase, it is abandoned (cannot resume an active HVAC session across a restart reliably).

### Sanity Bounds (from `const.py`)

| Constant | Value | Meaning |
|---|---|---|
| `THERMAL_K_PASSIVE_MIN` | `-0.5` hr⁻¹ | Minimum envelope decay rate (very leaky) |
| `THERMAL_K_PASSIVE_MAX` | `-0.001` hr⁻¹ | Maximum envelope decay rate (very well insulated) |
| `THERMAL_K_ACTIVE_HEAT_MIN` | `0.5` °F/hr | Minimum HVAC heating contribution |
| `THERMAL_K_ACTIVE_HEAT_MAX` | `15.0` °F/hr | Maximum HVAC heating contribution |
| `THERMAL_K_ACTIVE_COOL_MIN` | `-15.0` °F/hr | Maximum HVAC cooling contribution (magnitude) |
| `THERMAL_K_ACTIVE_COOL_MAX` | `-0.5` °F/hr | Minimum HVAC cooling contribution (magnitude) |

### `get_weather_bias() -> dict`

Analyzes historical records where both forecast and observed temperatures are present to detect systematic forecast error (the weather service consistently running warm or cool).

**Output dict structure:**

```python
{
    "high_bias": float,       # mean forecast high error in °F (positive = forecast runs warm)
    "low_bias": float,        # mean forecast low error in °F (positive = forecast runs warm)
    "confidence": str,        # "none" | "low" | "medium" | "high"
    "sample_count": int,      # number of days used to compute the bias
}
```

**Confidence levels:** same thresholds as `get_thermal_model()` (3/10/20 observations).

### Suggestion Keys for Thermal Learning

| Suggestion Key | Detection | Suggestion Text |
|---|---|---|
| `thermal_model_ready` | `get_thermal_model()` confidence transitions from `"none"` to `"low"` or better for both heat and cool | "Your home's thermal profile is ready. Bedtime setback depth and pre-heat timing are now tuned to your home's actual heating performance." |
| `forecast_bias_significant` | `get_weather_bias()` high or low bias magnitude exceeds 3°F at `"medium"` or higher confidence | "Your weather service appears to run [warm/cool] by about X°F on average. Climate Advisor has adjusted its timing calculations to compensate." |

### Sensor Attributes (Compliance Sensor)

The following attributes are added to `sensor.climate_advisor_comfort_score` when the thermal model is available:

| Attribute | Type | Description |
|---|---|---|
| `thermal_heating_rate` | `float \| None` | `k_active_heat` in user's configured unit per hour (`None` if no data) |
| `thermal_cooling_rate` | `float \| None` | `abs(k_active_cool)` in user's configured unit per hour (`None` if no data) |
| `thermal_confidence` | `str` | Confidence level: `"none"`, `"low"`, `"medium"`, or `"high"` |
| `thermal_observation_count` | `int` | Total heat + cool observations recorded |
| `forecast_high_bias` | `float` | Forecast high bias in user's configured unit (0.0 if no data) |
| `forecast_low_bias` | `float` | Forecast low bias in user's configured unit (0.0 if no data) |
| `forecast_bias_confidence` | `str` | Confidence level for weather bias estimate |

---

## Future Learning Capabilities (v0.3+)

### ~~Thermal Model Learning~~ _(Complete — see Thermal Model section above)_
~~Track how quickly the house heats/cools under different conditions to build a simple thermal model.~~ Implemented in Phase 5 (scalar rate model) and replaced with a two-parameter physics model in Issue #114 (`k_passive` + `k_active`). The thermal model drives adaptive bedtime setback depth, pre-heat start time, and the physics-based predicted temperature curve. Parameters are exposed via the compliance sensor.

### Seasonal Baselines
After a full year of data, establish seasonal baselines for runtime, comfort scores, and energy use. Detect anomalies (e.g., "Your heating runtime this November is 30% higher than last November — possible insulation issue or thermostat drift").

### Override Pattern Analysis
Instead of just counting overrides, analyze their direction and timing:
- "You consistently raise the temperature 2°F around 3pm" → suggest a scheduled bump
- "You lower the temperature every night before your usual bedtime" → suggest moving bedtime setback earlier

### Energy Cost Integration
With utility rate data, convert runtime minutes to estimated cost and show daily/weekly/monthly savings compared to "no automation" baseline.

## Configuration Changes Applied by Suggestions

| Suggestion Key | Config Changes |
|---------------|---------------|
| `low_window_compliance` | `disable_window_recommendations: true` |
| `frequent_overrides` | `request_setpoint_analysis: true` |
| `short_departures` | `occupancy_setback_minutes: 5` |
| `comfort_violations` | `setback_modifier: +2`, `morning_preheat_offset_minutes: 15` |
| `frequent_door_pauses` | `door_pause_seconds: 300` |
| `thermal_model_ready` | No config change — informational notification only |
| `forecast_bias_significant` | No config change — bias is applied internally at runtime |

## Compliance Summary API

The `get_compliance_summary()` method returns a dict suitable for sensor attributes:

```python
{
    "status": "active",            # or "collecting_data"
    "days_recorded": 28,
    "window_compliance": 0.35,     # 35% of recommended days
    "avg_daily_hvac_runtime_minutes": 145.5,
    "comfort_score": 0.92,         # 92% of time in comfort range
    "total_manual_overrides": 8,
    "pending_suggestions": 2,
}
```

### Metric Definitions

#### Comfort Violations (`comfort_violations_minutes`)
**Unit:** minutes
**What it means:** Time during the day when the indoor temperature was outside the configured
comfort range (`comfort_heat`–`comfort_cool` settings, default 70–75°F).
**How it accumulates:** The coordinator checks indoor temperature on every data update.
Each check adds the **actual elapsed time** since the previous check (capped at 30 minutes)
when the temperature is outside the comfort range. This prevents double-counting when the
coordinator is refreshed more frequently than the 30-minute scheduled interval (e.g., after
door/window events or automation revisits).
**Maximum per day:** 1440 minutes (24 hours). Values above 1440 in historical records
indicate data recorded before v0.2.x (fixed-30-min bug) and should be disregarded.

#### Comfort Score (`comfort_score`)
**Formula:** `1 − (sum of daily violation_minutes / (days_recorded × 1440))`
**Range:** 0.0 (always outside range) to 1.0 (always within range), reported as a percentage
**Example:** 3 days, total 2160 violation minutes → `1 − (2160 / 4320)` = 0.50 = 50%
**Sensor:** `sensor.climate_advisor_comfort_score` reports this as a percentage (0–100%)
**Trigger:** More than 5 days with over 30 violation minutes triggers the `comfort_violations`
suggestion, which reduces setback aggressiveness and starts morning pre-heat 15 minutes earlier.
