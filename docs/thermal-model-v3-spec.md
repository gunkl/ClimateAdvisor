<!-- Nav: ← [05-LEARNING-ENGINE-DESIGN.md](05-LEARNING-ENGINE-DESIGN.md) | → [learning.py](../custom_components/climate_advisor/learning.py) + [coordinator.py](../custom_components/climate_advisor/coordinator.py) | ↔ [08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md) -->

# Thermal Model v3 — Territory Spec (Tier 3)

## Anchors

| Question | Short answer (≤2 sentences) | → Full answer |
|---|---|---|
| What triggers a `passive_decay` observation and what does it measure? | Starts when HVAC off, fan off, windows closed, and `\|T_indoor − T_outdoor\| ≥ 3°F`; measures `k_passive` (envelope decay rate, hr⁻¹, always negative). Requires 30 samples minimum (long-window path) or 5 samples (rolling-window path). | [§Observation Types](#observation-types) |
| When does the gate bridge activate and what does it do? | Activates when `k_passive is None OR confidence_k_passive == "none"` AND `k_vent_window` is not None and `≤ 0`. Promotes `k_vent_window` as a proxy `k_passive` so physics prediction can run on bridge-only homes without a data wipe. | [§Gate Bridge](#gate-bridge) |
| What are the three rolling-window thresholds and what happens at each? | `THERMAL_ROLLING_MIN_WINDOW_MINUTES` (30 min): earliest commit attempt; `THERMAL_ROLLING_MAX_WINDOW_MINUTES` (240 min): hard cap — commit or abandon regardless of signal; signal check (`THERMAL_ROLLING_MIN_DELTA_T_F = 0.2°F`): required indoor ΔT to commit between the two limits. | [§Rolling Window Constraints](#rolling-window-constraints) |
| How does `compute_k_passive` reject an observation and what codes does it emit? | Five rejection codes: `REJECT_TOO_FEW_SAMPLES`, `REJECT_SMALL_DELTA` (Σδ² = 0), `REJECT_OLS_WRONG_SIGN` (k > 0), `REJECT_OLS_BOUNDS` (k outside [-0.5, -0.001]), `REJECT_OLS_BAD_FIT` (R² < 0.20). Exactly one of `k_passive` or `rejection_code` is non-None in the return tuple. | [§OLS Functions — compute\_k\_passive](#compute_k_passive) |
| What invariant must hold for every committed observation's `k_passive`? | `k_passive < 0` for all non-bridge envelope modes; `k_solar ≥ 0` for all committed solar observations. The bridge proxy (`k_vent_window`) may equal 0.0 exactly (perfectly inert home) but never > 0. | [§Invariants](#invariants) |
| How does `ventilated_decay` commit path choose between 1-param and 2-param OLS? | When solar factor range across samples ≥ 0.30, the 2-param path fires first and — if bounds pass — commits both `k_env` and `k_solar`, bypassing 1-param entirely. If 2-param fails bounds/R², the 1-param path runs as fallback. | [§OLS Functions — compute\_k\_env\_solar](#compute_k_env_solar) |
| What is the EWMA alpha for each confidence grade and which observation types write `k_passive`? | Alpha: high = 0.30, medium = 0.15, low = 0.05. Only `passive`, `heat`, and `cool` modes write `k_passive`; `fan_only` writes `k_vent`; `ventilated` writes `k_vent_window`; `solar` writes `k_solar`. | [§EWMA Update](#ewma-update-_update_thermal_model_cache) |
| How does the dual-estimator framework select between endpoint and block-averaged OLS per overnight window? | Both estimators always run; an 8-row decision table selects based on R²_B and 30% relative agreement. On disagreement, Estimator A (endpoint) wins. On R²_B ≥ 0.50 and agreement, B wins with medium grade (α=0.15). | [§Dual Estimator Framework](#dual-estimator-framework) |
| Where is the solar factor formula defined and what does `phase_offset_h` do? | `_solar_factor(local_hour, phase_offset_h)` shifts the sinusoidal solar input peak by `phase_offset_h` hours. With the default offset=2 the peak falls at local hour 15 (3pm) instead of 13 (1pm). | [§Solar Factor](#solar-factor) |
| How is `solar_phase_offset_h` learned from chart_log? | Daytime passive windows (HVAC off, fan off, windows closed) are scanned for the indoor temperature peak hour. `phase_obs = peak_hour − 13` is accumulated via EWMA (α=0.10), clamped to [0, 4]. | [§Solar Phase Offset Learning](#solar-phase-offset-learning) |
| What is engine visibility and where is it exposed? | `get_engine_status()` returns per-engine `active`, `value`, and (for `k_passive`/`k_solar`) `confidence` fields. Exposed at REST `/api/climate_advisor/engines`, dashboard Debug tab, AI investigator context, and `tools/engine_status.py`. | [§Engine Visibility](#engine-visibility) |
| Why does `k_active_cool` stay `None` despite AC cycling normally? | Two v0.3.50 bugs can cause this: (1) the `"samples": []` key shadow — `_start_hvac_observation` created a `"samples"` key that was always returned instead of `"active_samples"`; (2) startup recovery discarded all HVAC pending obs by reading the wrong key. Both fixed in v0.3.50. Check `--rejections --type hvac_cool` for `n=0` entries with elapsed > 0. | [§Observation Pipeline Failure Modes](#observation-pipeline-failure-modes) |
| What rejection codes indicate normal operation vs. pipeline failure for HVAC obs types? | `new_session_started` (short-cycling), `plateau_guard` (insufficient post-heat decay), and `n=0 delta_t=0.00°F` (sensor quantization) are expected on some homes. `n=0` with elapsed > 0 in pre-v0.3.50 coordinators indicates the key-shadow bug. | [§Known Rejection Patterns](#known-rejection-patterns) |

---

## Scope

**Files:**
- `learning.py` — OLS functions (`compute_k_passive`, `compute_k_passive_blocks`, `compute_k_env_solar`, `compute_k_active`, `compute_k_active_single_point`), commit routing (`_commit_event_from_dict`), EWMA update (`_update_thermal_model_cache`), model output (`get_thermal_model`, `record_thermal_observation`), solar phase learning (`update_solar_phase_offset`), engine visibility (`get_engine_status`)
- `coordinator.py` — observation orchestration (`_sample_all_observations`, `_start_hvac_observation`, `_start_decay_observation`, `_end_hvac_active_phase`, `_check_hvac_stabilization`, `_evaluate_rolling_window`, `_commit_rolling_window_obs`, `_commit_observation_if_sufficient`, `_abandon_observation`, `_commit_observation`), ODE prediction (`_build_predicted_indoor_future`, `_simulate_indoor_physics`, `_simulate_indoor_physics_v3`), dual-estimator chart_log fit (`_is_solar_hour`, `_select_estimator`, `_extract_passive_windows`, `_passive_endpoint_estimate`, `_run_passive_chart_log_fit`, `_extract_ventilated_windows`, `_ventilated_endpoint_estimate`, `_run_ventilated_chart_log_fit`), solar factor (`_solar_factor`), solar phase offset learning (`_estimate_solar_phase_offset`, `_run_solar_phase_chart_log_fit`)

**Line ranges (verified against source):**

| Function | File | Start line |
|---|---|---|
| `compute_k_passive` | learning.py | 187 |
| `compute_k_passive_blocks` | learning.py | 285 |
| `compute_k_env_solar` | learning.py | 370 |
| `compute_k_active` | learning.py | 446 |
| `compute_k_active_single_point` | learning.py | 505 |
| `record_thermal_observation` | learning.py | 734 |
| `_update_thermal_model_cache` | learning.py | 754 |
| `get_thermal_model` | learning.py | 1028 |
| `_commit_event_from_dict` | learning.py | 1127 |
| `update_solar_phase_offset` | learning.py | 902 |
| `get_engine_status` | learning.py | 959 |
| `_start_hvac_observation` | coordinator.py | 2429 |
| `_sample_all_observations` | coordinator.py | 2527 |
| `_check_hvac_stabilization` | coordinator.py | 3468 |
| `_evaluate_rolling_window` | coordinator.py | 3777 |
| `_commit_rolling_window_obs` | coordinator.py | 3848 |
| `_run_solar_phase_chart_log_fit` | coordinator.py | 3311 |
| `_simulate_indoor_physics` | coordinator.py | 4595 |
| `_simulate_indoor_physics_v3` | coordinator.py | 4723 |
| `_build_predicted_indoor_future` | coordinator.py | 4905 |
| `_solar_factor` | coordinator.py | 4640 |
| `_estimate_solar_phase_offset` | coordinator.py | 4661 |

**Out of scope for this spec:** suggestion generation, weather bias, daily record lifecycle, automation engine, briefing text.

---

## Observation Types

All six types run concurrently in `_pending_observations: dict[str, PendingObservation]`. The dict is keyed by the `OBS_TYPE_*` string constant.

| obs_type | Trigger conditions | Target parameter(s) | Min samples (long-window path) | Can run concurrently with? |
|---|---|---|---|---|
| `hvac_heat` | `hvac_action = "heating"` transitions to active | `k_active_heat`, `k_passive` (via pre-heat buffer) | 4 post-heat samples (`THERMAL_MIN_POST_HEAT_SAMPLES`) | Nothing HVAC; all passive types commit/abandon on HVAC start |
| `hvac_cool` | `hvac_action = "cooling"` transitions to active | `k_active_cool`, `k_passive` | 4 post-heat samples | Same as hvac_heat |
| `passive_decay` | HVAC off, fan off, windows closed, `\|T_indoor − T_outdoor\| ≥ THERMAL_PASSIVE_MIN_DELTA_F (3.0°F)` | `k_passive` | 30 (`THERMAL_PASSIVE_MIN_SAMPLES`) | `fan_only_decay`, `ventilated_decay`, `solar_gain` — but mutual exclusion via trigger conditions prevents concurrent passive + fan or passive + vent |
| `fan_only_decay` | Fan active (`_fan_active` or thermostat `fan_only` state), HVAC off, no open sensors | `k_vent` | 15 (`THERMAL_FAN_MIN_SAMPLES`) | `solar_gain` is not started while fan is active |
| `ventilated_decay` | Any door/window sensor open, HVAC off, `\|T_indoor − T_outdoor\| ≥ THERMAL_VENTILATED_MIN_DELTA_F (1.0°F)` | `k_vent_window` (and optionally `k_solar` via 2-param path) | 20 (`THERMAL_VENT_MIN_SAMPLES`) | Can coexist with anything not HVAC; but fan trigger and sensor-open trigger are mutually exclusive |
| `solar_gain` | HVAC off, fan off, windows closed, `T_indoor > T_outdoor`, daytime (local 08:00–18:00, `THERMAL_SOLAR_DAYTIME_START_H` / `END_H`) | `k_solar` | 20 (`THERMAL_SOLAR_MIN_SAMPLES`) | `passive_decay` may also be running (different parameter) |

**HVAC contamination rule:** When HVAC starts (`_start_hvac_observation()`), all four non-HVAC types in `_pending_observations` are committed via `_commit_observation_if_sufficient()` (which commits if `len(samples) >= min_samples`, else abandons). The contamination check fires before the new HVAC observation is started.

---

## Observation Lifecycle

### State Machine

```
(start trigger fires)
        ↓
   status = "monitoring"
        ↓
   samples accumulate via _sample_all_observations()
        ↓
   ┌─── commit condition ────┐    ┌─── abandon condition ───┐
   │ status → "committing"   │    │ popped from dict         │
   │ _commit_observation()   │    │ rejection_log appended   │
   │ → COMMITTED             │    │ → ABANDONED              │
   └─────────────────────────┘    └──────────────────────────┘
```

Terminal states: committed (observation recorded in `thermal_observations`) or abandoned (rejection event logged in `rejection_log`). No re-entry once a type is removed from `_pending_observations`.

### Rolling Decay Types (passive_decay, fan_only_decay, ventilated_decay, solar_gain)

**Start:** `_start_decay_observation(obs_type)` fires from Section B of `_sample_all_observations()` when trigger conditions are first met. Creates the observation dict with `status="monitoring"`, empty `samples`, and `flags_at_start`.

**Sample accumulation:** Section A of `_sample_all_observations()` iterates all monitoring observations. Each type has a per-type decimation gate:

| Type | Gate constant | Interval |
|---|---|---|
| `passive_decay` | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | 5 min |
| `fan_only_decay` | `THERMAL_FAN_SAMPLE_INTERVAL_S` | 2 min |
| `ventilated_decay` | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | 5 min |
| `solar_gain` | `THERMAL_SOLAR_SAMPLE_INTERVAL_S` | 5 min |

A sample is appended only when `elapsed_since_last_sample >= interval_s`. Each sample is a dict with `timestamp`, `indoor_temp_f`, `outdoor_temp_f`, `elapsed_minutes`. For `ventilated_decay`, `solar_factor` (from `_solar_factor(now.hour)`) is also recorded at collection time. The hard cap per observation is `THERMAL_MAX_OBS_SAMPLES = 200`.

**Commit decision (`_evaluate_rolling_window`):** Called from Section C for each rolling type. Two-threshold logic:

1. `elapsed < THERMAL_ROLLING_MIN_WINDOW_MINUTES (30)` AND signal not sufficient → keep collecting (return `False`)
2. `elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES` AND `signal_sufficient=True` → commit via `_commit_rolling_window_obs()`
3. `elapsed >= THERMAL_ROLLING_MAX_WINDOW_MINUTES (240)` → commit if `len(samples) >= THERMAL_MIN_DECAY_SAMPLES + 1 (= 5)`, else abandon with reason `"max_window_exceeded"`
4. Between min and max, signal not sufficient → log and keep alive (samples trimmed to last 96 if > 96)

**Solar keep-alive guard (ventilated_decay only):** During daytime hours (08:00–18:00), if `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`, `_vent_signal_sufficient` is forced to `False`, suppressing early commit even after the 30-min minimum. This prevents a 1-param commit before the 2-param OLS can distinguish `k_env` from `k_solar`. The 240-min hard cap overrides the guard.

**Hard cap behavior:** When `elapsed >= THERMAL_ROLLING_MAX_WINDOW_MINUTES`, `_evaluate_rolling_window` commits unconditionally (with `skip_delta_guard=True`) if `len(samples) >= 5`, otherwise abandons with `reason_code="max_window_exceeded"`.

### HVAC Types (hvac_heat, hvac_cool)

**Start:** `_start_hvac_observation(session_mode)` fires when `hvac_action` changes to `"heating"` or `"cooling"`. Creates the observation with `_phase="active"`, `active_samples=[]`, `pre_heat_samples` from the `_pre_heat_sample_buffer`, and `status="monitoring"`.

**Sample accumulation:** Active phase samples every coordinator poll (no decimation gate). Post-heat phase uses `THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S` (5 min). Post-heat samples go into `post_heat_samples`.

**Active → post_heat transition:** `_end_hvac_active_phase(obs_type)` fires when `hvac_action` leaves `"heating"`/`"cooling"`. Before transitioning:
1. Appends a final active sample at the exact HVAC-off moment (so swing uses the true shutoff temperature, not the most-recent 30-min poll value).
2. Updates `peak_indoor_f` to `max(prior_peak, final_indoor_temp)` — never lowers the peak.

Then sets `_phase="post_heat"`, records `active_end`, computes `session_minutes`.

**Commit decision (`_check_hvac_stabilization`):** Minimum post-heat samples before commit attempt:
- No proxy: `THERMAL_MIN_POST_HEAT_SAMPLES = 4`
- Proxy available (`k_vent_window < 0` in cache): 1 sample

Plateau guard (non-proxy path only): requires `peak_indoor_f − end_indoor_f >= THERMAL_HVAC_MIN_DECAY_F (0.3°F)`. If not met, abandons with reason `"plateau guard: insufficient post-heat decay"`.

When commit conditions are met, `obs["status"] = "stabilized"` is set and `_commit_observation(obs_type)` is called.

Post-heat timeout: if `elapsed_post > THERMAL_POST_HEAT_TIMEOUT_MINUTES (45)`, the observation is abandoned.

---

## OLS Functions

### compute_k_passive()

**Signature:** `compute_k_passive(post_samples, pre_samples=None, min_samples=None) -> tuple[float | None, float, str | None]`

**Input:** `post_samples` — list of sample dicts (mandatory). `pre_samples` — optional pre-heat buffer samples processed as a separate window to avoid spurious rates at the pre→post boundary. `min_samples` — overrides the OLS floor (default: `THERMAL_MIN_POST_HEAT_SAMPLES = 4`; pass `THERMAL_MIN_DECAY_SAMPLES = 4` for rolling-window decay types).

**Pre-condition:** `total_samples = len(post_samples) + len(pre_samples) >= min_samples + 1`

**Computation:** 1-param OLS forced through origin:
```
k = Σ(rate_i × delta_i) / Σ(delta_i²)
```
where for each consecutive sample pair `(i, i+1)`:
- `rate_i = (T_indoor[i+1] − T_indoor[i]) / dt_hours`
- `delta_i = midpoint(T_indoor) − midpoint(T_outdoor)` over the interval
- `T_indoor` values are first passed through a 3-sample centered moving average (`_smooth_temps`) to reduce 1°F quantisation noise; edge samples are unchanged

Each pre/post window is processed independently. Pairs are built within each window only (no cross-boundary pairs).

**R² formula (forced-through-origin model):**
```
R² = 1 − SS_res / SS_tot
where SS_res = Σ(rate_i − k × delta_i)²
      SS_tot = Σ(rate_i²)
```
R² is clamped to [0.0, ∞).

**Rejection codes (in evaluation order):**

| Code | Condition |
|---|---|
| `REJECT_TOO_FEW_SAMPLES` | `len(rates) < min_samples` after building pairs |
| `REJECT_SMALL_DELTA` | `Σ(delta_i²) == 0` (all indoor/outdoor differentials are zero) |
| `REJECT_OLS_WRONG_SIGN` | `k > 0` (physics requires negative k for passive cooling toward outdoor) |
| `REJECT_OLS_BOUNDS` | `k` outside `[THERMAL_K_PASSIVE_MIN (-0.5), THERMAL_K_PASSIVE_MAX (-0.001)]` |
| `REJECT_OLS_BAD_FIT` | `R² < THERMAL_MIN_R_SQUARED (0.20)` |

**Post-condition:** Returns `(k_passive, r_squared, rejection_code)`. Exactly one of `k_passive` or `rejection_code` is non-None. On success: `k_passive < 0`, `rejection_code = None`. On failure: `k_passive = None`, `rejection_code` is one of the `REJECT_*` constants, `r_squared` is the computed value (may be 0.0 if OLS never ran).

---

### compute_k_passive_blocks()

**Signature:** `compute_k_passive_blocks(window_entries, block_minutes=60, min_blocks=6) -> tuple[float | None, float, str | None]`

**Input:** `window_entries` — list of chart_log entry dicts with fields `ts` (ISO-8601 string), `indoor` (float, °F), `outdoor` (float, °F). These are raw chart_log entries at ~30-minute cadence, not 5-min thermostat samples.

**Purpose:** Block-averaged OLS for k_passive using the nightly chart_log window. Averaging 30-min entries into 60-min blocks reduces quantization noise via CLT: from ±1°F raw to ±0.71°F per block (√(1/2) × 1°F). This produces R² of 0.5–0.8 on clean nights vs. ≈0.02 for raw 5-min consecutive-pair OLS.

**Algorithm:**
1. Compute elapsed minutes per entry from the first entry's `ts`.
2. Group entries into 60-min blocks by `floor(elapsed_min / block_minutes)`. Skip any block with fewer than 2 entries.
3. Per usable block: compute mean `indoor`, mean `outdoor`, mean elapsed_min.
4. If usable blocks < `min_blocks` (6): return `(None, 0.0, REJECT_TOO_FEW_BLOCKS)`.
5. Build synthetic sample dicts `{"indoor_temp_f": ..., "outdoor_temp_f": ..., "elapsed_minutes": ...}` from block means.
6. Call `compute_k_passive(synthetic_samples, min_samples=min_blocks - 1)` and return its result directly.

**Rejection codes:**

| Code | Condition |
|---|---|
| `REJECT_TOO_FEW_BLOCKS` | Usable blocks (≥2 entries each) < `min_blocks` (6) |
| `REJECT_TOO_FEW_SAMPLES` | Delegated from `compute_k_passive` on the synthetic samples |
| `REJECT_OLS_WRONG_SIGN` | Delegated from `compute_k_passive` |
| `REJECT_OLS_BOUNDS` | Delegated from `compute_k_passive` |
| `REJECT_OLS_BAD_FIT` | Delegated from `compute_k_passive` (R² < 0.20) |

**Post-condition:** Same as `compute_k_passive`: `(k_passive, r_squared, rejection_code)`. Exactly one of `k_passive` or `rejection_code` is non-None.

---

### compute_k_env_solar()

**Signature:** `compute_k_env_solar(samples, min_samples=4) -> tuple[float | None, float | None, float | None]`

**Input:** `samples` — list of sample dicts with `indoor_temp_f`, `outdoor_temp_f`, `elapsed_minutes`, `solar_factor`. Consecutive pairs are used.

**Pre-condition:** `len(pairs) >= min_samples` (4 by default) AND `sf_range = max(sfs) − min(sfs) >= THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`.

**Computation:** 2-param OLS via 2×2 normal equations (no scipy):
```
[x1x1  x1x2] [k_env  ]   [x1y]
[x1x2  x2x2] [k_solar] = [x2y]

where:
  x1 = delta_i = midpoint(T_in − T_out)
  x2 = sf_i = midpoint(solar_factor)
  y  = rate_i = (T_in[i+1] − T_in[i]) / dt_hours
```
Solved as:
```
det = x1x1 * x2x2 − x1x2²
k_env   = (x2x2 * x1y − x1x2 * x2y) / det
k_solar = (x1x1 * x2y − x1x2 * x1y) / det
```

**R² (mean-centered, 2-param):**
```
R² = 1 − Σ(rate_i − k_env×delta_i − k_solar×sf_i)² / Σ(rate_i − mean_rate)²
```

**Fallback conditions (returns `(None, None, None)`):**
- Fewer than `min_samples` pairs
- `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE` — insufficient solar variation for 2-param separation (returns without emitting a rejection code; caller falls back to 1-param)
- `abs(det) < 1e-12` — numerical near-singular matrix
- Bounds fail or R² below threshold — rejection handled by caller (`_commit_event_from_dict`) which falls through to 1-param

**Accepted bounds (checked in `_commit_event_from_dict`):**
- `k_env` in `[THERMAL_K_PASSIVE_MIN (-0.5), 0.001]`
- `k_solar` in `[0.0, THERMAL_K_SOLAR_MAX_F_PER_HR (8.0)]`
- `R² >= THERMAL_MIN_R_SQUARED (0.20)`

**Post-condition:** Returns `(k_env, k_solar, r_squared)` on success, or `(None, None, None)` on any failure. Note: does not emit a `REJECT_*` string — the `None` tuple is the failure signal.

---

## Commit Routing (_commit_event_from_dict)

`_commit_event_from_dict(event, force_grade, obs_type)` selects the commit path based on `obs_type`. Returns `(obs_dict | None, reject_code | None, r_squared | None)`.

| obs_type | Commit path | Cache keys written by `_update_thermal_model_cache` | `hvac_mode` tag in committed obs dict |
|---|---|---|---|
| `passive_decay` | 1-param OLS (`compute_k_passive`) on `event["samples"]`; min `THERMAL_MIN_DECAY_SAMPLES (4)` | `k_passive`, `avg_r_squared_passive`, `observation_count_passive` | `"passive"` |
| `fan_only_decay` | 1-param OLS on `event["samples"]` | `k_vent`, `observation_count_fan_only` | `"fan_only"` |
| `ventilated_decay` | 2-param OLS attempted first (when `sf_range >= 0.30`); if fails, 1-param fallback | `k_vent_window`, `observation_count_vent`; `k_solar` additionally when `two_param=True` | `"ventilated"` |
| `solar_gain` | Mean rate: `(T_last − T_first) / total_hours`; reject if rate < 0 | `k_solar`, `observation_count_solar` | `"solar"` |
| `hvac_heat` | 2-param path: `compute_k_passive(post_samples, pre_samples)` → `compute_k_active(active_samples, k_p)`; bridge proxy and single-point fallback applied when OLS returns None | `k_active_heat`, `k_passive` (when not from proxy), `observation_count_heat`, `swing_heat_f` | `"heat"` |
| `hvac_cool` | Same as hvac_heat; `session_mode = "cool"` | `k_active_cool`, `k_passive` (when not from proxy), `observation_count_cool`, `swing_cool_f` | `"cool"` |

**Bridge proxy (hvac_heat/hvac_cool only, D17):** If `compute_k_passive()` returns `None` and `k_vent_window < 0` exists in `thermal_model_cache`, `k_vent_window` is used as proxy `k_passive` with `force_grade = "low"`. The committed obs dict writes `k_passive = None` (D21) so the proxy value never contaminates the envelope EWMA.

**Single-point fallback (D19):** If `k_active` is `None` after `compute_k_active()` (n_active < 2) and `k_p` is available (real or proxy), `compute_k_active_single_point()` is called with `T_start`, `T_peak`, `session_minutes / 60`, `k_p`, and `avg(T_in − T_out)`. Forces `grade = "low"`.

---

## EWMA Update (_update_thermal_model_cache)

Called by `record_thermal_observation()` on every successful commit. Applies one observation to the in-memory `thermal_model_cache` dict.

**Alpha lookup by confidence grade:**

| Grade | Alpha |
|---|---|
| `"high"` | 0.30 |
| `"medium"` | 0.15 |
| `"low"` | 0.05 |
| (unknown) | 0.05 |

**Confidence grade thresholds — `confidence_k_passive`** (counts from `observation_count_passive` + `observation_count_heat` + `observation_count_cool`):

| Observation count | Grade |
|---|---|
| < 5 | `"none"` |
| 5 – 14 | `"low"` |
| 15 – 29 | `"medium"` |
| ≥ 30 | `"high"` |

**Confidence grade thresholds — `confidence` (HVAC)** (counts from `observation_count_heat` + `observation_count_cool`):

| Observation count | Grade |
|---|---|
| < 5 | `"none"` |
| 5 – 9 | `"low"` |
| 10 – 19 | `"medium"` |
| ≥ 20 | `"high"` |

**EWMA formula for all continuous parameters:**
```
new_value = (1 − alpha) × old_value + alpha × observed_value
```
First observation initialises the cache field directly (no EWMA).

**Parameter routing by `obs["hvac_mode"]` (the tag written at commit time):**

| `hvac_mode` tag | Updates | Guard |
|---|---|---|
| `"heat"` | `k_passive` (EWMA if `k_p` not None), `avg_r_squared_passive`, `k_active_heat` (EWMA if `k_a` not None), `observation_count_heat`, `swing_heat_f` (if `swing_f` present) | `_envelope_modes = True` — k_passive EWMA runs |
| `"cool"` | `k_passive`, `avg_r_squared_passive`, `k_active_cool`, `observation_count_cool`, `swing_cool_f` | `_envelope_modes = True` |
| `"passive"` | `k_passive`, `avg_r_squared_passive`, `observation_count_passive` | `_envelope_modes = True` |
| `"fan_only"` | `k_vent` (EWMA of `obs["k_passive"]`), `observation_count_fan_only` | `_envelope_modes = False` — k_passive EWMA does NOT run |
| `"ventilated"` | `k_vent_window` (EWMA of `obs["k_passive"]`), `k_solar` (EWMA of `obs["k_solar"]` when `two_param=True`), `observation_count_vent` | `_envelope_modes = False` |
| `"solar"` | `k_solar` (EWMA of `obs["k_solar"]`), `observation_count_solar` | `_envelope_modes = False` |

**Swing update:** Applied for `"heat"` and `"cool"` modes only. Both `swing_heat_f` / `swing_cool_f` and their counters (`observation_count_swing_heat` / `observation_count_swing_cool`) are updated with the same alpha as the primary parameters.

---

## Rolling Window Constraints

| Constant | Value | Effect |
|---|---|---|
| `THERMAL_ROLLING_MIN_WINDOW_MINUTES` | 30 min | No commit attempt before this elapsed time; observation keeps accumulating regardless of signal |
| `THERMAL_ROLLING_MAX_WINDOW_MINUTES` | 240 min (4h) | Hard cap: forces commit if `len(samples) >= 5`, else abandons unconditionally; `skip_delta_guard=True` |
| `THERMAL_ROLLING_MIN_DELTA_T_F` | 0.2°F | Minimum indoor temperature range required to commit at min-window point (passive_decay, solar_gain); skipped for fan_only_decay and ventilated_decay (`skip_delta_guard=True`) |
| `THERMAL_MIN_DECAY_SAMPLES` | 4 | OLS pair floor for rolling-window commits; `_commit_rolling_window_obs` requires `len(samples) >= 5` (= 4 + 1) to guarantee 4 pairs |

**Early commit condition:** `elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES` AND `signal_sufficient=True`. Signal is type-specific:
- `passive_decay`: `max(indoor_temps) − min(indoor_temps) >= THERMAL_ROLLING_MIN_DELTA_T_F`
- `fan_only_decay`: same range check, but `skip_delta_guard=True` so this check is bypassed in `_commit_rolling_window_obs`
- `ventilated_decay`: indoor range check, additionally suppressed when solar keep-alive guard applies (daytime AND `sf_range < 0.30`)
- `solar_gain`: indoor range check

**Solar keep-alive guard:** Active during hours 08:00–17:59 when `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`. Forces `_vent_signal_sufficient = False` for `ventilated_decay`, deferring early commit until `sf_range` meets threshold or the 240-min hard cap fires.

---

## Gate Bridge

**Activation condition (in `_build_predicted_indoor_future`):**
```python
if (_k_passive is None or _conf_k_passive == "none") and _k_vent_window is not None and _k_vent_window <= 0:
    _k_passive = _k_vent_window
    _k_passive_via_bridge = True
```

The bridge also fires when `_k_passive is not None` but `_conf_k_passive == "none"` (Bug A fix from Issue #126): the passive estimate exists but confidence is too low without the bridge.

**Proxy semantics:** `k_vent_window` is an overestimate of `k_passive` because it includes ventilation effect. Force grade `"low"` is used for any HVAC observation that uses the proxy. In `_commit_event_from_dict`, committed obs writes `k_passive=None` (D21) so the proxy never enters the envelope EWMA.

**`physics_eligible` flag (exact code, coordinator.py ~L4371):**
```python
_physics_eligible = (
    (
        _conf != "none"
        or (_conf_k_passive is not None and _conf_k_passive not in (None, "none"))
        or _k_passive_via_bridge  # bridge-provided k needs no confidence count
    )
    and _k_passive is not None
    and (_k_passive < 0 or _k_passive_via_bridge)
)
```

When `_k_passive_via_bridge=True`, the confidence count requirement is bypassed — physics activates even with `conf="none"` and `conf_k_passive="none"`.

**ODE path when bridge is active:** `_k_passive_via_bridge=True` sets a secondary guard (`_bridge_guard_applies`) that disables per-hour `k_vent_window` substitution on window-open hours when a window schedule exists. This prevents double-application: the bridge already uses `k_vent_window` as the base; substituting it again for open-window hours would overcorrect.

**Bridge guard condition:**
```python
_bridge_guard_applies = (
    _k_passive_via_bridge
    and _windows_recommended          # classification has a window schedule
    and not _hour_windows_open        # current hour is outside the open window
)
```
When the guard applies, ramp interpolation is used for that hour. When windows are NOT recommended (no schedule), the bridge runs for all hours without guard interference.

---

## Swing Detection

**Formula:** `swing_f = abs(T_end − T_start) / 2`

**T_start:** `event["start_indoor_f"]` — indoor temperature at HVAC-on event.

**T_end definitions by mode:**
- `hvac_heat`: `active_samples[-1]["indoor_temp_f"]` — temperature at HVAC shutoff (last active sample), NOT the global peak. Using the global peak would include post-heat overshoot and bias swing high.
- `hvac_cool`: `min(s["indoor_temp_f"] for s in active_samples)` — trough temperature during active cooling.

**Minimum signal gate:** `abs(T_end − T_start) >= THERMAL_HVAC_MIN_SIGNAL_F (0.5°F)`. If the delta is below this threshold, no swing value is written.

**Valid range:** `[THERMAL_SWING_MIN_F (0.1°F), THERMAL_SWING_MAX_F (5.0°F)]`. Values outside this range are discarded.

**Storage:** `swing_heat_f` and `swing_cool_f` fields in `thermal_model_cache`. Both are EWMA-accumulated independently using the same alpha as k_active for that observation.

**Default for display:** `THERMAL_SWING_DEFAULT_F = 1.5°F` — used when the observed value is None (`swing_heat_f_display` / `swing_cool_f_display` in `get_thermal_model()` output).

**Confidence tiers:**

| `observation_count_swing_heat` or `_cool` | Grade |
|---|---|
| 0 (`< THERMAL_SWING_CONF_LOW = 1`) | `"none"` |
| 1–2 (`< THERMAL_SWING_CONF_MEDIUM = 3`) | `"low"` |
| 3–9 (`< THERMAL_SWING_CONF_HIGH = 10`) | `"medium"` |
| 10+ | `"high"` |

---

## Dual Estimator Framework

*Added in v0.3.45 (Issue #146). Applies to both `_run_passive_chart_log_fit` and `_run_ventilated_chart_log_fit`.*

### Motivation

The in-memory consecutive-pair OLS on 5-min thermostat samples structurally fails for 1°F thermostat resolution: overnight drift of ≈0.25°F/hr produces 0.021°F per 5-min interval, well below the 1°F quantization floor. Nearly all pairs show rate=0; rare pairs show ±12°F/hr spikes. R² ≈ 0.02; almost all windows are rejected. The v0.3.43 chart_log endpoint backfill committed 8 windows at α=0.05, yielding only 33% convergence ((0.95)^8 = 0.663 weight on prior). Both k_passive and k_vent_window were ≈3–5× too large, causing the overnight predicted indoor temperature to dip 8–10°F below actual.

The dual-estimator framework runs both estimators on every overnight chart_log window and selects per-night based on data quality. Backfill v2 reprocesses 30 days, accumulating enough EWMA iterations for convergence.

### Estimator A — Endpoint

`k = ln((T_end − T_out_avg) / (T_start − T_out_avg)) / Δt_hours`

- Uses only the bookend readings of the window; immune to mid-window sensor blips that corrupt interior samples
- Natural regime filter: ratio in (0, 1) rejects solar and HVAC contamination before OLS runs
- No R² (returns `r_squared=None`); grade always `"low"`
- Source label: `"endpoint"`

### Estimator B — Block-Averaged OLS

`compute_k_passive_blocks()` on the window's chart_log entries (see [§OLS Functions — compute\_k\_passive\_blocks](#compute_k_passive_blocks)).

- 60-min blocks, minimum 6 blocks (≥6h window)
- Produces a meaningful R² (typically 0.5–0.8 on clean overnight windows)
- Source label: `"block_ols"`

### Solar Guard

Both extractors (`_extract_passive_windows`, `_extract_ventilated_windows`) accept only windows where both the start **and** end timestamps fall within local hours 20:00–08:00 (i.e., neither end is in the 08:00–19:59 daytime band). `_is_solar_hour(ts_str)` returns `True` when local hour is 8–19. Any window touching a solar hour is dropped.

This prevents solar-heated afternoon samples from contaminating the passive decay estimate for both estimators simultaneously.

### Per-Night Selection (`_select_estimator`)

`_select_estimator(result_a, result_b) -> dict | None`

Both A and B always run. The decision table selects one result and sets the final `grade`:

| A-valid | B-valid | R²_B | Agree (≤30% rel diff)? | Selection | Grade |
|---|---|---|---|---|---|
| no | no | — | — | `None` | — |
| yes | no | — | — | A | `low` |
| no | yes | < 0.20 | — | `None` | — |
| no | yes | 0.20–0.49 | — | B | `low` |
| no | yes | ≥ 0.50 | — | B | `medium` |
| yes | yes | < 0.20 | — | A | `low` |
| yes | yes | 0.20–0.49 | yes | B | `low` |
| yes | yes | 0.20–0.49 | no | A | `low` |
| yes | yes | ≥ 0.50 | yes | B | `medium` |
| yes | yes | ≥ 0.50 | no | A | `low` |

Agreement is defined as `abs(k_A − k_B) / max(abs(k_A), abs(k_B)) <= THERMAL_DUAL_AGREE_REL (0.30)`.

Thresholds: `THERMAL_DUAL_OLS_GOOD = 0.50` (medium grade boundary), `THERMAL_DUAL_OLS_OK = 0.20` (B-valid floor).

When both estimators are valid, `_select_estimator` logs an INFO line:
```
chart_log dual_estimator passive: k_A=−0.021 k_B=−0.019 R²_B=0.63 agree=True source=block_ols grade=medium
```

`observation_count_passive` increments by 1 per committed window regardless of which estimator won.

### Backfill v2

New flags `_passive_k_backfill_v2` and `_vent_k_backfill_v2` (distinct from the v1 `_passive_k_backfill` flag). Persisted in `_build_state_dict`, restored in `async_restore_state`.

On startup, if `_passive_k_backfill_v2` is `False`, `_run_passive_chart_log_fit(backfill=True)` processes the last 30 days of chart_log entries through the full dual-estimator pipeline. Each selected window commits one EWMA update. At medium grade (α=0.15), 30 updates converge to ≈99% of the true value: `1 − (0.85)^30 ≈ 0.990`. Same for `_vent_k_backfill_v2` via `_run_ventilated_chart_log_fit(backfill=True)`.

### Symmetric Application

`_run_ventilated_chart_log_fit` follows the same structure: extract windows with solar guard → per-window run A + B + `_select_estimator` → `record_thermal_observation`. The ventilated path writes to `k_vent_window` rather than `k_passive`, but the estimator machinery (`compute_k_passive_blocks`, `_passive_endpoint_estimate`, `_select_estimator`) is reused unchanged.

---

## Solar Factor

**Scope:** `_solar_factor(local_hour, phase_offset_h)` in `coordinator.py`. Determines how much solar gain to add to the ODE at a given local hour. Used by `_simulate_indoor_physics_v3`, `_build_predicted_indoor_future`, ventilated-decay sample injection, and the `solar_gain` observation trigger.

### Signature

```python
def _solar_factor(
    local_hour,                                              # int | float; local clock hour
    phase_offset_h=THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT,   # default = 2 (peak at 3pm)
) -> float
```

**Pre-conditions:**
- `local_hour` must be an `int` or `float`; any other type returns `0.0`
- `phase_offset_h` is read from `get_thermal_model()["solar_phase_offset_h"]` at each call site, falling back to `THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT = 2` when the learned value is not yet available

### Algorithm

```python
effective_hour = int(local_hour) - int(round(phase_offset_h))
if effective_hour < THERMAL_SOLAR_DAYTIME_START_H:   # 8
    return 0.0
if effective_hour >= THERMAL_SOLAR_DAYTIME_END_H:    # 18
    return 0.0
# sin curve over [8, 18), peak at effective_hour = 13
angle = (effective_hour - THERMAL_SOLAR_DAYTIME_START_H) / (THERMAL_SOLAR_DAYTIME_END_H - THERMAL_SOLAR_DAYTIME_START_H) * π
return max(0.0, sin(angle))
```

`effective_hour = 13` → `sin(π/2) = 1.0` (global maximum).

### Peak Mapping by Offset

| `phase_offset_h` | `effective_hour` at `local_hour = 13 + offset` | Solar factor | Real-world peak |
|---|---|---|---|
| 0 | 13 | 1.0 | 1:00 PM (old hard-coded behavior) |
| 1 | 13 | 1.0 | 2:00 PM |
| 2 | 13 | 1.0 | 3:00 PM (default prior — `THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT`) |
| 3 | 13 | 1.0 | 4:00 PM |
| 4 | 13 | 1.0 | 5:00 PM |

### Algebraic Correctness

The formula satisfies `_solar_factor(13 + n, n) == 1.0` for all integer `n` in [0, 4]:

```
effective_hour = int(13 + n) - int(round(n)) = 13
sin(angle at effective_hour=13) = sin(π/2) = 1.0
```

This is the scientific proof for test `test_peak_at_15_with_offset_two` (and all offset variants).

### Call-site update pattern

All existing `_solar_factor(hour)` calls must be updated to pass `phase_offset_h`. The coordinator reads the offset once per prediction cycle:

```python
_phase_offset = self._solar_phase_offset  # float; updated each _async_update_data
# … per-hour loop:
sf = _solar_factor(h, _phase_offset)
```

`self._solar_phase_offset` is an instance attribute initialised to `THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT` and refreshed from `get_thermal_model()["solar_phase_offset_h"]` on each coordinator update cycle.

### Invariants

- Return value is always in [0.0, 1.0]
- Returns `0.0` when `local_hour` is not a numeric type (guards against `MagicMock` test stubs)
- Returns `0.0` for `effective_hour < 8` or `effective_hour >= 18` regardless of offset

---

## Solar Phase Offset Learning

**Scope:** `_estimate_solar_phase_offset`, `_run_solar_phase_chart_log_fit`, and `update_solar_phase_offset` in `coordinator.py` / `learning.py`. Learns the home's thermal lag from chart_log daytime passive windows. Writes `solar_phase_offset_h` to `thermal_model_cache` via EWMA.

### Core Concept

Buildings with high thermal mass absorb solar radiation through the afternoon and re-radiate it as heat into the interior, causing the indoor temperature peak to lag the solar peak by 2–4 hours. This lag is home-specific and must be learned, not hard-coded. The phase offset calibrates `_solar_factor` to match the home's actual thermal inertia.

### Phase Observation Formula

```
phase_obs = actual_indoor_peak_hour - 13
```

`actual_indoor_peak_hour` is the local hour of the maximum indoor temperature in the chart_log window. The value 13 is the no-offset solar peak hour. A `phase_obs` of 2 means the indoor peak occurred at 3pm — exactly 2 hours after the no-offset solar peak.

### EWMA Update Formula

```python
new_value = (1 - THERMAL_SOLAR_PHASE_ALPHA) × solar_phase_offset_h
           + THERMAL_SOLAR_PHASE_ALPHA × clamp(phase_obs, THERMAL_SOLAR_PHASE_OFFSET_MIN, THERMAL_SOLAR_PHASE_OFFSET_MAX)
```

| Constant | Value | Meaning |
|---|---|---|
| `THERMAL_SOLAR_PHASE_ALPHA` | 0.10 | Slow EWMA — building physics changes only with major renovation |
| `THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT` | 2 | Prior before any learning (peak at 3pm) |
| `THERMAL_SOLAR_PHASE_OFFSET_MIN` | 0 | Lower clamp bound (no advance of solar peak) |
| `THERMAL_SOLAR_PHASE_OFFSET_MAX` | 4 | Upper clamp bound (peak at 5pm maximum) |

**First observation:** Initialises `solar_phase_offset_h` directly to the clamped `phase_obs` — no EWMA blend on the first update (same pattern as all other thermal parameters).

### Valid Window Criteria

A chart_log window is eligible for a phase observation only when all six conditions are met:

1. **HVAC off throughout:** `hvac` field is not `"heating"` or `"cooling"` for every entry
2. **Fan off throughout:** `fan` field is falsy for every entry
3. **Windows closed throughout:** `windows_open` field is `False` for every entry
4. **Daytime span:** all entries fall within local hours 08:00–20:00
5. **Minimum window span:** `last_entry_ts − first_entry_ts >= THERMAL_SOLAR_PHASE_MIN_WINDOW_H (4h)`
6. **Minimum entry count:** `len(window_entries) >= THERMAL_SOLAR_PHASE_MIN_ENTRIES (3)`
7. **Sufficient solar signal:** `max(indoor) − min(indoor) >= THERMAL_SOLAR_PHASE_MIN_DT_F (1.5°F)` — distinguishes a real solar rise from sensor noise
8. **Not a leading peak:** the maximum indoor temperature must NOT be the first entry — a first-entry peak means the window captured the tail of a prior peak, not a rise. A last-entry peak is acceptable (the window end may have truncated a still-rising temperature)

### Rejection Codes

| Code | Condition | Constant |
|---|---|---|
| `REJECT_TOO_FEW_SAMPLES` | `len(window_entries) < THERMAL_SOLAR_PHASE_MIN_ENTRIES` | Existing constant |
| `REJECT_WINDOW_TOO_SHORT` | Window span < `THERMAL_SOLAR_PHASE_MIN_WINDOW_H` | New in v0.3.46 |
| `REJECT_SMALL_DELTA` | Indoor ΔT < `THERMAL_SOLAR_PHASE_MIN_DT_F` | Existing constant |
| `REJECT_NO_INTERIOR_PEAK` | Peak is at the first entry (`peak_idx == 0`) — a last-entry peak is accepted | New in v0.3.46 |

### New Functions

**`_estimate_solar_phase_offset(window_entries) → (float | None, str | None)`**

- **Input:** list of chart_log entry dicts (fields: `ts`, `indoor`, `outdoor`, `hvac`, `fan`, `windows_open`)
- **Pre-conditions:** all valid window criteria above
- **Computation:** find `idx = argmax(indoor values)`; reject if `idx == 0` (leading peak, not a rise); compute `phase_obs = local_hour(ts[idx]) − 13`
- **Post-conditions:** returns `(phase_obs_clamped, None)` on success; `(None, reject_code)` on any failure
- **Invariant:** exactly one of `phase_obs` or `reject_code` is non-None

**`_run_solar_phase_chart_log_fit(backfill=False)`**

- **Purpose:** iterates daytime passive windows in the chart_log and calls `_estimate_solar_phase_offset` on each, calling `self.learning.update_solar_phase_offset(phase_obs, THERMAL_SOLAR_PHASE_ALPHA)` on each accepted window. When `backfill=True`, scans the last 30 days; when `backfill=False`, scans only the last 2 days (most-recent qualifying window only via `windows[-1:]`)
- **Backfill flag:** `_solar_phase_backfill: bool` persisted in state. On startup, if `False`, runs in `backfill=True` mode over the last 30 days and sets flag to `True`
- **Call site:** called once at startup (inside the chart_log processing block in `_async_update_data`) when `_solar_phase_backfill` is `False`. No incremental per-cycle call is made after the backfill flag is set

**`learning.update_solar_phase_offset(observed_h, alpha)`**

- Applies EWMA update to `thermal_model_cache["solar_phase_offset_h"]`
- On first call (value is `None`), initialises directly: `solar_phase_offset_h = clamp(observed_h, MIN, MAX)`
- Thread-safe: called only from the coordinator's async context

### `_build_learning_health` update

`REJECT_WINDOW_TOO_SHORT` and `REJECT_NO_INTERIOR_PEAK` must be added to `all_reason_codes` in `_build_learning_health()` in `coordinator.py` so they appear in the rejection summary exposed to the AI investigator and dashboard.

---

## Engine Visibility

**Scope:** `get_engine_status()` in `learning.py`; REST endpoint in `api.py`; dashboard card in `index.html`; AI context in `ai_skills_activity.py`; CLI tool `tools/engine_status.py`.

### `get_engine_status()` Return Shape

`learning.get_engine_status() → dict`

```python
{
  "k_passive": {
    "active": bool,          # True when k_passive is not None
    "value": float | None,   # current thermal_model_cache["k_passive"]
    "confidence": str,       # "none" | "low" | "medium" | "high"
  },
  "k_solar": {
    "active": bool,
    "value": float | None,
    "confidence": str,       # derived from observation_count_solar (same grade thresholds as k_passive)
  },
  "solar_phase_offset_h": {
    "active": bool,          # True when solar_phase_offset_h is not None
    "value": float | None,
  },
  "k_vent_window": {
    "active": bool,
    "value": float | None,
  },
  "k_active_hvac": {
    "active": bool,                                  # True when k_active_heat or k_active_cool is not None
    "value": {"heat": float | None, "cool": float | None},  # k_active_heat and k_active_cool
  },
  "ode_version": str,        # "v3" when k_solar or k_vent present; "basic" otherwise
  "physics_eligible": bool,  # True when the ODE prediction path is currently active
  "physics_eligible_reason": str,  # human-readable explanation of eligibility state
}
```

**`physics_eligible_reason` values** (returned by `get_engine_status()`; bridge-home state is not reflected here — bridge activation is determined in `_build_predicted_indoor_future`):

| Condition | Reason string |
|---|---|
| `k_passive is None` | `"k_passive not yet learned"` |
| `k_passive >= 0` (wrong sign) | `"k_passive has wrong sign"` |
| `confidence_k_passive == "none"` | `"confidence insufficient (none)"` |
| `k_passive < 0` and `confidence != "none"` | `f"k_passive + confidence={conf_k_passive}"` (e.g. `"k_passive + confidence=low"`) |

### Exposure Points

| Consumer | Mechanism |
|---|---|
| REST API | `GET /api/climate_advisor/engines` returns `get_engine_status()` JSON directly |
| Dashboard Debug tab | "Prediction Engines" card in `index.html`; table: engine \| active \| value \| confidence; auto-refreshes with status panel |
| AI investigator | `ACTIVE_PREDICTION_ENGINES` section prepended to activity context in `ai_skills_activity.py`; plain-text table for LLM consumption |
| CLI tool | `tools/engine_status.py` reads learning DB via SSH (same pattern as `tools/learning_db.py`), prints formatted table; `--history` flag also tails `ha_logs.py --thermal` and greps for engine activation events |

### `get_thermal_model()` additions

`solar_phase_offset_h` is included in the `get_thermal_model()` return dict. Downstream consumers (`coordinator.py`, `api.py`, `ai_skills_activity.py`) read from this output, not from `thermal_model_cache` directly.

---

## Observation Pipeline Failure Modes

This section documents failure modes in the HVAC observation pipeline that have been diagnosed and fixed, and known operational rejection patterns. Understanding these modes is essential when interpreting `--rejections` output or investigating `k_active_cool=None` on a home with active AC cycling.

### Fixed: `"samples": []` Key Shadow (Issue #156)

**Root cause:** `_start_hvac_observation` previously created the observation dict with both a `"samples": []` key and an `"active_samples": []` key. Any code that called `obs.get("samples", ...)` received the empty `[]` immediately — the `"active_samples"` key (where real data was appended) was never reached.

**Symptoms:** `k_active_cool=None` and `k_active_heat=None` despite HVAC cycling normally. Rejection log would show `n=0` for hvac_heat/hvac_cool entries with non-zero elapsed time. The 5-min polling tick was appending samples to `active_samples`, but commit-path code reading `"samples"` always saw an empty list.

**Resolution (v0.3.50):** The `"samples": []` key was removed from the HVAC obs dict at creation time. Only `"active_samples"` and `"post_heat_samples"` are used for HVAC types. The commit-path and startup-recovery code was updated to read these keys explicitly.

**Detection:** If `--rejections --type hvac_cool` shows repeated entries with `n=0` and elapsed > 0, and the coordinator version predates v0.3.50, this is the likely cause.

### Fixed: Startup Recovery Discarding All HVAC Pending Observations (Issue #156)

**Root cause:** The startup recovery loop (run on HA restart to decide whether in-flight observations are worth continuing or should be abandoned) used `obs.get("samples", [])` for all observation types. For HVAC types with the `"samples"` shadow bug, this always returned `[]`, and recovery immediately abandoned every pending HVAC observation with `n=0`.

**Resolution (v0.3.50):** Startup recovery is now phase-aware for HVAC types:
- `_phase == "post_heat"`: reads `post_heat_samples`, requires `min_s = THERMAL_MIN_POST_HEAT_SAMPLES (4)`
- `_phase == "active"` (default): reads `active_samples`, requires `min_s = 1` — any sample is worth recovering so the post-heat window can continue after restart
- Backward-compat fallback: if `active_samples` is empty (pre-fix persisted obs), falls back to `obs.get("samples", [])`

Non-HVAC types (passive_decay, fan_only_decay, etc.) use the generic `obs.get("samples", obs.get("active_samples", []))` path.

### Fixed: `_abandon_observation` Reporting n=0 (Issue #156)

**Root cause:** The rejection log entry's `n` field was always computed from `obs.get("samples", [])` — the shadowed empty list — rather than the actual sample count.

**Resolution (v0.3.50):** `_abandon_observation` now reads sample count from the correct key per type: `active_samples` for HVAC active-phase observations, `post_heat_samples` for post-heat, and `samples` for rolling-window types. Rejection log entries now show real sample counts.

**Debugging note:** Rejection log entries with `n=0` and elapsed > a few minutes in pre-v0.3.50 coordinators are a strong signal that the shadow bug was active — those observations had real data that wasn't being counted.

### Enhancement: Event-Driven Sampling (Issue #156)

**Context:** The 5-min polling tick (`_sample_all_observations`) can miss short HVAC cycles entirely. A cycle that starts and ends between two polling ticks produces only 1 initial sample — yielding 0 consecutive pairs for OLS. `k_active` is never fitted.

**Resolution (v0.3.50):** `_async_thermostat_changed` now appends a sample to `active_samples` when HVAC action is active at the time of any thermostat state change (temperature update, attribute change). A 60-second decimation gate prevents duplicate samples within the same minute.

**Effect:** Short cycles (1–4 min) now accumulate 3–10 event-driven samples if the thermostat is chatty, making single-point fallback (`compute_k_active_single_point`) much more likely to succeed for these sessions.

### Known Rejection Patterns

These rejection patterns in `--rejections` output are expected and do not indicate bugs:

| Rejection reason / pattern | Meaning | Action |
|---|---|---|
| `new_session_started` (repeated for hvac_cool/hvac_heat) | HVAC started a new session before the previous post-heat window finished. Most common on short-cycling thermostats or systems with rapid cycling. | Expected on short-cycling systems. If count is high relative to committed obs, the home may never reach `THERMAL_MIN_POST_HEAT_SAMPLES` before the next cycle. |
| `n=0, delta_t=0.00°F` on HVAC types (coordinator ≥ v0.3.50) | Sensor quantization — the thermostat's 1°F resolution cannot resolve 0.3–0.8°F temperature change in short cycles. Both active_samples and post_heat_samples have real data, but consecutive pairs produce rate ≈ 0. | Normal on short-cycle homes. Event-driven sampling (v0.3.50) and the single-point fallback mitigate this. |
| `plateau_guard: insufficient post-heat decay` | The post-heat phase ended without the indoor temperature dropping `THERMAL_HVAC_MIN_DECAY_F (0.3°F)` below the peak. Common on efficient systems or when HVAC duty cycle is very short. | Threshold was reduced from 1.0°F to 0.3°F in v0.3.22. If still firing, the system may have a very tight thermostat deadband. |
| `max_window_exceeded` on rolling types | Passive/fan/vent observation ran for 4 hours without meeting signal threshold. | Not a bug — the 240-min hard cap forces a commit or abandon when signal is never sufficient. |
| `REJECT_OLS_BAD_FIT` (R² < 0.20) on passive_decay | Consecutive 5-min pair OLS on 1°F thermostat data structurally fails; see §Dual Estimator Framework. | The chart_log dual-estimator (Estimator B) handles this. Active passive_decay observations supplement with real-time data. |

---

## Invariants

The following conditions must always hold after a successful commit and EWMA update:

1. **k_passive sign:** Every value written to `cache["k_passive"]` via `_update_thermal_model_cache` is negative (`k_p < 0`). The `REJECT_OLS_WRONG_SIGN` check in `compute_k_passive` enforces this before any value reaches the cache. The only exception is the bridge proxy path in `_commit_event_from_dict` — but in that path `k_passive=None` is written to the obs dict (D21), so the cache is never updated with the proxy value.

2. **k_vent_window sign:** `k_vent_window` in cache is always ≤ 0 when valid. The bridge activation check (`_k_vent_window <= 0`) enforces this. A value of exactly 0.0 is physically valid (perfectly inert home with zero ventilation effect) and produces a flat ODE prediction.

3. **k_solar sign:** `k_solar` is always non-negative. The bounds check `0.0 <= k_solar <= THERMAL_K_SOLAR_MAX_F_PER_HR` in `_commit_event_from_dict` and the mean-rate sign check (`if mean_rate < 0: reject`) for solar_gain observations enforce this.

4. **Separation of envelope and ventilation:** The guard `_envelope_modes = mode not in ("fan_only", "ventilated")` in `_update_thermal_model_cache` ensures that `fan_only` and `ventilated` observations never write to `cache["k_passive"]`. Only `"heat"`, `"cool"`, and `"passive"` modes update the envelope decay estimate.

5. **Rejection code exclusivity:** `compute_k_passive` returns exactly one of `(k_passive, rejection_code)` as non-None. The function never returns both `k_passive` and a `rejection_code` as non-None simultaneously.

6. **Obs cap:** `thermal_observations` list in `LearningState` never exceeds `THERMAL_OBS_CAP (200)` entries. The 90-day rolling trim runs first; the hard cap enforces the absolute maximum.

7. **Rejection log cap:** Each obs_type bucket in `rejection_log` is capped at 100 entries. Both `_abandon_observation()` in coordinator and `load_state()` enforce this cap.

8. **Bridge does not contaminate k_passive EWMA:** When `_k_p_from_proxy=True` in `_commit_event_from_dict`, `obs["k_passive"] = None` is set before calling `record_thermal_observation()`. This ensures `_update_thermal_model_cache` sees `k_p = None` and skips the `k_passive` EWMA update.

9. **ODE k_passive must be negative for exponential decay:** `_simulate_indoor_physics` and `_simulate_indoor_physics_v3` use `exp(k_passive * dt_hours)`. With `k_passive < 0` this decays toward `t_outdoor`; with `k_passive = 0` the division-by-zero branch uses linear extrapolation (`t_start + q * dt_hours`). The bridge allows `k_vent_window = 0.0` exactly, which routes to this linear branch — correct for a perfectly inert home.
