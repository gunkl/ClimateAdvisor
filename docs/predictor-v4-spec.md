<!-- Nav: ← [Thermal Model v3](thermal-model-v3-spec.md) | → [predictor_v4.py](../custom_components/climate_advisor/predictor_v4.py) [fit#L781](../custom_components/climate_advisor/predictor_v4.py#L781) [predict#L1085](../custom_components/climate_advisor/predictor_v4.py#L1085) | ↔ [v4 Shadow](v4-shadow-spec.md) [Chart State Log](chart-log-spec.md) [Forecast Archive](forecast-archive-spec.md) -->

# Predictor v4 - Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does v4 predict and for whom? | The temperature each configured room sensor will read 1-24 h ahead, learned from the home's own chart_log. On Zone 1 (real data) its 4 h MAE on the reading CA shows is about 0.6-0.8 F against 1.5 F for "nothing changes" and 1.6 F for v3. | [Model](#model-equations), `predictor_v4.py#L781` |
| Does anything decide with v4? | Only when v4 is the PRIMARY engine (default, Issue #1230 R6b): the ceiling guard and nat-vent plan read its no-HVAC baseline path (`run_cycle(baseline=True)`), the rate decisions read `control_rates()`. As the comparison engine, or when its fit is `none`, nothing decides with it. | [Control feed](prediction-engines-spec.md#control-feed-r6b), [v4 Shadow: invariant](v4-shadow-spec.md#nothing-on-a-decision-path-reads-v4) |
| What is the state, and what are the inputs? | Primary node `H` (the thermostat's room reading), hidden thermal mass `M`, one node `B_j` per extra sensor. Inputs per 15-min step: outdoor temperature, sun, windows open, fan running, heating duty, cooling duty. | [Model](#model-equations) |
| How is the thermostat modelled? | Replayed from the recorded heating/cooling duty when fitting; emulated when predicting, from the plan's heat/cool setpoints with learned start offsets, burst length and reading quantisation. | [Thermostat](#thermostat-learning-and-emulation), `predictor_v4.py#L408` |
| When is `predict()` allowed to answer? | Never when `confidence == "none"` (< 3 days of samples, diverged fit, or no 10 % skill over "no change" at 4 h on the training data). Callers then show/score v3 only. | [Confidence Gates](#confidence-gates), `predictor_v4.py#L82-L86` |
| How is the forecast corrected? | 24 learned hourly biases (actual - forecast, interpolated around the clock) plus today's current miss decaying with a learned time constant, applied INSIDE `predict()` to the RAW forecast. | [Forecast Correction](#forecast-correction), `predictor_v4.py#L488` |
| How long does a fit take? | Typically 0.2-6 s for 30-60 days of data on a laptop (100-iteration cap, 160 anchors, 2 sensors; a warm refit runs a second start). `predict()` is about 1 ms. | [Performance](#performance) |
| Where does the fitted model live, who calls `fit()`/`predict()` and when? | `v4_shadow.py` and the coordinator call them: nightly fit 03:15, predict every 30-min cycle, model in `climate_advisor_v4_model_<id>.json`, predictions in `climate_advisor_v4_archive_<id>.json`. | [v4 Shadow Spec](v4-shadow-spec.md) |
| How is v4 scored against v3 and "no change"? | `GET /api/climate_advisor/prediction_accuracy` (live archive) and `tools/predict_backtest.py` (walk-forward history; DP-8 gate). | [REST API](rest-api.md#get-apiclimate_advisorprediction_accuracy-issue-1230-r1), [Debugging Guide](09-DEBUGGING-GUIDE.md#prediction-backtest-toolspredict_backtestpy-issue-1230-r1) |

## Decision rates (Issue #1230 R6b)

`control_rates(model)` turns a fitted model into the thermal-model scalars the rate decisions read when v4 is the primary engine (`k_active_heat`, `k_active_cool`, `heating_rate_f_per_hour`, `cooling_rate_f_per_hour`, `k_passive`, `confidence`, `confidence_k_passive`): the net gain of one hour of continuous heat (cool) from a settled room, rolled through this module's own `rollout` with no leak and no sun (so the hidden-mass lag is included, about 0.8 x `q_h` for the fitted Zone 1 house); `k_passive = -a_o`; grade `low` -> `low`, `ok` -> `medium`. `v4_shadow.run_cycle(baseline=True)` predicts the house with no setpoints (planned windows only): the path the ceiling guard and the nat-vent plan scan. Design, sensor choice and fallback: [Prediction Engines Spec: Control feed](prediction-engines-spec.md#control-feed-r6b).

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

Occupant view: the dashboard line used to tell a person with the heat on that the room would be ~1 F colder than it ends up (and ~1 F warmer with the AC on), because the predictor's thermostat started a short burst at its start threshold and then idled - it had no stop condition, so it delivered 0.34-0.5x the recorded duty (Issue #1230 R1b traces). The emulator is now hysteretic.

**The setpoint quantity.** Training and prediction use the SAME quantity: the plan's target band edge for the mode (heat: `lower`, cool: `upper`), which is all the live shadow knows. `samples_from_chart_log` assigns it to `V4Sample.heat_sp_f` / `cool_sp_f` as follows: only ACTIVE rows set the mode (heating -> heat, cooling -> cool); an idle row keeps the last active mode within 12 h (else a guess from the outdoor temperature) and takes that mode's OWN band edge, never the opposite edge (the simulated zones log the opposite edge while idle, which used to reject ~80 % of the starts). Rows without a band use the row `setpoint` on active rows and the last active row's setpoint of that mode on idle rows. The real house commands a heat setpoint about 1.5 F above the band floor; calibrating against the band absorbs that into the learned offsets (`--setpoint-source commanded` moves the backtest by only 0.1-0.25 F).

`learn_thermostat_detail(samples, primary)` (`learn_thermostat` is the thermostat-only wrapper) returns `V4Thermostat` plus start counts:

- **Start offset** (`on_below_f` / `on_above_f`): a sample whose heating (cooling) duty is > 0 while the previous contiguous sample's was 0; offset `setpoint - min(reading_prev, reading_now)` (`max(...) - setpoint` for cooling). Starts where the setpoint in force changed between the previous two samples or the start sample are not accepted. Median of at least 5 accepted starts, clamped to [-2, 4] F; otherwise the calibrated default 0.0 (hold at the setpoint), no longer the silent 1.0. This is only the uncalibrated estimate - `fit` refines it.
- **Stop offset** (`off_above_f` for heating: stop at `sp + off`; `off_below_f` for cooling: stop at `sp - off`): median of (reading around a run's end - setpoint) over runs whose setpoint did not move; default 0.0.
- **Run length:** `burst_minutes` / `burst_cool_minutes` are the median total active minutes of a run (reported; clamped 90 / 120). The emulator's **minimum run** is `min_run_heat_min` / `min_run_cool_min`: the 20th percentile of the run lengths, in [5, 30] min (a v1 model carries neither: `min(burst, 15)`). The old median was saturated on four of seven zones and is not a minimum.
- **Accepted-start counts** go to `fit["starts_accepted"]` / `fit["starts_total"]` per mode. A mode the zone uses (`heat_seen` / `cool_seen`) with fewer than `GATE_MIN_ACCEPTED_STARTS = 5` accepted starts is not trusted: the emulator keeps the calibrated defaults for it, `fit["thermostat_defaults_used"]` is true and confidence is at most `low` (reason "too few accepted thermostat starts in a used mode").
- **Reading quantisation:** `reading_step_f` = 1.0 when at least 98 % of the primary readings are whole numbers, 0.5 for half-degree, else 0. The thermostat acts on the rounded reading. A whole-degree house is where the old median start offset collapsed to 0 (it ran the room ~0.75 F warm); the calibration recovers the true offset (see `TestQuantisedReadings`).

**Closed-loop calibration (`_calibrate_thermostat`, in `fit` after the optimum is found).** For each mode with at least 5 accepted starts and 50 scored points, the EMULATOR (not the recorded-duty replay) is rolled from up to 100 training anchors - with the run state at the anchor and the setpoint the sample carries - and the weighted squared error against the recorded primary readings is compared over a grid of (on, off) in {-0.5, 0, 0.5, 1, 1.5, 2}^2 (36 candidates). Candidates are visited nearest-to-default first; among those within 4 % of the lowest squared error the least biased (smallest mean signed error) wins, because the replayed fit assumed a room that holds its level. Only anchors whose setpoint is known at every step are used (data hygiene: an unknown setpoint would teach "never heat"). Result in `fit["thermostat_calibration"][mode] = {calibrated, on_f, off_f, rmse_f, rmse_default_f, anchors}`; `V4Thermostat.calibrated` is true when any mode was calibrated. Cost: about 0.3 s per mode.

**Emulation (`rollout`, predict path), per 15-min step.** With the reading `rd` the thermostat sees (`H` rounded to `reading_step_f`):
1. A heating run ends when its setpoint is gone, or (after its minimum run) `rd >= sp + off_above` (cooling: `rd <= sp - off_below`).
2. If idle: heating starts when `rd <= heat_sp - on_below`; else cooling when `rd >= cool_sp + on_above`.
3. A running heat step has duty 1; once the minimum run is served and the free evolution would reach the stop point within the step, the step is PARTIAL (`need = (stop - H_free) / (q_h * dt)`) and ends the run with the room at the stop point. Cooling is the mirror.
4. `rollout(..., init_run=(+1 | -1, minutes_already_run))` and `predict(..., hvac_action_now="heating"|"cooling", run_elapsed_min=...)` honour a run already in progress at the anchor (its minimum run counts from `run_elapsed_min`, unknown -> 0). The backtest passes the run state from the recorded samples; the live shadow measures it from the newest chart rows (`v4_shadow.run_state_from_rows`).
5. No setpoint = HVAC off.

Whether the emulator is faithful is checked by duty: on the training anchors the emulated heat duty is within 20 % of the recorded duty (`TestCalibration`), where it was 0.34-0.51x before.

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
9. **Restarts (R1b).** `max_iterations` defaults to 100 (was 60: 7 of 10 backtest fits hit the cap and two landed in poor minima). A fit that does not converge, or that started from `warm_start`, is run a second time from a different start - the cold defaults after a warm start, a "light house" variant (small `a_o`/`c_hm`/`c_mh`, higher `q_h`) after a cold one - and the lower-cost optimum is kept. `fit["converged"]`, `fit["restarts"]` and (when the second start won) `fit["restart_won"]` report it. A nightly refit therefore costs up to twice a cold fit.
10. After the optimum: thermostat calibration (above), then the expected-openness prior is stored (below).
11. `c_mh >= 0.02`. With the bound at 0 the fit drove the hidden mass to a near-constant "ambient level" (0.006 on a simulated zone) that carried the heating-season level into the summer; the bound improved Zone 1 heating 12 h from -0.68 to -0.32 F bias and its MAE from 1.51 to 1.23 (backtest, unchanged elsewhere). `from_dict` clamps an older model's smaller `c_mh` instead of rejecting it.

## Training Window

The fit trains on a bounded window: the last 30 days before the fit (`const.V4_FIT_TRAIN_DAYS`; `v4_shadow.run_fit(train_days=)` keeps the chart rows - and the forecast snapshots - inside it; the backtest's `--train-days` default is the same 30). One rule, live and backtest. Months-old rows are a different season, a different sensor era and sometimes a different thermostat behaviour: with all history the Zone 1 whole-house-fan rate `a_f` came out 0.014/h (unresolved) and R4's night replay found fan nights at MAE 1.79 F / bias +1.18 on all-history fits versus 1.30 / +0.51 on the last 30 days. Chosen by walk-forward on all seven zones (22 scored `_ca` gate rows, honest v3, setpoint hygiene), not on one zone:

| train window | gate rows passing | mean v4 MAE | mean abs bias | worst abs bias | mean v4/flat MAE |
|---|---|---|---|---|---|
| 30 d | 15/22 | 0.99 | 0.29 | 0.59 | 0.54 |
| 45 d | 12/22 | 0.98 | 0.33 | 0.81 | 0.52 |
| 60 d | 12/22 | 1.00 | 0.37 | 1.07 | 0.52 |
| 90 d | 12/22 | 1.00 | 0.39 | 1.24 | 0.52 |
| all history | 13/22 | 1.00 | 0.38 | 1.24 | 0.51 |

The gain comes from the zones whose early history differs from the test weeks (S1 12 h bias -1.07 -> -0.46, HEAT 4 h -0.59 -> -0.40, S3 12 h -0.65 -> -0.43); Zone 1 is unchanged within noise (its 4/12 h heating and 4 h summer rows pass at every window). The cost is the COOL zone (bias +0.28 -> +0.56 at 4 h; v3 wins that zone at every window) and a thinner window per fit: a 30-day window sees both modes less often, so confidence stays `low` more often in shoulder seasons ("heating or cooling not yet observed" keeps the unseen mode's gain tied to the seen one by the production ratio). Exponential recency weights over all history were not implemented: a bounded window is the same rule in the fit and the backtest with no extra parameters, and the 45-90 d windows already show most of the benefit is simply dropping stale rows.

## Expected-Openness Prior

Occupant view: on a house that cools itself with open windows and the whole-house fan at night, the plan hands the predictor "windows closed, fan off" after the first hours, so the line said the room would stay ~0.5 F warmer at 12 h than it does (Zone 1 summer, +0.84 F).

`learn_openness(samples, tz)` stores, per local hour, the share of the last 28 days of training samples with `windows_open` and with the whole-house fan running (`V4Sample.whf_running` = `fan_running` AND the chart row's `nat_vent_active`; the furnace blower also sets `fan_running` and must not count). Under 7 days of samples it stores zeros. It is part of the model (`V4Model.openness`, serialised, refreshed by every fit). `predict(openness_prior=True)` applies it as FRACTIONAL `win` / `fan` wherever the plan says closed / off (an explicitly planned open window stays 1.0); the leak terms `a_w`, `a_f` already take floats. The prior lives in `predictor_v4`, not in the callers, so the backtest and the live shadow cannot diverge; the backtest's "v4 with actual inputs" diagnostic passes `openness_prior=False`. Because the share is taken over the last 28 days the prior vanishes by itself when the windows stop being opened (a heating season).

## Node Selection (shared by the live shadow and the backtest)

`resolve_nodes(entries, primary, sensors, aliases, in_sleep, tz)` is the one rule that picks the primary node and the extra nodes for a fit; `v4_shadow.run_fit` and `tools/predict_backtest.week_nodes` both call it. Rows older than the entity primary have no `indoor_sensors`, so `samples_from_chart_log(..., primary=, primary_aliases=, in_sleep=)` fills the primary node on every row from, in order: the entity's own reading, a climate-attribute alias (the thermostat attribute before the finer companion sensor became the primary - the same node), `indoor_primary`, then `indoor` unless the row is inside the sleep window while a sleep sensor is configured (`indoor` was then the bedroom). The entity primary is kept when it reads on at least 80 % of the rows (after this mapping), else the model is the `_ca` reading alone; extra entities need 50 % coverage. Before R1b the live fit on a log that predates `indoor_sensors` returned `none` with 0 anchors and ignored 212 days of history while the backtest silently fell back to `_ca`.

The confidence gates count samples that carry a primary reading (`fit["primary_samples"]`, `sample_days`), not every grid point.

## The `_ca` Line

`_ca` is the reading Climate Advisor itself uses: the comfort-facing `indoor`, which is the sleep sensor's reading inside the sleep window. `ca_line(preds, primary, sleep_node, sleep_time, wake_time, tz)` composes it from the node predictions (the sleep node's inside the window when that node is modelled, else the primary's); `in_sleep_window` mirrors `automation._in_sleep_window` (pinned by a test). `predict(..., ca_sleep_node=...)` returns it under `"_ca"`; the archive derives it on read; the backtest scores `_ca` this way (it infers the sleep sensor and window from the log, since it has no HA config).


`fit` dict: `n_anchors`, `span_days` (first to last sample), `sample_days` (samples x 15 min), `mae_by_lead` / `flat_mae_by_lead` (`"1h"`, `"4h"`, `"12h"`; replay MAE on the training anchors and the "no change" MAE on the same), `per_sensor` (4 h MAE each), `skill_vs_flat`, `iterations`, `cost`, `heat_seen`, `cool_seen`, `frozen`, `horizon_h`, `reason`.

## Confidence Gates

| Confidence | Condition | `predict()` |
|---|---|---|
| `none` | fewer than 3 days of samples that carry a primary reading (`sample_days < 3`), or fewer than 12 anchors, or a non-finite fit, or training MAE at 4 h not at least 10 % below "no change" | returns `None` |
| `low` | otherwise, but fewer than 10 days of samples, or heating or cooling not yet observed (at least 2 h of duty each), or a used mode with fewer than 5 accepted thermostat starts | predicts |
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
 "thermostat": {on_below_f, on_above_f, burst_minutes, reading_step_f, burst_cool_minutes | null,
                "off_above_f", "off_below_f", "min_run_heat_min" | null, "min_run_cool_min" | null, "calibrated"},   # R1b
 "openness": {"window_by_hour": [24 floats 0..1], "fan_by_hour": [24 floats 0..1], "days": float} | null,        # R1b
 "forecast": {"hourly_bias_f": [24 floats], "nowcast_tau_h": float},
 "fit": {...see Fit...}, "confidence": "none" | "low" | "ok"}
```

`from_dict` returns `None` on a wrong/missing `schema_version`, a non-mapping, non-string or duplicated sensor ids, `sensors[0] != primary`, any missing/non-finite/out-of-bounds parameter, a missing node, a non-positive burst, a bias list that is not 24 finite floats, `nowcast_tau_h <= 0`, an unknown confidence, or a non-string `fitted_at`. R1b added fields without bumping `V4_SCHEMA_VERSION`: `from_dict` reads a model written before R1b (no stop offsets, minimum runs, `calibrated` or `openness`; they default to 0.0 / `None` / false / no prior, and a `c_mh` below the new 0.02 bound is clamped) and a malformed `openness` is rejected. A model without the new fields emulates with its stored start offsets and no stop offset until the next nightly fit replaces it. A future incompatible change bumps `V4_SCHEMA_VERSION`; older files are then refitted, not migrated.

## Deviations From The Contract (additive)

- `V4Sample.heat_duty` / `cool_duty` (default `None`): fraction of the 15 minutes ending at `ts` spent heating / cooling, integrated from the chart_log row timeline. Exact burst accounting; without them the instant `hvac_action` is used.
- `V4Thermostat.burst_cool_minutes` (default `None`); R1b: `off_above_f`, `off_below_f`, `min_run_heat_min`, `min_run_cool_min`, `calibrated`. `V4Model.openness`, `V4Sample.whf_running`.
- `predict(..., outdoor_now_f=None, forecast_now_f=None)`: needed for the nowcast term. R1b: `hvac_action_now=None`, `run_elapsed_min=None`, `openness_prior=True`, `ca_sleep_node/ca_sleep_time/ca_wake_time=None`.
- `rollout(..., init_run=None)`; `samples_from_chart_log(..., primary=None, primary_aliases=(), in_sleep=None)`; `fit(..., max_iterations=100)`.
- `fit(..., max_anchors=160, warm_start=None)`.
- `step_mass(model, *, mass_f, readings, sample=None)`: `sample` optional.
- `predict()` omits a sensor whose reading is missing instead of inventing a start value.
- Helpers: `summary_line(model)` (INFO line for the nightly fit), `grid_steps(start, n)`, `learn_thermostat_detail`, `learn_openness`, `resolve_nodes`, `run_state_at`, `ca_line`, `in_sleep_window`, `CA_NODE`.

## Performance

Measured on the development machine (CPython 3.14, one core): 30 days of 15-min samples, two sensors, 160 anchors, 12 h horizon: 0.2-1.5 s typical (5-40 iterations), 3.3 s for 60 iterations (about 55 ms per iteration). A Raspberry Pi 4 is roughly 4-6x slower: about 5-8 s typical, 15-25 s worst case, inside the 60 s budget. `predict()` for 96 steps and 2 sensors costs about 1 ms. A nightly refit with `warm_start` converges in a few iterations.

R1b cost (7 zones, 60 days of 30-min rows, `_ca` primary, same machine): per fit the baseline took a mean 10.3 s (max 15.3 s) and R1b 8.9 s (max 14.7 s) with six backtests running in parallel on the box, i.e. no increase - the thermostat calibration adds about 0.3 s per mode (36 emulated candidates x up to 100 anchors) and the `c_mh` bound makes the fits converge in fewer iterations (COOL zone 24 -> 8). Uncontended, a 60-day fit is 0.6-0.8 s (Zone 1) to 3.6-6 s (the 38-51-iteration simulated zones). All seven fits converged (`fit["converged"]`) inside the new 100-iteration cap; a fit that does not converge, or a warm-started one, runs a second start, so a nightly warm refit costs up to twice a cold fit.

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
