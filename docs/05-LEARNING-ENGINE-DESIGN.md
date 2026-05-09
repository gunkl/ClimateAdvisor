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

The thermal model characterises how the house envelope and HVAC system move indoor temperature over time. Observations accumulate continuously from six parallel sources — passive temperature drift, fan operation, window ventilation, solar gain, and HVAC heating/cooling cycles — and are used to compute adaptive bedtime setback depth, pre-heat start time, and the physics-based predicted temperature curve shown in the dashboard.

### Physics Model (Issue #114 v2 / Issue #121 v3)

The v3 ODE adds ventilation and solar terms to the v2 two-parameter model:

```
dT/dt = (k_passive + k_vent_eff) * (T_out - T_in) + k_solar * solar_factor + Q_hvac
```

where:
- `k_passive` (hr⁻¹, always negative) — envelope decay rate; how fast the house drifts toward outdoor temperature with no HVAC, no fan, and no solar gain
- `k_vent` (hr⁻¹, negative) — additional decay rate from HVAC fan circulation only
- `k_vent_window` (hr⁻¹, negative) — additional decay rate when windows are open
- `k_solar` (°F/hr per unit solar factor) — solar gain contribution
- `k_vent_eff` = `k_vent` when fan is active, `k_vent_window` when windows are open, 0 otherwise
- `solar_factor` = sinusoidal 0 → 1 → 0 over daylight hours (local 8:00–18:00)
- `Q_hvac` = `k_active_heat` when heating, `k_active_cool` when cooling, 0 otherwise

**Analytical ODE solution** used for prediction and simulation:

```
T(t+dt) = T_outdoor + (T - T_outdoor) * exp(k_p * dt) + (Q/k_p) * (exp(k_p * dt) - 1)
```

**Why OLS regression over single-point delta:** The original scalar model computed `end_temp – start_temp` at the moment HVAC stopped. Thermostat sensor lag (15–30 s) means those two temperatures are often equal, yielding `rate = 0°F/hr` and causing the observation to be dropped. The OLS architecture builds the model from the full post-heat decay curve and the active-phase samples together, eliminating the lag problem.

### v3 Parallel Observation Architecture (Issue #121)

The v2 architecture had a single `PendingThermalEvent` state machine — only one observation could accumulate at a time. v3 replaces this with a `_pending_observations: dict[str, PendingObservation]` dict keyed by observation type string. All six types can run concurrently; each type targets a different thermal parameter.

#### Observation Types

| Type | Trigger conditions | Target parameter | Min samples |
|---|---|---|---|
| `hvac_heat` | `hvac_action = "heating"` | `k_active_heat`, `k_passive` (via pre-heat buffer) | 10 post-heat |
| `hvac_cool` | `hvac_action = "cooling"` | `k_active_cool` | 10 post-heat |
| `passive_decay` | HVAC off, fan off, windows closed, `\|ΔT\|` ≥ 3°F (indoor vs outdoor) | `k_passive` | 30 |
| `fan_only_decay` | Fan active (`_fan_active` or thermostat `fan_only` mode), HVAC off, windows closed | `k_vent` | 15 |
| `ventilated_decay` | Any window/door sensor open, HVAC off | `k_vent_window` | 20 |
| `solar_gain` | HVAC off, fan off, windows closed, `T_in > T_out`, daytime (local 8:00–18:00) | `k_solar` | 20 |

**HVAC contamination rule:** When HVAC starts, all four non-HVAC observations in progress are committed (if sufficient samples) or abandoned. HVAC operation contaminates passive/vent/solar signals.

**HVAC plateau guard:** After the HVAC active phase ends, the observation is committed only if `peak_indoor_f − end_indoor_f ≥ THERMAL_HVAC_MIN_DECAY_F (0.3°F)`. The guard was reduced from 1.0°F in v3 — the old threshold rejected all observations on short-cycling thermostats.

#### Sampling Cadence (Issue #122 H1 — Decimation)

The coordinator polls every 30 seconds. Sampling all slow decay phenomena at poll rate yields noise — the temperature change between two adjacent samples is dominated by sensor quantisation, not the signal. Per-type sample gates enforce a minimum wall-clock interval between recorded samples:

| Type | Sample interval | Rationale |
|---|---|---|
| `hvac_heat` / `hvac_cool` (active phase) | Every poll (no gate) | Active HVAC is fast; all samples needed |
| `hvac_heat` / `hvac_cool` (post-heat phase) | 5 min (`THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S`) | Post-heat decay is passive dynamics |
| `passive_decay` | 5 min (`THERMAL_PASSIVE_SAMPLE_INTERVAL_S`) | Slow drift; 5-min samples give clean signal |
| `fan_only_decay` | 2 min (`THERMAL_FAN_SAMPLE_INTERVAL_S`) | Fan effect faster than passive; 2-min gives adequate resolution |
| `ventilated_decay` | 5 min (`THERMAL_PASSIVE_SAMPLE_INTERVAL_S`) | Window-open drift is slow |
| `solar_gain` | 5 min (`THERMAL_SOLAR_SAMPLE_INTERVAL_S`) | Solar ramp is slow; 5-min gives adequate resolution |

The gate is stored as `"last_sample_time"` in the observation dict. Section A of `_sample_all_observations()` checks `elapsed_since_last >= _interval_s` before appending a sample.

**Convergence improvement:** With 5-min decimation, a 6-hour passive overnight window yields ~72 samples vs. the noise-ridden 720 samples at 30-s poll rate. The 30-sample minimum (`THERMAL_PASSIVE_MIN_SAMPLES`) is calibrated to 5-min intervals, requiring ~2.5 hours of clean signal.

#### Rolling-Window Commits (Issue #122 H2)

Long observation windows are accurate but slow to accumulate. Rolling commits break each long observation into overlapping 30-minute windows. After `THERMAL_ROLLING_WINDOW_MINUTES (30 min)` elapses, the observation is committed and immediately restarted (if conditions still hold on the next poll).

Rolling commit rules (in `_commit_rolling_window_obs()`):
- Minimum 3 samples in the window (absolute floor)
- For `passive_decay` and `solar_gain`: total indoor ΔT ≥ `THERMAL_ROLLING_MIN_DELTA_T_F (0.2°F)` — prevents noise-fitting on near-flat data
- For `fan_only_decay` and `ventilated_decay`: the ΔT guard is skipped (`skip_delta_guard=True`); the signal guarantee for those types is the indoor–outdoor differential (checked by the trigger condition), not the temperature trend

**Convergence impact:** Rolling windows produce ~16 `passive_decay` commits per overnight period (6 hours ÷ 30 min) vs. 1 per full-night commit in v2. The model reaches 5% accuracy in ~4 nights (α = 0.05 EWMA) vs. ~60 nights before.

#### Wall-Clock Abandon Timeout (Issue #122 H4)

`ventilated_decay` and `fan_only_decay` have a 60-minute wall-clock abandon limit (`THERMAL_DECAY_MAX_WINDOW_MINUTES`). If 60 minutes elapse without the rolling-window commit threshold being met and without the signal crossing the minimum ΔT guard, the observation is abandoned with reason `"max_window_elapsed_low_signal"`. This prevents stale, low-signal observations from persisting indefinitely when a window is left open in near-equilibrium conditions.

`passive_decay` and `solar_gain` do not have this wall-clock timeout — they rely on rolling commits to bound window length.

### Observation Lifecycle Methods

| Method | Owner | Role |
|---|---|---|
| `_start_hvac_observation(session_mode)` | `coordinator.py` | Begins `hvac_heat` or `hvac_cool`; abandons/commits non-HVAC obs; attaches pre-heat buffer |
| `_start_decay_observation(obs_type)` | `coordinator.py` | Creates `passive_decay`, `fan_only_decay`, `ventilated_decay`, or `solar_gain` observation dict |
| `_sample_all_observations()` | `coordinator.py` | Section A: samples all active obs with per-type decimation gate. Section B: checks trigger conditions and starts new non-HVAC obs. Section C: evaluates exit conditions and calls commit/abandon per type |
| `_end_hvac_active_phase(obs_type)` | `coordinator.py` | Transitions HVAC obs `active → post_heat` when `hvac_action` leaves heating/cooling |
| `_check_hvac_stabilization(obs_type)` | `coordinator.py` | Evaluates stabilization criterion for HVAC post-heat; applies plateau guard; commits or abandons |
| `_commit_observation(obs_type)` | `coordinator.py` | Passes observation dict to `learning._commit_event_from_dict()`; pops from `_pending_observations` |
| `_commit_observation_if_sufficient(obs_type, abandon_reason)` | `coordinator.py` | Commits if `len(samples) >= min_samples` (with short-window ΔT guard), else abandons |
| `_commit_rolling_window_obs(obs_type, obs)` | `coordinator.py` | Commits a 30-min rolling window slice; pops observation so it can be restarted |
| `_abandon_observation(obs_type, reason)` | `coordinator.py` | Discards pending observation; logs WARNING with type, reason, sample count, and ΔT |
| `_update_thermal_model_cache(obs)` | `learning.py` | Applies committed observation to EWMA cache; routes each mode to the correct parameter |

**Pre-heat buffer:** `_pre_heat_sample_buffer` is sampled every coordinator poll regardless of HVAC state. Holds at most `THERMAL_PRE_HEAT_BUFFER_MINUTES (15)` entries. When `_start_hvac_observation()` fires, these samples are included in the HVAC observation's `pre_heat_samples` list and contribute to the OLS regression for `k_passive`.

### Parameter Extraction

`k_passive` is estimated from OLS regression over post-heat decay samples (plus pre-heat buffer for HVAC obs):

```
rate_i = k_passive * delta_i
```

where `rate_i = ΔT/Δt` (°F/hr) and `delta_i = T_indoor − T_outdoor` for each sample pair. Minimum: 10 post-heat samples, R² ≥ 0.2.

`k_active` is extracted from active-phase HVAC samples:

```
k_active_i = rate_i - k_passive * delta_i
```

Mean of per-sample estimates; out-of-bounds values rejected by sanity bounds.

`k_vent` (fan-only decay) and `k_vent_window` (ventilated decay) use the same OLS formula as `k_passive` on their respective sample sets.

`k_solar` is extracted from solar gain observations as a mean rate adjusted for `solar_factor`.

**Confidence grades and EWMA alpha:**

| Grade | k_passive condition | k_active condition | EWMA alpha |
|---|---|---|---|
| `"high"` | R² ≥ 0.7 and ≥ 10 post samples | R² ≥ 0.7 | 0.30 |
| `"medium"` | R² ≥ 0.4 and ≥ 5 post samples | R² ≥ 0.4 | 0.15 |
| `"low"` | R² ≥ 0.2 (minimum) | any passing sanity bounds | 0.05 |
| rejected | R² < 0.2 or < 10 post samples | — | observation discarded |

Rolling-window commits are always committed with `force_grade="low"` (α = 0.05), reflecting the shorter sample window.

### `_update_thermal_model_cache()` Routing (Issue #122 E6 fix)

Each committed observation updates the EWMA cache in `learning.py`. The routing by `hvac_mode` field:

| `hvac_mode` value | Updates | Counter incremented |
|---|---|---|
| `"heat"` | `k_active_heat` (if `k_active` present), `k_passive` (always if present) | `observation_count_heat` |
| `"cool"` | `k_active_cool` (if `k_active` present), `k_passive` (always if present) | `observation_count_cool` |
| `"passive"` | `k_passive` only (no `k_vent` update) | `observation_count_passive` |
| `"fan_only"` | `k_vent` (from `k_passive` field of the obs) | `observation_count_fan_only` |
| `"ventilated"` | `k_vent_window` (from `k_passive` field of the obs) | `observation_count_vent` |
| `"solar"` | `k_solar` (from `k_solar` field of the obs) | `observation_count_solar` |

**E6 fix (Issue #122):** The `passive` branch no longer writes `k_p` to `cache["k_vent"]`. Before this fix, `passive_decay` observations were incorrectly populating the ventilation parameter. Now only `fan_only` observations update `k_vent`.

### Adaptive 2-Param Ventilated OLS (Issue #126)

#### Design

`ventilated_decay` observations were originally 1-param: OLS solves for `k_vent_window`
only, treating solar gain as noise. On sunny mornings with windows open, uncontrolled
solar gain inflates `dT/dt`, biasing `k_vent_window` toward a larger (more negative)
value than the true ventilated decay rate. The 2-param path removes this bias by jointly
solving for both `k_env_vent` (the true ventilated envelope decay rate) and `k_solar`
(the solar contribution) when the observation window spans enough solar variation to
distinguish the two effects.

The upgrade is adaptive — it fires at commit time when the solar factor range across the
observation's samples meets a minimum threshold. No new observation type, no new trigger
condition, no new lifecycle complexity.

#### Decision Points (D1–D7, Issue #126)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | New obs type vs. upgrade ventilated_decay | Upgrade existing | Ventilated windows are a single physical event; two concurrent obs types starting/stopping on the same event would complicate the lifecycle with no benefit |
| D2 | Record solar_factor at collection time vs. compute at commit | Collect at sample time | Ensures samples reflect actual solar conditions during collection; commit-time computation would use a different clock position |
| D3 | Hard day/night trigger gate (only run 2-param after 8 AM) | No gate | sf_range check is self-gating: nighttime-only windows have sf=0 throughout → range=0 → 1-param fallback fires automatically |
| D4 | Clock-based solar_factor vs. cloud-aware/lag-corrected | Clock-based accepted | Adequate as approximation; lag averages out over hour-long prediction periods; EWMA smoothing attenuates single-obs error; cloud-aware model deferred |
| D5 | Self-healing via conf_passive guard vs. data wipe | Guard (non-destructive) | Wipe loses accumulated k_vent_window and solar history; guard promotes k_vent_window to proxy without touching stored data |
| D6 | 2-param result writes k_solar only vs. also k_vent_window | Both updated | k_env_vent is a cleaner estimate of ventilated decay (solar contamination removed); discarding it in favour of the 1-param result would lose information |
| D7 | k_solar from ventilated 2-param usable in all prediction paths | Yes | k_solar is a model parameter, not obs-type-specific; the ODE applies it whenever solar conditions and window state warrant |

#### Gate Bridge Self-Healing (Issue #126 Phase A)

The gate bridge handles homes where only ventilated observations have accumulated (no
passive decay, no HVAC cycles). Without the bridge, those homes would never get physics
prediction because `confidence_k_passive == "none"` blocks `_physics_eligible()`.

**Three install states:**

1. **Fresh:** `k_passive = None`, `k_vent_window = None` → bridge has nothing to promote;
   ramp interpolation used. Correct behaviour.
2. **Contaminated** (pre-Issue #126 installs with the original bug): `k_passive = None`,
   `confidence = "none"`, `k_vent_window` has a valid value → bridge detects
   `_conf_k_passive == "none"` (Bug A fix) and promotes `k_vent_window` as proxy
   k_passive. `_k_passive_via_bridge = True` flag bypasses the `_physics_eligible()`
   confidence guard (Bug B fix). Physics prediction activates automatically on next
   coordinator update — no user action, no data wipe.
3. **Healed / normal:** k_passive is set (either via bridge promotion or real HVAC/passive
   obs) → bridge does not fire; normal physics path.

**H1 confirmed in production:** Logs from the affected install show
`"using physics model (conf=none conf_k_passive=low k_passive=-0.2813)"` after the
bridge runs — confirming that `conf=none` does not block physics when the bridge flag is
set.

#### Future Scope (Deferred)

- **Cloud-aware solar_factor:** Replace the clock-based sinusoidal approximation with a
  forecast-derived solar irradiance estimate (e.g., from a weather entity that exposes
  cloud cover). Reduces thermal mass lag error on partly-cloudy days.
- **`k_vent_delta` derived metric:** `k_vent_window − k_passive` quantifies the
  incremental ventilation benefit above bare envelope decay. Useful for advising users on
  whether opening windows meaningfully accelerates cooling.

### HVAC Observation Commit Path (Issue #130)

Issue #130 fixed zero `k_active_heat` / `k_active_cool` observations despite 60 days of
heat cycles. The root causes and their fixes determine how HVAC observations now commit.

#### Root Causes Confirmed

| # | Root Cause | Fix |
|---|---|---|
| RC1 | Post-heat OLS required 10 samples (50 min) — systematic timeout on 5–30 min cycles | D14: lower `THERMAL_MIN_POST_HEAT_SAMPLES` 10 → 4 |
| RC2 | Most cycles never produced a logged rejection — observation start was invisible | Phase A: INFO log at observation start, abandon, and commit |
| RC3 | `outdoor=None` at HVAC state transitions blocked post-heat sample collection | D16: `_last_known_outdoor_f` 30-min fallback |
| RC4 | Bridge homes: `k_passive=None` prevented `compute_k_active()` from running | D17: use `k_vent_window` as proxy k_passive in hvac commit path |
| RC5 | No backfill mechanism for historical heat cycles | D18: `thermal_replay --hvac` mode |

#### Decision Points (D14–D26, Issue #130)

| D# | Decision | Rationale |
|----|----------|-----------|
| D14 | `THERMAL_MIN_POST_HEAT_SAMPLES`: 10 → 4 | Aligns with rolling obs min; enables short-cycle commits |
| D15 | Remove stabilization variance gate from `_check_hvac_stabilization()` | OLS R² is the quality gate; waiting for ±0.3°F stability over 5 min was redundant and blocked all short cycles |
| D16 | 30-min outdoor temp fallback (`_last_known_outdoor_f`) | `outdoor=None` at HVAC state transitions was blocking post-heat sample collection |
| D17 | Use `k_vent_window` as k_passive proxy in hvac commit path | Bridge homes: enables `k_active` commit without `k_passive` from passive_decay or HVAC obs |
| D18 | `thermal_replay --hvac` mode (`run_hvac_replay_ols()`) | Backfill historical heat/cool cycles from chart_log |
| D19 | Single-point `k_active` estimator when `n_active < 2` | Physics valid with 1 active sample if timestamps give true elapsed |
| D20 | Use `post[0].ts` as cycle end timestamp when `n_act=1` | State-change entry gives exact HVAC-off time |
| D21 | Emit `k_passive=None` in obs dict when bridge proxy was used | Prevents proxy value from contaminating k_passive EWMA |
| D22 | Load `k_vent_window` proxy in dry-run mode for `thermal_replay` | Proxy needed for single-point path regardless of write mode |
| D23 | `THERMAL_HVAC_MIN_SIGNAL_F = 0.5°F` | Minimum ΔT threshold; filters noise and setpoint-maintenance cycles where heater ran but temperature barely moved |
| D24 | `n_post` minimum drops 4 → 1 when proxy available | Post-heat OLS is not needed when proxy provides k_passive; single-point only needs `post[0]` for the HVAC-off timestamp |
| D25 | Plateau guard bypassed when proxy available | The plateau guard validates OLS decay quality; irrelevant for the single-point path |
| D26 | Proxy check via `model_cache.k_vent_window` in `coordinator.py` | Consistent with `learning.py` proxy detection pattern |

#### Single-Point `k_active` Estimator (`compute_k_active_single_point()`)

When `n_active < 2` (the HVAC cycle is shorter than the 5-min sampling interval so only
one active-phase data point was recorded), OLS cannot produce a heating rate. The
single-point estimator uses exact HVAC on/off timestamps from state-change chart_log
entries to compute `k_active` directly from physics:

```
k_active = (T_peak − T_start) / elapsed_hours − k_passive × avg(T_in − T_out)
```

where:
- `T_start` = indoor temperature at HVAC start
- `T_peak` = peak indoor temperature during or after the cycle
- `elapsed_hours` = true HVAC-on duration from timestamps (not sample interval)
- `k_passive` = envelope decay rate or proxy (from `k_vent_window` on bridge homes)
- `avg(T_in − T_out)` = mean indoor-outdoor differential across available samples

The estimator is called from `_commit_event_from_dict()` in `learning.py` when OLS
returns `None` (insufficient active samples). It is also used by
`run_hvac_replay_ols()` in `tools/thermal_replay.py` with the same fallthrough logic.

**Signal guard:** `THERMAL_HVAC_MIN_SIGNAL_F = 0.5°F` — if `|T_peak − T_start| < 0.5°F`
the cycle is treated as a setpoint-maintenance run (heater ran briefly but temperature
barely moved at chart_log 1°F resolution). The observation is rejected to avoid
fitting noise.

**Implementation:** `learning.py` ~line 401. Fallthrough in `_commit_event_from_dict()`
~line 1145.

#### Proxy-Aware `n_post` Gating

`_check_hvac_stabilization()` in `coordinator.py` applies different commit thresholds
depending on whether a `k_vent_window` proxy is available in the model cache:

| Condition | `n_post` minimum | Plateau guard |
|---|---|---|
| No proxy (normal home, fresh install) | `THERMAL_MIN_POST_HEAT_SAMPLES` (4) | Active: rejects if `peak − end < THERMAL_HVAC_MIN_DECAY_F` |
| Proxy available (`k_vent_window < 0`) | 1 | Bypassed |

When the proxy is available, `post[0]` provides the HVAC-off timestamp for the
single-point estimator — no OLS decay curve is needed. The same proxy-aware logic
applies in `run_hvac_replay_ols()` in `thermal_replay.py`.

**Implementation:** `coordinator.py` ~line 2951–2979.

#### Phase A Diagnostic Logging

Before Phase A, most HVAC observation starts and abandons were invisible — the rejection
log showed only 1 entry across 38 historical cycles. Phase A adds INFO-level log lines at:

- Observation start (`_start_hvac_observation()`)
- Observation abandon (`_abandon_observation()`) — already existed at WARNING; downgraded
  to INFO in Issue #124; Phase A confirms coverage
- Observation commit (`_commit_event_from_dict()`)

This makes the observation pipeline auditable from HA logs without SSH access to the
learning DB.

#### Backfill Results (Phase D)

After Phases A–D, `thermal_replay --hvac --days 60` committed 24/47 detected cycles (51%).
The 23 remaining rejections are data-quality limits of chart_log 1°F resolution:
- 16 signal=0 rejections: setpoint-maintenance cycles where `T_start = T_peak` at 1°F resolution
- 5 data artifacts: sub-2-minute elapsed times from timestamp coincidences or thermostat glitches
- 2 borderline: k_active estimates just above the 15.0 °F/hr sanity bound

The live coordinator reads `sensor.santa_maria_hallway_current_temperature` at 0.18°F
resolution and samples at the exact HVAC state-change moment — expected to substantially
exceed the 51% chart_log acceptance rate.

### `record_thermal_observation(obs: dict) -> None`

Called by `learning._commit_event_from_dict()` after parameter extraction. Appends the observation to the rolling history (capped at 90 entries) and calls `_update_thermal_model_cache()`. State is saved to disk immediately after each commit — HA restarts mid-day do not lose accumulated observations.

### Predicted Indoor Temperature — Band Schedule Alignment (Issue #119)

`_build_predicted_indoor_future()` in `coordinator.py` accepts a `classification` parameter alongside `thermal_model`. When both are provided:

- The target band schedule is computed once via `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now, setback_modifier, thermal_model, classification)` before iterating forecast hours (pre-computed, not per-hour — Issue #119 B3 fix).
- Sleep temperatures in the band are derived from `compute_bedtime_setback(config, thermal_model, classification)`, the same function used by `automation.py`. Chart band, physics prediction, and automation engine all use identical adaptive sleep setpoints when a model is available.
- Occupancy mode is propagated: away today → setback setpoints for today's hours only; vacation → deep setback for all forecast days.

When `thermal_model` or `classification` is absent, the band falls back to static sleep-temp defaults and the ramp interpolation path is used for the indoor prediction curve.

### `get_thermal_model() -> dict`

Returns the current accumulated thermal model from `thermal_model_cache`.

**Output dict structure (v3):**

```python
{
    # Core physics parameters
    "k_passive": float | None,              # envelope decay rate (hr⁻¹, negative)
    "k_active_heat": float | None,          # HVAC heating contribution (°F/hr, positive)
    "k_active_cool": float | None,          # HVAC cooling contribution (°F/hr, negative)
    "k_vent": float | None,                 # fan-only ventilation decay rate (hr⁻¹, negative)
    "k_vent_window": float | None,          # window-open ventilation decay rate (hr⁻¹, negative)
    "k_solar": float | None,               # solar gain coefficient (°F/hr per unit solar factor)
    # Confidence
    "confidence": str,                      # HVAC confidence: "none"|"low"|"medium"|"high" (heat+cool obs count)
    "confidence_k_passive": str,            # passive confidence: "none"|"low"|"medium"|"high" (passive obs count)
    "confidence_k_hvac": str,              # same as "confidence" — explicit alias
    # Observation counts
    "observation_count_heat": int,
    "observation_count_cool": int,
    "observation_count_total": int,
    "observation_count_passive": int,
    "observation_count_fan_only": int,
    "observation_count_vent": int,
    "observation_count_solar": int,
    # Legacy compatibility fields
    "heating_rate_f_per_hour": float | None,  # = abs(k_active_heat), rounded to 2dp
    "cooling_rate_f_per_hour": float | None,  # = abs(k_active_cool), rounded to 2dp
    # Diagnostics
    "avg_r_squared_passive": float | None,
    "last_observation_date": str | None,
}
```

`confidence` (HVAC) is graded by `observation_count_heat + observation_count_cool`: `"none"` (< 3), `"low"` (3–9), `"medium"` (10–19), `"high"` (20+).

`confidence_k_passive` is graded independently by `observation_count_passive`: same thresholds. Physics prediction activates when either confidence is > `"none"` — enabling prediction on homes with zero HVAC cycles recorded (passive-only households).

The legacy `heating_rate_f_per_hour` and `cooling_rate_f_per_hour` fields are kept for backward compatibility with `compute_bedtime_setback()` and `_compute_ramp_hours()`.

### `LearningState` Fields

| Field | Type | Description |
|---|---|---|
| `pending_thermal_event` | `dict \| None` | Serialised in-progress HVAC event (legacy compat field; v3 also uses `coordinator._pending_observations` dict) |
| `thermal_model_cache` | `dict \| None` | EWMA-accumulated model parameters for all six observation types |

**Startup recovery:** On startup, the coordinator reads `pending_thermal_event` from `LearningState`. If an HVAC observation is in `post_heat` phase, it is restored to `_pending_observations` and monitoring continues. Active-phase HVAC observations are abandoned (cannot reliably resume an active HVAC session across a restart). Non-HVAC observations are not persisted and are simply restarted on the next poll if conditions hold.

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
