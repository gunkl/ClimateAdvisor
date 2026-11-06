<!-- Nav: ← [Thermal Model v3](thermal-model-v3-spec.md) | → [predictor_v4.py](../custom_components/climate_advisor/predictor_v4.py) [fit#L781](../custom_components/climate_advisor/predictor_v4.py#L781) [predict#L1085](../custom_components/climate_advisor/predictor_v4.py#L1085) | ↔ [v4 Shadow](v4-shadow-spec.md) [Chart State Log](chart-log-spec.md) [Forecast Archive](forecast-archive-spec.md) -->

# Predictor v4 - Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does v4 predict and for whom? | The temperature each configured room sensor will read 1-24 h ahead, learned from the home's own chart_log. On Zone 1 (real data) its 4 h MAE on the reading CA shows is about 0.6-0.8 F against 1.5 F for "nothing changes" and 1.6 F for v3. | [Model](#model-equations), `predictor_v4.py#L781` |
| Does anything decide with v4? | No. R1 is shadow only: nothing on a decision path calls `predict()`; v3 still drives every decision and the Status chart. | [Scope](#scope), [v4 Shadow: invariant](v4-shadow-spec.md#nothing-on-a-decision-path-reads-v4) |
| What is the state, and what are the inputs? | Primary node `H` (the thermostat's room reading), hidden thermal mass `M`, one node `B_j` per extra sensor. Inputs per 15-min step: outdoor temperature, sun, windows open, fan running, heating duty, cooling duty. | [Model](#model-equations) |
| How is the thermostat modelled? | Replayed from the recorded heating/cooling duty when fitting; emulated when predicting, from the plan's heat/cool setpoints with learned start offsets, burst length and reading quantisation. | [Thermostat](#thermostat-learning-and-emulation), `predictor_v4.py#L408` |
| When is `predict()` allowed to answer? | Never when `confidence == "none"` (< 3 days of samples, diverged fit, or no 10 % skill over "no change" at 4 h on the training data). Callers then show/score v3 only. | [Confidence Gates](#confidence-gates), `predictor_v4.py#L82-L86` |
| How is the forecast corrected? | 24 learned hourly biases (actual - forecast, interpolated around the clock) plus today's current miss decaying with a learned time constant, applied INSIDE `predict()` to the RAW forecast. | [Forecast Correction](#forecast-correction), `predictor_v4.py#L488` |
| How long does a fit take? | Typically 0.2-1.5 s for 30 days of data on a laptop, worst case about 3.5 s (60 iterations, 160 anchors, 2 sensors). `predict()` is about 1 ms. | [Performance](#performance) |
| Where does the fitted model live, who calls `fit()`/`predict()` and when? | `v4_shadow.py` and the coordinator call them: nightly fit 03:15, predict every 30-min cycle, model in `climate_advisor_v4_model_<id>.json`, predictions in `climate_advisor_v4_archive_<id>.json`. | [v4 Shadow Spec](v4-shadow-spec.md) |
| How is v4 scored against v3 and "no change"? | `GET /api/climate_advisor/prediction_accuracy` (live archive) and `tools/predict_backtest.py` (walk-forward history; DP-8 gate). | [REST API](rest-api.md#get-apiclimate_advisorprediction_accuracy-issue-1230-r1), [Debugging Guide](09-DEBUGGING-GUIDE.md#prediction-backtest-toolspredict_backtestpy-issue-1230-r1) |

## Scope

- **File:** `custom_components/climate_advisor/predictor_v4.py` (pure Python; `math`, lists, dataclasses; no numpy/scipy, no `hass` import)
- **Entry points:** `fit()`, `predict()`, `step_mass()`, `samples_from_chart_log()`, `learn_thermostat()`, `learn_forecast_correction()`, `to_dict()` / `from_dict()`, `rollout()`
- **Integration step:** 15 minutes (`V4_STEP_MINUTES`); all temperatures internal degrees F, time in hours inside the equations.
- **Not covered here:** where the fitted model is stored and when it is fitted, the per-cycle inputs, the v4 prediction archive and the three files ([v4 Shadow Spec](v4-shadow-spec.md)); the REST endpoint ([REST API](rest-api.md#get-apiclimate_advisorprediction_accuracy-issue-1230-r1)); the walk-forward backtest `tools/predict_backtest.py` ([Debugging Guide](09-DEBUGGING-GUIDE.md#prediction-backtest-toolspredict_backtestpy-issue-1230-r1)); the preview page ([Architecture Reference](02-ARCHITECTURE-REFERENCE.md#prediction-accuracy-preview-page-issue-1230-r1)). v3 stays the decision model and is [Thermal Model v3](thermal-model-v3-spec.md). v4 trains on the [Chart State Log](chart-log-spec.md) rows and, when available, [Forecast Archive](forecast-archive-spec.md) snapshots.

## Model Equations

With `dt = 0.25 h`, inputs of the step *ending* at each grid point (explicit Euler, state before the update on the right-hand side):

```
sun(hour) = sin((e - 8) / 10 * pi)   for e = hour - phase in [8, 18), else 0      (local clock hour)
leak      = a_o + a_w * windows + a_f * fan
dH        = leak * (out - H) + c_hm * (M - H) + s * sun + q_h * u_heat - q_c * u_cool
dM        = c_mh * (H - M)                                  # hidden thermal-mass node
dB_j      = b_h * (H - B_j) + (b_o + b_w * windows) * (out - B_j) + s_b * sun
            + q_b * (u_heat - (q_c / q_h) * u_cool)         # one per extra sensor; never feeds back into H
```

- `u_heat` / `u_cool` are the fraction of the 15-minute step the thermostat heats / cools (0..1), so a 10-minute burst is 0.67.
- Cooling is the symmetric counterpart of heating with its own gain `q_c` (a subtraction); extra sensors receive the same share `q_b` of the cooling as of the heating, scaled by `q_c / q_h`.
- The primary node is the thermostat's room reading (the finer companion sensor when CA uses it, 0.9.2+). Nodes are driven by `H` only, which is why the Jacobian for node parameters re-rolls only that node.
- Fan: `fan_running` from chart_log (whole-house fan or blower); the blower and the whole-house fan are not distinguished by chart_log, so `a_f` is the average effect of "fan running".

### Parameters and bounds

| Name | Unit | Start | Low | High | Meaning |
|---|---|---|---|---|---|
| `a_o` | 1/h | 0.03 | 0.001 | 0.5 | envelope leak to outdoor |
| `a_w` | 1/h | 0.10 | 0 | 1 | extra leak with windows open |
| `a_f` | 1/h | 0.30 | 0 | 2 | extra leak with the fan running (capped so Euler at 15 min stays stable) |
| `c_hm` | 1/h | 0.6 | 0 | 3 | air <- mass coupling |
| `c_mh` | 1/h | 0.2 | 0 | 1 | mass <- air coupling |
| `s` | F/h | 0.7 | 0 | 5 | sun gain at the peak of the sine |
| `phase` | h | 3.0 | 0 | 5 | sun sine shift (peak at 13 + phase) |
| `q_h` | F/h | 8.0 | 0.5 | 40 | heating gain while on |
| `q_c` | F/h | 8.8 | 0.5 | 40 | cooling gain while on |
| `b_h` | 1/h | 0.4 | 0 | 3 | extra node <- house air |
| `b_o` | 1/h | 0.02 | 0 | 0.5 | extra node <- outdoor |
| `b_w` | 1/h | 0.10 | 0 | 1 | extra node <- outdoor, windows open |
| `s_b` | F/h | 0.1 | 0 | 3 | extra node sun gain |
| `q_b` | F/h | 4.0 | -5 | 40 | share of the HVAC gain reaching the extra node |

Parameters with no data behind them are **frozen** at their start (or warm-start) value and listed in `fit["frozen"]`: `a_w`/`b_w` when no window-open step is in the anchors, `a_f` when the fan never ran, `q_h` when heating was never seen, `q_c` when cooling was never seen (then tied to `q_h * 6.13/5.55`, production's learned cool/heat ratio; if only cooling was seen, `q_h = q_c / (6.13/5.55)`), `q_b` when neither mode was seen. "Seen" = at least 8 anchor steps (2 h) with that duty.

### Hidden-mass filter

The mass state at an anchor is not assumed equal to `H`: it is the filter `M <- M + c_mh * dt * (H_measured - M)` run over the contiguous history before the anchor (reset to `H` at a break). `step_mass()` is the same filter for the live cycle; callers carry `mass_f` between 30-minute cycles (it advances one 15-min step per call, so the coordinator calls it per elapsed step). `predict(mass_f=None)` starts at the primary reading.

## Thermostat Learning and Emulation

`learn_thermostat(samples, primary)` returns `V4Thermostat(on_below_f, on_above_f, burst_minutes, reading_step_f, burst_cool_minutes)`:

- **Start:** a sample whose heating (cooling) duty is > 0 while the previous contiguous sample's was 0. The offset is `setpoint - min(reading_prev, reading_now)` (`max(...) - setpoint` for cooling). Starts where the setpoint in force changed between the previous two samples or the start sample (about the 10-minute window of the plan) are skipped. Median of at least 5 starts, clamped to [0, 4] F; otherwise 1.0.
- **Burst:** median total active minutes of a run (sum of duty x 15 min), at least 5 runs, clamped to [5, 90] min (heat) / [5, 120] (cool); default 10. `burst_cool_minutes` is `None` when cooling runs were not observed (then `burst_minutes` is used).
- **Reading quantisation:** `reading_step_f` = 1.0 when at least 98 % of the primary readings are whole numbers, 0.5 for half-degree, else 0 (fine). The thermostat acts on the rounded reading: `round(H / step) * step`.
- **Emulation (`rollout`, predict path):** at each step, if no burst is in progress and a setpoint exists: heat starts when `reading <= heat_sp - on_below`; cooling when `reading >= cool_sp + on_above`; the burst then runs `burst_minutes` (carrying across steps if longer than 15 min). When the start threshold is exceeded by at least 1 F (`_STAY_ON_MARGIN_F`) the burst is extended to the full step (a recovering house stays on). No setpoint = HVAC off.
- chart_log `setpoint` is null in older Zone 1 history (before September 2026); starts cannot be learned from those rows, so a home with only such history keeps the defaults.

## Forecast Correction

- `hourly_bias_f[h]` = mean(actual outdoor - forecast) over samples in local hour `h`, shrunk by `n / (n + 4)`; the value for hour `h` is centred at `h + 0.5` and linearly interpolated around the clock. Needs at least 48 pairs overall, otherwise all zeros.
- `nowcast_tau_h`: the candidate in {1, 2, 3, 4, 6, 8, 12} h that best predicts the bias-corrected error's persistence at 1-12 h leads (default 3 h).
- `predict()` corrected outdoor: `raw + bias(hour) + miss_now * exp(-lead_h / tau)` where `miss_now = outdoor_now - (forecast_now + bias(now))`. The nowcast term needs the optional `outdoor_now_f` and `forecast_now_f` arguments.
- Forecast source when learning: with `archive_snapshots`, the value of the snapshot issued about 6 h earlier for that hour; otherwise the chart_log `pred_outdoor` of the row.

## Fit

Bounded Levenberg-Marquardt on multi-step rollout residuals:

1. Parameters are scaled to [0, 1] by their bounds and clipped to them after every step.
2. **Anchors:** every sample with a primary reading from which at least 4 contiguous steps with outdoor temperature exist; capped at `max_anchors = 160` by an even stride (deterministic).
3. **Rollout:** from the anchor reading, mass-filter state and recorded `heat_duty` / `cool_duty` (replay) with the observed outdoor, window and fan inputs, up to `horizon_h` (12 h = 48 steps) or the first break.
4. **Residuals:** model - actual for the primary and every extra sensor at lead steps 1-4 and then every 2nd step; weight `1.0 -> 0.5` linearly with lead.
5. **Jacobian:** numeric forward difference (step 0.001 in scaled units); node parameters re-roll only their own node from the cached house trajectory.
6. **Update:** `(J^T J + lambda * diag(J^T J)) dz = -J^T r`, `lambda` /3 on success, x4 on failure (8 tries); stops when the relative cost drop is under 1e-4 twice, the step is under 1e-5, no step helps, or `max_iterations` (default 60).
7. Deterministic: no randomness, no wall clock. `warm_start` seeds the parameters from the previous night's model.
8. `fit()` never raises on thin or degenerate data; it returns a model with `confidence == "none"` and `fit["reason"]`.

`fit` dict: `n_anchors`, `span_days` (first to last sample), `sample_days` (samples x 15 min), `mae_by_lead` / `flat_mae_by_lead` (`"1h"`, `"4h"`, `"12h"`; replay MAE on the training anchors and the "no change" MAE on the same), `per_sensor` (4 h MAE each), `skill_vs_flat`, `iterations`, `cost`, `heat_seen`, `cool_seen`, `frozen`, `horizon_h`, `reason`.

## Confidence Gates

| Confidence | Condition | `predict()` |
|---|---|---|
| `none` | fewer than 3 days of samples (`sample_days < 3`), or fewer than 12 anchors, or a non-finite fit, or training MAE at 4 h not at least 10 % below "no change" | returns `None` |
| `low` | otherwise, but fewer than 10 days of samples, or heating or cooling not yet observed (at least 2 h of duty each) | predicts |
| `ok` | at least 10 days, both modes observed, skill gate passed | predicts |

`predict()` also returns `None` when the primary reading is missing or `inputs` is empty. Sensors whose own reading is missing are left out of the result.

## Pre-conditions

1. `samples` are ordered by time on the 15-minute grid; consecutive timestamps exactly 900 s apart are one continuous run (epoch comparison, DST-safe). A larger step is a break.
2. `samples_from_chart_log` rows are raw chart_log entries (`ts`, `hvac`, `fan`/`fan_running`, `indoor`, `outdoor`, `windows_open`, `pred_outdoor`, `setpoint`, optional `indoor_sensors`).
3. `predict()` inputs are consecutive 15-minute grid points after `now` (`grid_steps()` builds them DST-safely); `outdoor_f` is the RAW forecast.

## Post-conditions

1. `fit()` returns a `V4Model` whose parameters lie within the bounds above; `primary` is `sensors[0]`.
2. `to_dict()` output is JSON-serialisable (no NaN); `from_dict(to_dict(m))` reproduces `to_dict(m)` exactly.
3. `predict()` returns, for each sensor with a reading, one `(ts, temp_f)` per input in the same order.

## Invariants

1. Pure and headless: no I/O, no clock, no logging side effects; same inputs give the same model and the same prediction.
2. Nothing in R1 reads v4 for a decision; a v4 failure must never affect a cycle (callers catch).
3. A sensor node never changes the primary node's trajectory.
4. A mode that was never observed never produces an unbounded gain: its parameter is frozen and tied to the other by the production cool/heat ratio.
5. Time is compared by epoch (`timestamp()`), never by subtracting aware datetimes that share a `tzinfo` (wall-clock arithmetic breaks on DST days).

## Error Conditions

| Failure | Handling | Caller receives |
|---|---|---|
| Too little data / under 3 days / no skill | `confidence = "none"`, `fit["reason"]` set | model, `predict()` -> `None` |
| Numeric failure in the fit | caught, reason `fit failed: <type>` | model with `confidence = "none"` and default parameters |
| Unknown schema or malformed dict | `from_dict` -> `None` | `None` (callers refit) |
| Missing primary reading | `predict` -> `None` | `None` |
| Gap in chart_log over 2 h | samples inside the gap are not emitted (timestamps jump); rollouts stop at the jump | shorter anchors |

## Schema (`schema_version = 1`)

```
{"schema_version": 1, "fitted_at": iso, "primary": entity_id, "sensors": [primary, ...],
 "house": {a_o, a_w, a_f, c_hm, c_mh, s, phase, q_h, q_c},
 "nodes": {entity_id: {b_h, b_o, b_w, s_b, q_b}},                 # every non-primary sensor
 "thermostat": {on_below_f, on_above_f, burst_minutes, reading_step_f, burst_cool_minutes | null},
 "forecast": {"hourly_bias_f": [24 floats], "nowcast_tau_h": float},
 "fit": {...see Fit...}, "confidence": "none" | "low" | "ok"}
```

`from_dict` returns `None` on a wrong/missing `schema_version`, a non-mapping, non-string or duplicated sensor ids, `sensors[0] != primary`, any missing/non-finite/out-of-bounds parameter, a missing node, a non-positive burst, a bias list that is not 24 finite floats, `nowcast_tau_h <= 0`, an unknown confidence, or a non-string `fitted_at`. A schema change bumps `V4_SCHEMA_VERSION`; older files are refitted, not migrated.

## Deviations From The Contract (additive)

- `V4Sample.heat_duty` / `cool_duty` (default `None`): fraction of the 15 minutes ending at `ts` spent heating / cooling, integrated from the chart_log row timeline. Exact burst accounting; without them the instant `hvac_action` is used.
- `V4Thermostat.burst_cool_minutes` (default `None`).
- `predict(..., outdoor_now_f=None, forecast_now_f=None)`: needed for the nowcast term.
- `fit(..., max_anchors=160, warm_start=None)`.
- `step_mass(model, *, mass_f, readings, sample=None)`: `sample` optional.
- `predict()` omits a sensor whose reading is missing instead of inventing a start value.
- Helpers: `summary_line(model)` (INFO line for the nightly fit), `grid_steps(start, n)`.

## Performance

Measured on the development machine (CPython 3.14, one core): 30 days of 15-min samples, two sensors, 160 anchors, 12 h horizon: 0.2-1.5 s typical (5-40 iterations), 3.3 s for 60 iterations (about 55 ms per iteration). A Raspberry Pi 4 is roughly 4-6x slower: about 5-8 s typical, 15-25 s worst case, inside the 60 s budget. `predict()` for 96 steps and 2 sensors costs about 1 ms. A nightly refit with `warm_start` converges in a few iterations.

## Code Reference

`custom_components/climate_advisor/predictor_v4.py`:

- Constants and bounds: `V4_STEP_MINUTES` L35, `HOUSE_BOUNDS`/`NODE_BOUNDS` L48-L65, `HOUSE_START`/`NODE_START` L66-L79, confidence gates `GATE_*` L82-L86
- Dataclasses: `V4Sample` L96, `V4Input` L119, `V4Thermostat` L131, `V4ForecastCorrection` L141, `V4Model` L147, `V4Roll` L161
- Integrator: `rollout()` L251, `_roll_house()` L284, `_roll_node()` L369
- Learning: `learn_thermostat()` L408, `learn_forecast_correction()` L488, `_corrected_outdoor()` L541
- Fitter: `_Problem` L577, `fit()` L781, `_fit_problem()` L845 (gates at L1032-L1051)
- Live: `step_mass()` L1067, `predict()` L1085
- Serialisation: `to_dict()` L1157, `from_dict()` L1194, `summary_line()` L1266
- Chart-log adapter: `samples_from_chart_log()` L1290 and helpers `_duty()` L1421, `_mode_for()` L1411, `_archive_forecast()` L1480; `grid_steps()` L1497
- Callers: `v4_shadow.run_fit` / `run_cycle` ([v4 Shadow Spec](v4-shadow-spec.md#code-reference)), `tools/predict_backtest.py`
- Tests: `tests/test_predictor_v4.py` (synthetic house with known parameters, gates, serialisation, DST, gaps, chart_log), `tests/test_predictor_v4_real.py` (Zone 1 replay, `tests/fixtures/v4_zone1_replay.json`), `tests/test_v4_shadow_real_engine.py` (the shadow wiring against the real engine)
