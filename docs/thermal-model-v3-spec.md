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
| How does `vent_window_decay`/`vent_fan_decay` commit path choose between 1-param and 2-param OLS? | When solar factor range across samples ≥ 0.30, the 2-param path fires first and — if bounds pass — commits both `k_env` and `k_solar`, bypassing 1-param entirely. If 2-param fails bounds/R², the 1-param path runs as fallback. Identical logic for both vent-split types (shared, regime-agnostic). | [§OLS Functions — compute\_k\_env\_solar](#compute_k_env_solar) |
| What is the EWMA alpha for each confidence grade and which observation types write `k_passive`? | Alpha: high = 0.30, medium = 0.15, low = 0.05. Only `passive`, `heat`, and `cool` modes write `k_passive`; `vent_window` writes `k_vent_window`; `vent_fan` writes `k_vent_fan`; `solar` writes `k_solar` (subtracting `k_passive`'s conductive contribution first — Issue #587 Defect B). `fan_only`/`k_vent` retired (Issue #587). | [§EWMA Update](#ewma-update-_update_thermal_model_cache) |
| How does `compute_k_passive_endpoint()` fix the old endpoint formula's bias? | The old formula collapsed the window's outdoor readings into one scalar average, assuming outdoor temp was constant — biasing k by 1.3x–1.9x. The new one RK4-forward-integrates against the real per-sample outdoor trace and bisection-root-finds the k that reproduces the observed `T_end`. | [§OLS Functions — compute\_k\_passive\_endpoint](#compute_k_passive_endpoint) |
| How does `compute_k_solar()` avoid crediting conduction as solar gain? | It subtracts `k_passive * delta` (the ordinary conductive contribution) from the observed rate per interval before averaging — mirrors `compute_k_active`'s pattern exactly. Bounded to `[0.0, THERMAL_K_SOLAR_MAX_F_PER_HR]`; rejects with `REJECT_NO_K_PASSIVE` if no `k_passive` is cached yet. | [§OLS Functions — compute\_k\_solar](#compute_k_solar) |
| What changed with Issue #587 (k_vent/fan_only_decay retirement + vent split)? | `k_vent`/`fan_only_decay` retired outright (confirmed dead — never affected a live forecast). `ventilated_decay` split into `vent_window_decay` (windows open, fan/WHF off) and `vent_fan_decay` (windows open, fan/WHF on) via one shared trigger table and one shared commit-evaluation method. | [§k_vent / fan_only_decay retirement](#k_vent--fan_only_decay-retirement-issue-587) |
| How does the dual-estimator framework select between endpoint and block-averaged OLS per overnight window? | Both estimators always run; an 8-row decision table selects based on R²_B and 30% relative agreement. On disagreement, Estimator A (endpoint) wins. On R²_B ≥ 0.50 and agreement, B wins with medium grade (α=0.15). | [§Dual Estimator Framework](#dual-estimator-framework) |
| Where is the solar factor formula defined and what does `phase_offset_h` do? | `_solar_factor(local_hour, phase_offset_h)` shifts the sinusoidal solar input peak by `phase_offset_h` hours. With the default offset=2 the peak falls at local hour 15 (3pm) instead of 13 (1pm). | [§Solar Factor](#solar-factor) |
| How is `solar_phase_offset_h` learned from chart_log? | Daytime passive windows (HVAC off, fan off, windows closed) are scanned for the indoor temperature peak hour. `phase_obs = peak_hour − 13` is accumulated via EWMA (α=0.10), clamped to [0, 4]. | [§Solar Phase Offset Learning](#solar-phase-offset-learning) |
| What is engine visibility and where is it exposed? | `get_engine_status()` returns per-engine `active`, `value`, and (for `k_passive`/`k_solar`) `confidence` fields. Exposed at REST `/api/climate_advisor/engines`, dashboard Debug tab, AI investigator context, and `tools/engine_status.py`. | [§Engine Visibility](#engine-visibility) |
| Why does `k_active_cool` stay `None` despite AC cycling normally? | Two v0.3.50 bugs can cause this: (1) the `"samples": []` key shadow — `_start_hvac_observation` created a `"samples"` key that was always returned instead of `"active_samples"`; (2) startup recovery discarded all HVAC pending obs by reading the wrong key. Both fixed in v0.3.50. Check `--rejections --type hvac_cool` for `n=0` entries with elapsed > 0. | [§Observation Pipeline Failure Modes](#observation-pipeline-failure-modes) |
| What rejection codes indicate normal operation vs. pipeline failure for HVAC obs types? | `new_session_started` (short-cycling), `plateau_guard` (insufficient post-heat decay), and `n=0 delta_t=0.00°F` (sensor quantization) are expected on some homes. `n=0` with elapsed > 0 in pre-v0.3.50 coordinators indicates the key-shadow bug. | [§Known Rejection Patterns](#known-rejection-patterns) |

---

## Scope

**Files:**
- `learning.py` — OLS functions (`compute_k_passive`, `compute_k_passive_blocks`, `compute_k_passive_endpoint`, `compute_k_env_solar`, `compute_k_active`, `compute_k_active_single_point`, `compute_k_solar`), RK4 helpers (`_interp_outdoor`, `_integrate_indoor_rk4`), commit routing (`_commit_event_from_dict`), EWMA update (`_update_thermal_model_cache`), confidence grading (`_grade_passive_confidence`), model output (`get_thermal_model`, `record_thermal_observation`), solar phase learning (`update_solar_phase_offset`), engine visibility (`get_engine_status`)
- `coordinator.py` — observation orchestration (`_sample_all_observations`, `_start_hvac_observation`, `_start_decay_observation`, `_end_hvac_active_phase`, `_check_hvac_stabilization`, `_evaluate_rolling_window`, `_commit_rolling_window_obs`, `_commit_observation_if_sufficient`, `_abandon_observation`, `_commit_observation`, `_evaluate_vent_split_observation`), ODE prediction (`_build_predicted_indoor_future`, `_simulate_indoor_physics`, `_simulate_indoor_physics_v3`), dual-estimator chart_log fit (`_is_solar_hour`, `_select_estimator`, `_extract_passive_windows`, `_passive_endpoint_estimate`, `_run_passive_chart_log_fit`, `_extract_ventilated_windows` (now takes `fan_state` kwarg), `_vent_endpoint_estimate` (renamed from `_ventilated_endpoint_estimate`), `_run_vent_window_chart_log_fit`, `_run_vent_fan_chart_log_fit`, `_run_vent_chart_log_fit_impl`), solar factor (`_solar_factor`), solar phase offset learning (`_estimate_solar_phase_offset`, `_run_solar_phase_chart_log_fit`)

**Issue #587 (k_vent/fan_only_decay retirement + vent split), and the two accompanying calculation-defect fixes, landed together** — see [§k_vent / fan_only_decay retirement](#k_vent--fan_only_decay-retirement-issue-587) for the combined summary. Tracking issues: [#587](https://github.com/gunkl/ClimateAdvisor/issues/587) (structural split), [#851](https://github.com/gunkl/ClimateAdvisor/issues/851) (endpoint estimator time-varying-outdoor bias), [#852](https://github.com/gunkl/ClimateAdvisor/issues/852) (solar_gain conduction attribution). Known-gap follow-ups filed alongside, not fixed in this round: [#853](https://github.com/gunkl/ClimateAdvisor/issues/853) (vent-observation sensor-causality gap), [#854](https://github.com/gunkl/ClimateAdvisor/issues/854) (post-deploy monitoring).

**Line ranges (re-verified against source, 2026-09-04, post-Issue-#587):**

| Function | File | Start line |
|---|---|---|
| `compute_k_passive` | learning.py | 276 |
| `compute_k_passive_blocks` | learning.py | 374 |
| `_interp_outdoor` | learning.py | 459 |
| `_integrate_indoor_rk4` | learning.py | 480 |
| `compute_k_passive_endpoint` | learning.py | 513 |
| `compute_k_env_solar` | learning.py | 609 |
| `compute_k_active` | learning.py | 685 |
| `compute_k_solar` | learning.py | 744 |
| `compute_k_active_single_point` | learning.py | 806 |
| `record_thermal_observation` (method) | learning.py | 1078 |
| `update_solar_phase_offset` | learning.py | 1219 |
| `get_engine_status` | learning.py | 1296 |
| `get_thermal_model` | learning.py | 1372 |
| `_commit_event_from_dict` | learning.py | 1478 |
| `_start_hvac_observation` | coordinator.py | 6115 |
| `_sample_all_observations` | coordinator.py | 6219 |
| `_evaluate_vent_split_observation` | coordinator.py | 6467 |
| `_is_solar_hour` | coordinator.py | 6542 |
| `_select_estimator` | coordinator.py | 6553 |
| `_extract_passive_windows` | coordinator.py | 6630 |
| `_extract_ventilated_windows` (`fan_state` kwarg) | coordinator.py | 6842 |
| `_passive_endpoint_estimate` | coordinator.py | 6701 |
| `_vent_endpoint_estimate` (renamed from `_ventilated_endpoint_estimate`) | coordinator.py | 6916 |
| `_run_passive_chart_log_fit` | coordinator.py | 6734 |
| `_run_vent_window_chart_log_fit` | coordinator.py | 6954 |
| `_run_vent_fan_chart_log_fit` | coordinator.py | 6958 |
| `_run_vent_chart_log_fit_impl` | coordinator.py | 6962 |
| `_run_solar_phase_chart_log_fit` | coordinator.py | 7095 |
| `_start_decay_observation` | coordinator.py | 7339 |
| `_end_hvac_active_phase` | coordinator.py | 7361 |
| `_check_hvac_stabilization` | coordinator.py | 7407 |
| `_commit_observation` | coordinator.py | 7495 |
| `_abandon_observation` | coordinator.py | 7531 |
| `_commit_observation_if_sufficient` | coordinator.py | 7700 |
| `_evaluate_rolling_window` | coordinator.py | 7733 |
| `_commit_rolling_window_obs` | coordinator.py | 7804 |
| `_simulate_indoor_physics` | coordinator.py | 9722 |
| `_estimate_solar_phase_offset` | coordinator.py | 9796 |
| `_simulate_indoor_physics_v3` | coordinator.py | 9962 |
| `_build_predicted_indoor_future` | coordinator.py | 10335 |
| `_solar_factor` | coordinator.py | 9775 |

Line numbers shifted substantially from the pre-#587 table (coordinator.py grew ~2000 lines across this and other intervening changes) — re-verify with grep rather than trusting a cached line number if this table goes stale again.

**Out of scope for this spec:** suggestion generation, weather bias, daily record lifecycle, automation engine, briefing text.

---

## Observation Types

All six types run concurrently in `_pending_observations: dict[str, PendingObservation]`. The dict is keyed by the `OBS_TYPE_*` string constant.

| obs_type | Trigger conditions | Target parameter(s) | Min samples (long-window path) | Can run concurrently with? |
|---|---|---|---|---|
| `hvac_heat` | `hvac_action = "heating"` transitions to active | `k_active_heat`, `k_passive` (via pre-heat buffer) | 4 post-heat samples (`THERMAL_MIN_POST_HEAT_SAMPLES`) | Nothing HVAC; all passive types commit/abandon on HVAC start |
| `hvac_cool` | `hvac_action = "cooling"` transitions to active | `k_active_cool`, `k_passive` | 4 post-heat samples | Same as hvac_heat |
| `passive_decay` | HVAC off, fan off, windows closed, `\|T_indoor − T_outdoor\| ≥ THERMAL_PASSIVE_MIN_DELTA_F (3.0°F)` | `k_passive` | 30 (`THERMAL_PASSIVE_MIN_SAMPLES`) | `vent_window_decay`, `vent_fan_decay`, `solar_gain` — but mutual exclusion via trigger conditions (windows-closed vs. windows-open) prevents concurrent passive + either vent type |
| `vent_window_decay` | Any door/window sensor open, fan/WHF **off** (`_fan_active == False`), HVAC off, `\|T_indoor − T_outdoor\| ≥ THERMAL_VENTILATED_MIN_DELTA_F (1.0°F)` | `k_vent_window` (and optionally `k_solar` via 2-param path) | 20 (`THERMAL_VENT_MIN_SAMPLES`) | Can coexist with `passive_decay`/`solar_gain`'s parameters conceptually, but its own trigger and `vent_fan_decay`'s are mutually exclusive by fan state |
| `vent_fan_decay` | Any door/window sensor open, fan/WHF **on** (`_fan_active == True`), HVAC off, same delta threshold | `k_vent_fan` (and optionally `k_solar` via the same 2-param path) | 20 (`THERMAL_VENT_MIN_SAMPLES`) | Mutually exclusive with `vent_window_decay` by fan state; a mid-window fan toggle commits the in-progress observation and lets the sibling's trigger pick up the continuation (no discard) |
| `solar_gain` | HVAC off, fan off, windows closed, `T_indoor > T_outdoor`, daytime (local 08:00–18:00, `THERMAL_SOLAR_DAYTIME_START_H` / `END_H`) | `k_solar` (subtracting `k_passive`'s conductive contribution — Issue #587 Defect B) | 20 (`THERMAL_SOLAR_MIN_SAMPLES`) | `passive_decay` may also be running (different parameter) |

**Retired (Issue #587):** `fan_only_decay`/`k_vent` — confirmed dead code (the only live-forecast consumer was called with `ventilation_active=False` hardcoded, so `k_vent` never affected a real prediction). See [§k_vent / fan_only_decay retirement](#k_vent--fan_only_decay-retirement-issue-587).

**Mutual exclusion correction (Issue #587):** Prior to #587, `ventilated_decay` was never actually exclusive of fan influence — "any door/window sensor open" fired regardless of whether the fan/WHF was also running, so windows-open-with-fan-running was silently included in the same `k_vent_window` estimate as windows-open-with-fan-off. The three-way split above (`passive_decay` / `vent_window_decay` / `vent_fan_decay`, keyed on `_sensor_open` × `_fan_active`) is what makes the types *actually* mutually exclusive by trigger condition — each poll cycle, at most one of the three decay types can newly trigger for a given sensor/fan state combination.

**HVAC contamination rule:** When HVAC starts (`_start_hvac_observation()`), all non-HVAC types in `_pending_observations` are committed via `_commit_observation_if_sufficient()` (which commits if `len(samples) >= min_samples`, else abandons). The contamination check fires before the new HVAC observation is started.

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

### Rolling Decay Types (passive_decay, vent_window_decay, vent_fan_decay, solar_gain)

**Start:** `_start_decay_observation(obs_type)` fires from Section B of `_sample_all_observations()` when trigger conditions are first met. Creates the observation dict with `status="monitoring"`, empty `samples`, and `flags_at_start`.

**Sample accumulation:** Section A of `_sample_all_observations()` iterates all monitoring observations. Each type has a per-type decimation gate:

| Type | Gate constant | Interval |
|---|---|---|
| `passive_decay` | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | 5 min |
| `vent_window_decay` | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | 5 min |
| `vent_fan_decay` | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | 5 min |
| `solar_gain` | `THERMAL_SOLAR_SAMPLE_INTERVAL_S` | 5 min |

`fan_only_decay`'s `THERMAL_FAN_SAMPLE_INTERVAL_S` (2 min) gate is retired along with the type (Issue #587); both vent-split types use the same 5-min gate `ventilated_decay` used.

A sample is appended only when `elapsed_since_last_sample >= interval_s`. Each sample is a dict with `timestamp`, `indoor_temp_f`, `outdoor_temp_f`, `elapsed_minutes`. For `vent_window_decay`/`vent_fan_decay`, `solar_factor` (from `_solar_factor(now.hour)`) is also recorded at collection time. The hard cap per observation is `THERMAL_MAX_OBS_SAMPLES = 200`.

**Commit decision (`_evaluate_rolling_window`):** Called from Section C for each rolling type. Two-threshold logic:

1. `elapsed < THERMAL_ROLLING_MIN_WINDOW_MINUTES (30)` AND signal not sufficient → keep collecting (return `False`)
2. `elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES` AND `signal_sufficient=True` → commit via `_commit_rolling_window_obs()`
3. `elapsed >= THERMAL_ROLLING_MAX_WINDOW_MINUTES (240)` → commit if `len(samples) >= THERMAL_MIN_DECAY_SAMPLES + 1 (= 5)`, else abandon with reason `"max_window_exceeded"`
4. Between min and max, signal not sufficient → log and keep alive (samples trimmed to last 96 if > 96)

**Solar keep-alive guard (vent_window_decay / vent_fan_decay only):** During daytime hours (08:00–18:00), if `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`, `_vent_signal_sufficient` is forced to `False`, suppressing early commit even after the 30-min minimum. This prevents a 1-param commit before the 2-param OLS can distinguish `k_env` from `k_solar`. The 240-min hard cap overrides the guard. Identical logic for both vent-split types — implemented once in the shared `_evaluate_vent_split_observation()` method (Issue #587), not duplicated per type.

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

### compute_k_passive_endpoint()

*Added Issue #587 (Defect A fix). Replaces the old closed-form endpoint formula in both `_passive_endpoint_estimate` and the shared `_vent_endpoint_estimate`.*

**Signature:** `compute_k_passive_endpoint(window: list[dict], k_min: float, k_max: float) -> float | None`

**Input:** `window` — chart_log window entries (dicts with `ts` ISO-8601 string, `indoor` °F, `outdoor` °F), ordered by time, at least 2 entries. `k_min`/`k_max` — bisection bracket, typically `THERMAL_K_PASSIVE_MIN`/`THERMAL_K_PASSIVE_MAX`.

**Root cause fixed:** The prior closed-form formula (`ratio = (T_end − T_out_avg) / (T_start − T_out_avg); k = ln(ratio) / dt`) collapsed the window's outdoor readings into a single scalar average, assuming outdoor temperature held constant across the whole window. It doesn't — outdoor swings through the day — producing k values 1.3x–1.9x higher in magnitude than the ODE-consistent true rate on real overnight windows (confirmed via independent RK4 numerical integration against the same raw samples).

**Algorithm:**
1. Build a piecewise-linear interpolation of the window's real per-sample outdoor trace (`_interp_outdoor`).
2. Bisection root-find (60 iterations) the k value whose RK4 forward-integration (`_integrate_indoor_rk4`, ≥40 sub-steps, at least 4 per sample interval) of `dT/dt = k*(T − T_out(t))` — starting from `T_start = window[0]["indoor"]` — reproduces `T_end = window[-1]["indoor"]` at `t = dt_hours`.
3. If the residual doesn't change sign across `[k_min, k_max]`, no single-k decay model in the valid range reproduces the observed outcome — returns `None`.

**Key semantics — stays bookend-only on the indoor side:** only `T_start`/`T_end` ever feed the root-find target — no interior indoor samples are read. This preserves #141's original blip-immunity design constraint (a mid-window indoor sensor blip cannot skew the result). Only the *outdoor* series gains full per-sample resolution. On a 2-sample window, piecewise-linear interpolation over 2 points *is* the whole trace, so the result naturally matches the old closed-form answer in that degenerate case — a built-in regression guarantee, not a coincidence.

**Post-condition:** Returns the root-found k (hr⁻¹), or `None` if no root exists in `[k_min, k_max]` — plays the same regime-filter role the old `ratio<=0`/`ratio>=1.0` reject played. Callers (`_passive_endpoint_estimate`, `_vent_endpoint_estimate`) fall back to block-OLS (Estimator B) on `None`, exactly as before.

**Verified accuracy:** 0.02% error (new estimator) vs. 20.7% error (old formula) on a test window with known true k.

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

### compute_k_solar()

*Added Issue #587 (Defect B fix). Used by the `solar_gain` commit branch only — distinct from `compute_k_env_solar()`, which is the 2-param path shared by `vent_window_decay`/`vent_fan_decay`.*

**Signature:** `compute_k_solar(samples: list[dict], k_passive: float) -> tuple[float | None, float]`

**Input:** `samples` — `solar_gain` window samples (`indoor_temp_f`, `outdoor_temp_f`, `elapsed_minutes`), at least 2. `k_passive` — the cached envelope decay rate (from `thermal_model_cache["k_passive"]`) used as the subtrahend; never itself committed as part of this observation.

**Root cause fixed:** The `solar_gain` commit path previously attributed the *entire* observed daytime warming rate to solar gain, with no subtraction of the ordinary conductive contribution already explained by `k_passive * (T_in − T_out)`. The trigger condition never checked flow direction or distinguished radiative gain from ordinary conduction toward a warm outdoor.

**Computation — per-interval subtraction, mirroring `compute_k_active` exactly:**
```
k_solar_i = rate_i - k_passive * delta_i
```
averaged across all intervals, where `rate_i = (T_in[i+1] − T_in[i]) / dt_hours` and `delta_i = midpoint(T_in − T_out)` over the interval — the same pairing convention used throughout this spec's other OLS-adjacent functions.

**Bounds:** Result is clamped to `[0.0, THERMAL_K_SOLAR_MAX_F_PER_HR]`. A negative result (subtraction implying "negative solar gain," i.e. the window was entirely or more-than-entirely explained by conduction) is clamped to `0.0` rather than rejected — this is the mechanism that makes a pure-conduction window correctly report near-zero solar contribution instead of the old formula's full raw rate.

**Post-condition:** Returns `(k_solar, r_squared)`. `k_solar` is `None` when fewer than 2 samples are available or all intervals have non-zero/negative `dt`.

**Caller contract (`_commit_event_from_dict`, `solar_gain` branch):**
1. Reads `self._state.thermal_model_cache.get("k_passive")` — the current best envelope model.
2. If `k_passive is None` (fresh install, no envelope model learned yet), rejects with **`REJECT_NO_K_PASSIVE`** rather than silently falling back to the old raw-rate behavior — committing the raw rate would silently reintroduce the conduction-attribution bug this function exists to prevent.
3. Otherwise calls `compute_k_solar(samples, k_passive)` and commits its result. `obs["k_passive"]` stays `None` in the committed dict (unchanged contract — the subtrahend never leaks into the envelope EWMA, same invariant class as the bridge-proxy D21 rule).

**Verified accuracy:** a pure-conduction synthetic case that used to report 1.92°F/hr of fake "solar gain" now reports 0.0°F/hr.

**Observability:** an `[Issue #587]`-tagged INFO log line at commit time reports both `k_solar` (new, subtracted) and `raw_mean_rate` (what the old formula would have produced) side by side, for post-deploy monitoring (see [issue #854](https://github.com/gunkl/ClimateAdvisor/issues/854)).

---

## Commit Routing (_commit_event_from_dict)

`_commit_event_from_dict(event, force_grade, obs_type)` selects the commit path based on `obs_type`. Returns `(obs_dict | None, reject_code | None, r_squared | None)`.

| obs_type | Commit path | Cache keys written by `_update_thermal_model_cache` | `hvac_mode` tag in committed obs dict |
|---|---|---|---|
| `passive_decay` | 1-param OLS (`compute_k_passive`) on `event["samples"]`; min `THERMAL_MIN_DECAY_SAMPLES (4)` | `k_passive`, `avg_r_squared_passive`, `observation_count_passive` | `"passive"` |
| `vent_window_decay` | 2-param OLS (`compute_k_env_solar`) attempted first (when `sf_range >= 0.30`); if fails, 1-param (`compute_k_passive`) fallback | `k_vent_window`, `observation_count_vent_window`; `k_solar` additionally when `two_param=True` | `"vent_window"` |
| `vent_fan_decay` | Same commit path as `vent_window_decay` — identical logic, shared via the `_decay_tag_map`/2-param guard widening (Issue #587); the two types differ only in which fan state triggered the observation, not in how the samples are processed once collected | `k_vent_fan`, `observation_count_vent_fan`; `k_solar` additionally when `two_param=True` | `"vent_fan"` |
| `solar_gain` | `compute_k_solar(samples, k_passive)` — subtracts `k_passive`'s conductive contribution per interval before attributing the remainder to solar (Issue #587 Defect B); rejects with `REJECT_NO_K_PASSIVE` if no `k_passive` is cached | `k_solar`, `observation_count_solar` | `"solar"` |
| `hvac_heat` | 2-param path: `compute_k_passive(post_samples, pre_samples)` → `compute_k_active(active_samples, k_p)`; bridge proxy and single-point fallback applied when OLS returns None | `k_active_heat`, `k_passive` (when not from proxy), `observation_count_heat`, `swing_heat_f` | `"heat"` |
| `hvac_cool` | Same as hvac_heat; `session_mode = "cool"` | `k_active_cool`, `k_passive` (when not from proxy), `observation_count_cool`, `swing_cool_f` | `"cool"` |

**Retired (Issue #587):** `fan_only_decay` — 1-param OLS on `event["samples"]`, wrote `k_vent`/`observation_count_fan_only`, tagged `"fan_only"`. Confirmed dead code (its sole live-forecast consumer, `_simulate_indoor_physics_v3`, was always called with `ventilation_active=False` hardcoded) — removed outright, not deprecated. Old persisted `thermal_observations` entries with `hvac_mode == "fan_only"` are inert historical data; the frontend keeps a legacy label entry so old history still renders.

**Bridge proxy note:** the HVAC-commit bridge proxy (`learning.py`, D17/D26) stays keyed on `k_vent_window` only, never `k_vent_fan` — `k_vent_fan` mixes in forced air exchange, a worse proxy for envelope-only decay than window-only ventilation already is (Issue #587 Part 0 decision).

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

**Confidence grade thresholds — `confidence_k_passive`** (counts from `observation_count_passive` + `observation_count_heat` + `observation_count_cool`; `observation_count_vent_window`/`observation_count_vent_fan`/`observation_count_solar` are explicitly excluded — see [§k_vent / fan_only_decay retirement](#k_vent--fan_only_decay-retirement-issue-587) for why the old `observation_count_fan_only` inclusion was dropped rather than replaced):

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
| `"vent_window"` | `k_vent_window` (EWMA of `obs["k_passive"]`), `k_solar` (EWMA of `obs["k_solar"]` when `two_param=True`), `observation_count_vent_window` | `_envelope_modes = False` — k_passive EWMA does NOT run |
| `"vent_fan"` | `k_vent_fan` (EWMA of `obs["k_passive"]`), `k_solar` (EWMA of `obs["k_solar"]` when `two_param=True`), `observation_count_vent_fan` | `_envelope_modes = False` |
| `"solar"` | `k_solar` (EWMA of `obs["k_solar"]`), `observation_count_solar` | `_envelope_modes = False` |

**Retired (Issue #587):** `"fan_only"` mode — previously updated `k_vent` (EWMA of `obs["k_passive"]`) and `observation_count_fan_only`. The `elif mode in ("vent_window", "vent_fan"):` branch resolves the target field name (`k_vent_window`/`k_vent_fan`) and count field once via a shared `_ewma_update_field()` helper, rather than two copy-pasted branches — see [§k_vent / fan_only_decay retirement](#k_vent--fan_only_decay-retirement-issue-587).

**Swing update:** Applied for `"heat"` and `"cool"` modes only. Both `swing_heat_f` / `swing_cool_f` and their counters (`observation_count_swing_heat` / `observation_count_swing_cool`) are updated with the same alpha as the primary parameters.

---

## Rolling Window Constraints

| Constant | Value | Effect |
|---|---|---|
| `THERMAL_ROLLING_MIN_WINDOW_MINUTES` | 30 min | No commit attempt before this elapsed time; observation keeps accumulating regardless of signal |
| `THERMAL_ROLLING_MAX_WINDOW_MINUTES` | 240 min (4h) | Hard cap: forces commit if `len(samples) >= 5`, else abandons unconditionally; `skip_delta_guard=True` |
| `THERMAL_ROLLING_MIN_DELTA_T_F` | 0.2°F | Minimum indoor temperature range required to commit at min-window point (passive_decay, solar_gain); skipped for vent_window_decay and vent_fan_decay (`skip_delta_guard=True`) |
| `THERMAL_MIN_DECAY_SAMPLES` | 4 | OLS pair floor for rolling-window commits; `_commit_rolling_window_obs` requires `len(samples) >= 5` (= 4 + 1) to guarantee 4 pairs |

**Early commit condition:** `elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES` AND `signal_sufficient=True`. Signal is type-specific:
- `passive_decay`: `max(indoor_temps) − min(indoor_temps) >= THERMAL_ROLLING_MIN_DELTA_T_F`
- `vent_window_decay` / `vent_fan_decay`: indoor range check, additionally suppressed when solar keep-alive guard applies (daytime AND `sf_range < 0.30`); `skip_delta_guard=True` for both (identical to the old `ventilated_decay` behavior, now shared via `_evaluate_vent_split_observation()` rather than duplicated)
- `solar_gain`: indoor range check

**Solar keep-alive guard:** Active during hours 08:00–17:59 when `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`. Forces `_vent_signal_sufficient = False` for both `vent_window_decay` and `vent_fan_decay`, deferring early commit until `sf_range` meets threshold or the 240-min hard cap fires.

**Retired (Issue #587):** `fan_only_decay` previously used the same range check with `skip_delta_guard=True`.

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
    and _windows_recommended  # classification has a window schedule
    and not _hour_windows_open  # current hour is outside the open window
)
```
When the guard applies, ramp interpolation is used for that hour. When windows are NOT recommended (no schedule), the bridge runs for all hours without guard interference.

---

## k_vent / fan_only_decay retirement (Issue #587)

*Retired in Issue #587, alongside the [Defect A](#compute_k_passive_endpoint) and [Defect B](#compute_k_solar) calculation fixes and the `vent_window_decay`/`vent_fan_decay` split described throughout this spec. Tracking: [#587](https://github.com/gunkl/ClimateAdvisor/issues/587), [#851](https://github.com/gunkl/ClimateAdvisor/issues/851), [#852](https://github.com/gunkl/ClimateAdvisor/issues/852).*

### Dead-code confirmation

`k_vent` (from the retired `fan_only_decay` observation type) is confirmed genuinely dead: `_simulate_indoor_physics_v3`'s `ventilation_active` parameter was hardcoded `False` at its sole call site, so `k_vent` never affected a live forecast prediction — regardless of how many `fan_only_decay` observations had committed on a given install. On a real, live install, this manifested as a stale "Air exchange: 0.15/hr" value on the dashboard under a "Calibration: Strong" badge, while the same panel's rejection expander showed 0 kept / 32 skipped fan-only observations for the current 90-day window — the displayed number was carried over from early in the install's life and had nothing left feeding it.

The deeper reason `fan_only_decay` never worked in practice: it requires the fan running with windows **closed**, but a whole-house fan (WHF)'s entire purpose is pulling outside air through **open** windows. Homes that actually run a WHF essentially never produce a `fan_only_decay`-eligible window.

### Removal scope

Removed outright (not deprecated — per this codebase's #133 broken-state-trap discipline of never leaving old/parallel code paths behind): the `fan_only_decay` trigger and abort/commit blocks in `_sample_all_observations`; the `"fan_only"` branch in `_update_thermal_model_cache`; `k_vent`/`ventilation_active` from `_simulate_indoor_physics_v3`'s signature and its `k_eff` computation; `k_vent` from `get_thermal_model()`'s return dict and the coordinator's API-facing dict-builder; `OBS_TYPE_FAN_ONLY_DECAY` from all remaining maps/lists (`_build_learning_health`, `_commit_observation_if_sufficient`'s min-samples map, the sampling interval map, the restart-recovery map, the HVAC contamination tuple); the now-dead `THERMAL_FAN_MIN_SAMPLES`/`THERMAL_FAN_MIN_SIGNAL_F` constants. Old persisted `thermal_observations` entries with `hvac_mode == "fan_only"` are inert historical data — the frontend's `TYPE_LABELS` map keeps a legacy entry so old history still renders correctly, without any code path being able to write that string again.

The ODE's branch-selection condition (which observation-derived parameters are non-`None` enough to attempt physics prediction) uses the conservative form `if _k_solar is not None or _k_vent_window is not None or _k_vent_fan is not None:` rather than assuming `k_solar`/old-`k_vent` were never independently populated on any install.

### EWMA-no-reset decision

`k_vent_window`'s existing accumulated EWMA value is **not** hard-reset on upgrade, even though its *definition* narrowed from "windows open, any fan state" to "windows open, fan/WHF off specifically." Rationale: no reset-on-redefinition precedent exists anywhere in this codebase; unlike a genuinely wrong formula (Defect A), the pre-upgrade `k_vent_window` EWMA isn't "wrong" for its old wider definition — it's a blend of two now-distinct regimes that naturally re-weights toward the true fan-off rate as new, correctly-scoped observations arrive via EWMA's own recency-weighting. A hard reset would zero out months of accumulated signal for houses with sparse fan-off ventilation windows, regressing the gate bridge's physics eligibility for exactly the houses most reliant on it. The backfill-flag rename (see [§Symmetric Application](#symmetric-application)) already accelerates correction for the dominant (chart_log) contributor by forcing one fresh 30-day fan-off-only backfill pass on upgrade.

### Confidence-exclusion decision

`_grade_passive_confidence()` drops `observation_count_fan_only` from the envelope (`k_passive`) confidence count outright — it does **not** replace it with `observation_count_vent_window`/`observation_count_vent_fan`. The original `fan_only_decay` inclusion was justified because fan-only-no-windows genuinely resembles passive decay with a small forced-convection perturbation — a defensible approximation. Window-open decay (and especially fan-driven ventilation) are **not** close proxies for envelope-only decay: they measure a materially larger, different rate (the same premise behind the gate-bridge guard's own over-prediction concern, and behind the D17 bridge-proxy's forced `"low"` confidence grade). Folding either vent count into *envelope* confidence would be scientifically wrong in a way the original fan_only inclusion was not.

This is a real, intentional behavior change: houses that previously got confidence credit from `fan_only_decay` observations will show a slightly lower `confidence_k_passive` after upgrade. This is expected, not a regression — flagged explicitly in the CHANGELOG.

### Forecast-ODE scope boundary — k_vent_fan is learned/displayed but not wired into per-hour forecast selection

`k_vent_fan` is learned (via `vent_fan_decay` observations) and displayed (dashboard, `get_thermal_model()`, engine diagnostics) exactly like `k_vent_window`. However, `_build_predicted_indoor_future()`'s per-hour selection logic is **unchanged** from before Issue #587: window-open forecast hours still substitute `k_vent_window` only (`_k_passive_for_hour = _k_vent_window if (_hour_windows_open and not _k_passive_via_bridge) else _k_passive`, `coordinator.py` ~L10647-10650). `k_vent_fan` has no equivalent per-hour substitution slot.

**This is a deliberate scope boundary, not an oversight.** There is genuinely no forecast-time fan/WHF schedule to key a per-hour fan-active computation off — `classification`'s window fields are `window_open_time`/`window_close_time`/`windows_recommended` only; no fan/WHF-schedule field exists anywhere in the classification or config model. Retrofitting a forecast fan-schedule concept (predicting *when* the WHF will run, not just *that* it has run historically) is its own future "dedicated design pass," out of scope for #587. A regression test confirms `k_vent_fan`'s mere presence in the thermal model dict doesn't change any current forecast output — its only live consumers today are display surfaces, not the ODE.

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

*Added in v0.3.45 (Issue #146). Applies to `_run_passive_chart_log_fit`, `_run_vent_window_chart_log_fit`, and `_run_vent_fan_chart_log_fit` (the latter two are thin wrappers around a shared `_run_vent_chart_log_fit_impl`, since Issue #587's split of `_run_ventilated_chart_log_fit`).*

### Motivation

Thermostat quantization defeats the consecutive-pair OLS estimator. The raw samples produce near-zero R² while the early endpoint estimator converged too slowly, yielding estimates 3–5× too large.

| Metric | Value | Impact |
|---|---|---|
| Overnight passive drift | ≈0.25°F/hr | Produces 0.021°F per 5-min interval |
| Thermostat quantization | 1°F | Dwarfs the signal; nearly all rate pairs are zero |
| Consecutive-pair R² | ≈0.02 | Almost all windows rejected |
| Early convergence (8 windows @ α=0.05) | 66.3% weight still on prior (`(1-0.05)^8`) | Slow adaptation to true value |
| Resulting k estimates | 3–5× too large | Overnight predicted indoor dips 8–10°F below actual |

The dual-estimator framework runs both methods on every overnight window and selects per-night based on data quality. Backfill v2 reprocesses 30 days to converge via EWMA, reaching ≈99% weight in 30 medium-grade updates.

### Estimator A — Endpoint

*Updated Issue #587 (Defect A fix) — see [§OLS Functions — compute_k_passive_endpoint](#compute_k_passive_endpoint) for the full spec.*

`compute_k_passive_endpoint()`: bisection root-find of the k whose RK4 forward-integration of `dT/dt = k*(T − T_out(t))` — against the window's real per-sample outdoor trace, piecewise-linearly interpolated — reproduces the observed `T_end` from `T_start`.

- **No longer** the old closed-form `k = ln((T_end − T_out_avg) / (T_start − T_out_avg)) / Δt_hours`, which assumed outdoor temperature was constant across the window and was measurably biased (1.3x–1.9x) on real overnight windows — see the linked spec section for the full before/after.
- Uses only the bookend indoor readings of the window (`T_start`/`T_end`); immune to mid-window indoor sensor blips that corrupt interior samples — this part of #141's original design is unchanged
- Natural regime filter: no root in `[k_min, k_max]` ⇒ `None`, playing the same role the old `ratio<=0`/`ratio>=1.0` reject played
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

`_run_vent_window_chart_log_fit`/`_run_vent_fan_chart_log_fit` (both thin wrappers around `_run_vent_chart_log_fit_impl(fan_state=..., hvac_mode_tag=..., backfill=...)`, Issue #587) follow the same structure: extract windows filtered by `fan_state` with solar guard → per-window run A + B + `_select_estimator` → `record_thermal_observation`. The vent-split paths write to `k_vent_window`/`k_vent_fan` respectively rather than `k_passive`, but the estimator machinery (`compute_k_passive_blocks`, `_vent_endpoint_estimate` — renamed from `_ventilated_endpoint_estimate`, same shared body as `_passive_endpoint_estimate` uses, now fixed by `compute_k_passive_endpoint()` — `_select_estimator`) is reused unchanged and identically by both fan-state variants.

Backfill flags renamed on upgrade (deliberate, not just additive — see [§k_vent / fan_only_decay retirement](#k_vent--fan_only_decay-retirement-issue-587)): `_vent_k_backfilled`/`_vent_k_backfill_v2` → `_vent_window_k_backfilled`/`_vent_window_k_backfill_v2`, plus new `_vent_fan_k_backfilled`/`_vent_fan_k_backfill_v2`. The rename forces a fresh one-time 30-day backfill under the new fan-off-only filter on upgrade — idempotent, and desirable since the old flag's backfill ran under the wider (fan-state-agnostic) definition.

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
if effective_hour < THERMAL_SOLAR_DAYTIME_START_H:  # 8
    return 0.0
if effective_hour >= THERMAL_SOLAR_DAYTIME_END_H:  # 18
    return 0.0
# sin curve over [8, 18), peak at effective_hour = 13
angle = (
    (effective_hour - THERMAL_SOLAR_DAYTIME_START_H) / (THERMAL_SOLAR_DAYTIME_END_H - THERMAL_SOLAR_DAYTIME_START_H) * π
)
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

**Scope:** `get_engine_status()` in `learning.py`; REST endpoint in `api.py`; dashboard card in `index.html`; AI context in `ai_skills_context.py`; CLI tool `tools/engine_status.py`.

### `get_engine_status()` Return Shape

`learning.get_engine_status() → dict`

```python
{
    "k_passive": {
        "active": bool,  # True when k_passive is not None
        "value": float | None,  # current thermal_model_cache["k_passive"]
        "confidence": str,  # "none" | "low" | "medium" | "high"
    },
    "k_solar": {
        "active": bool,
        "value": float | None,
        "confidence": str,  # derived from observation_count_solar (same grade thresholds as k_passive)
    },
    "solar_phase_offset_h": {
        "active": bool,  # True when solar_phase_offset_h is not None
        "value": float | None,
    },
    "k_vent_window": {
        "active": bool,
        "value": float | None,
    },
    # "k_vent_fan" is not yet exposed via get_engine_status() — it is available on
    # get_thermal_model()'s output dict but this dashboard-facing engine list has not
    # been extended for it (display-only field, no forecast-eligibility implication;
    # see the forecast-ODE scope boundary note under k_vent/fan_only_decay retirement).
    "k_active_hvac": {
        "active": bool,  # True when k_active_heat or k_active_cool is not None
        "value": {"heat": float | None, "cool": float | None},  # k_active_heat and k_active_cool
    },
    "ode_version": str,  # "v3" when k_solar or k_vent_window/k_vent_fan present; "basic" otherwise. The
    # old bare "k_vent" (fan_only_decay) key is retired (Issue #587) and never appears.
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
| AI investigator | `ACTIVE_PREDICTION_ENGINES` section prepended to activity context in `ai_skills_context.py`; plain-text table for LLM consumption |
| CLI tool | `tools/engine_status.py` reads learning DB via SSH (same pattern as `tools/learning_db.py`), prints formatted table; `--history` flag also tails `ha_logs.py --thermal` and greps for engine activation events |

### `get_thermal_model()` additions

`solar_phase_offset_h` is included in the `get_thermal_model()` return dict. Downstream consumers (`coordinator.py`, `api.py`, `ai_skills_context.py`) read from this output, not from `thermal_model_cache` directly.

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

Non-HVAC types (passive_decay, vent_window_decay, vent_fan_decay, solar_gain) use the generic `obs.get("samples", obs.get("active_samples", []))` path.

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
| `max_window_exceeded` on rolling types | Passive/vent observation ran for 4 hours without meeting signal threshold. | Not a bug — the 240-min hard cap forces a commit or abandon when signal is never sufficient. |
| `REJECT_OLS_BAD_FIT` (R² < 0.20) on passive_decay | Consecutive 5-min pair OLS on 1°F thermostat data structurally fails; see §Dual Estimator Framework. | The chart_log dual-estimator (Estimator B) handles this. Active passive_decay observations supplement with real-time data. |

---

## Invariants

The following conditions must always hold after a successful commit and EWMA update:

1. **k_passive sign:** Every value written to `cache["k_passive"]` via `_update_thermal_model_cache` is negative (`k_p < 0`). The `REJECT_OLS_WRONG_SIGN` check in `compute_k_passive` enforces this before any value reaches the cache. The only exception is the bridge proxy path in `_commit_event_from_dict` — but in that path `k_passive=None` is written to the obs dict (D21), so the cache is never updated with the proxy value.

2. **k_vent_window / k_vent_fan sign:** Both are always ≤ 0 in cache when valid — same 1-param/2-param OLS bounds apply to `vent_fan_decay` as to `vent_window_decay` (Issue #587). The bridge activation check (`_k_vent_window <= 0`) reads `k_vent_window` only. A value of exactly 0.0 is physically valid (perfectly inert home with zero ventilation effect) and produces a flat ODE prediction.

3. **k_solar sign:** `k_solar` is always non-negative. The bounds check `0.0 <= k_solar <= THERMAL_K_SOLAR_MAX_F_PER_HR` in `_commit_event_from_dict` and the mean-rate sign check (`if mean_rate < 0: reject`) for solar_gain observations enforce this.

4. **Separation of envelope and ventilation:** The guard `_envelope_modes = mode not in ("vent_window", "vent_fan")` in `_update_thermal_model_cache` ensures that `vent_window` and `vent_fan` observations never write to `cache["k_passive"]`. Only `"heat"`, `"cool"`, and `"passive"` modes update the envelope decay estimate. (Retired Issue #587: the old guard was `mode not in ("fan_only", "ventilated")`.)

5. **Rejection code exclusivity:** `compute_k_passive` returns exactly one of `(k_passive, rejection_code)` as non-None. The function never returns both `k_passive` and a `rejection_code` as non-None simultaneously.

6. **Obs cap:** `thermal_observations` list in `LearningState` never exceeds `THERMAL_OBS_CAP (200)` entries. The 90-day rolling trim runs first; the hard cap enforces the absolute maximum.

7. **Rejection log cap:** Each obs_type bucket in `rejection_log` is capped at 100 entries. Both `_abandon_observation()` in coordinator and `load_state()` enforce this cap.

8. **Bridge does not contaminate k_passive EWMA:** When `_k_p_from_proxy=True` in `_commit_event_from_dict`, `obs["k_passive"] = None` is set before calling `record_thermal_observation()`. This ensures `_update_thermal_model_cache` sees `k_p = None` and skips the `k_passive` EWMA update.

9. **ODE k_passive must be negative for exponential decay:** `_simulate_indoor_physics` and `_simulate_indoor_physics_v3` use `exp(k_passive * dt_hours)`. With `k_passive < 0` this decays toward `t_outdoor`; with `k_passive = 0` the division-by-zero branch uses linear extrapolation (`t_start + q * dt_hours`). The bridge allows `k_vent_window = 0.0` exactly, which routes to this linear branch — correct for a perfectly inert home.
