<!-- Nav: ← [v4 Shadow Spec](v4-shadow-spec.md) | → [prediction_engines.py](../custom_components/climate_advisor/prediction_engines.py) [engine_state](../custom_components/climate_advisor/prediction_engines.py) -->

# Prediction Engines (primary / shadow) - Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant choose? | Settings -> Prediction engine (`primary_engine`, default **v4**) and an optional Comparison engine (`shadow_engine`, default **none**). The primary's line is the Status chart's main prediction, the shadow's is drawn for comparison, the Next 24 hours path and its text follow the primary. | [Settings](#settings) |
| How many engines run? | At most two: the primary and the optional shadow. The registry itself is open (a dict; built for more engines, e.g. a v5, with no count cap), but an engine that is neither primary nor shadow is not fitted, not predicted and not archived. v3's learned OBSERVATIONS (swing, solar phase, the learning database's samples) are inputs, not an engine, and are always collected; v3's own modelled curve (`_last_predicted_indoor`, chart-log `pred_indoor`) and its 4 h `pred_archive` are built only while v3 is the primary or the shadow (Issue #1230 R6, X5). | [Which engines run](#which-engines-run) |
| What does an engine have to provide? | The `PredictionEngine` contract: `id`, `label`, `state_file`, `archive_file`, `fit(history)`, `predict(state, horizon_hours, plan_inputs)`, `confidence(state)`, `state_of(source)`, `future_path(source)`, `past(...)`. | [Contract](#contract) |
| How do I add a v5? | `prediction_engines.register(V5Engine())`. The options form, validation, "at most two", the chart payload's engine catalogue and the archive lookups read the registry; `tests/test_prediction_engines.py` proves it with a fake v5. | [Adding an engine](#adding-an-engine) |
| What shows while the primary is still learning? | The plain schedule estimate (the engine-agnostic no-model path of `_build_predicted_indoor_future`, called with no thermal model), labelled "Prediction: learning - showing the schedule estimate", or "Learning: v4 is fitting from your history" while a fit runs, plus (R6b) "Climate Advisor is using its default plan for pre-heat, the time-of-use bank, the ceiling guard, setbacks and the window close time until v4 has learned your home." Never the other engine. Logged once per episode. | [Fallback](#fallback-while-the-primary-is-learning) |
| Does the choice change how the house runs? | Yes (R6b). The PRIMARY engine's path and rates drive the ceiling guard, the nat-vent plan's close time and start prediction, the nat-vent start veto, the time-of-use bank's lead time and the adaptive setback depth. The shadow never acts. With no usable primary fit they take today's "learning not finished" defaults, never the other engine. | [Control feed](#control-feed-r6b) |
| Is DP-8 a gate? | No. The backtest and the preview page are information. Nothing is hidden or shown because of a pass mark. | [No gate](#no-gate) |

## Settings

| Key | Values | Default | Notes |
|---|---|---|---|
| `primary_engine` | any registered id (`v3`, `v4`) | `v4` (upgrades and new installs; read with this default, no migration) | an unknown value reads as the default with one WARNING |
| `shadow_engine` | `none` or a registered id | `none` | must differ from the primary: the options form (Learning & Behavior) rejects it with `shadow_same_as_primary`; at read time an equal or unknown value reads as `none` with one WARNING |

Both live in `CONFIG_METADATA` (with defaults, so the dashboard Settings tab lists them under Learning & prediction), the options-flow `advanced` step, and `strings.json` / `translations/en.json`. Saving raises the usual "reload needed" repair issue; the new choice applies after the reload.

## Which engines run

`prediction_engines.running_ids(config)` = `[primary, shadow?]` (at most two; Issue #1230 O3, R6 X5). v3's learned OBSERVATIONS are inputs, not an engine, and are always collected (`observations_running()`; v4's control model lays its scalars over v3's observation keys); v3's modelled curve (`_last_predicted_indoor`) and its 4 h `pred_archive` are built only while v3 is the primary or the shadow (otherwise `_last_predicted_indoor` is empty). `Coordinator.engine_running(id)` is what the v4 orchestration asks: `_v4_shadow_cycle`, `_v4_start_fit` (nightly and first fit), `_v4_start_night_replay`, `_timeline_night_apply` and `v4_chart_series` all return early while v4 is not running. Switching the primary to v4 while it was off: after the reload the cycle finds no model, starts the first fit from chart_log history (executor, under the domain-wide fit lock), and the display shows the learning state until the fit lands, then v4 takes over.

## Contract

```text
id, label, state_file, archive_file
fit(history) -> state                       blocking; v4: v4_shadow.run_fit, v3: the learning engine's thermal model
predict(state, horizon_h, plan_inputs)      {sensor: [(ts, temp_f)]}; "_ca" = the line CA reads
confidence(state) -> str                    "none" = still learning; v3 grades none|low|medium|high, v4 none|low|ok
state_of(source) / future_path(source)      what the coordinator holds / its newest cached prediction
past(source, sensor, lead, start, end)      {slot_epoch: temp_f} the engine said `lead` ahead (the archive)
```

`usable(engine, source)` is true once `confidence` is in `USABLE_CONFIDENCE` = `("low", "ok", "medium", "high")` ("ok", v4's top grade, was missing before R6b, so a fully fitted v4 read as still learning); a read that raises counts as not usable. `engine_state(source, config)` builds the payload block (`primary`, `shadow`, per-engine `running/primary/shadow/usable/confidence`, `primary_state`, `fallback`, `fitting`, `text`, and since R6b `control` = `{engine, source: "<id>"|"defaults", ready, text}`).

## Fallback while the primary is learning

`get_chart_data()` adds `schedule_estimate` (display unit) only while `engine_state(...)["fallback"]` is true: `_build_predicted_indoor_future(..., thermal_model={})`, v3's setpoint-schedule ramp (the same path it has always used when its thermal model is below low confidence). The Status key shows it as "Schedule estimate (learning)" and `#chart-v4-note` carries the text. The timeline keeps today's default path (no `selected_path`) until the primary has a usable fit.

## What each consumer reads

| Consumer | Reads | Code |
|---|---|---|
| Status chart main line (both dashboards, `chart_v2.js`) | `prediction_engines.primary`; v3 line, v4 per-sensor lines or the schedule estimate | `_defaultOn`, `_engines()` |
| Comparison line | `prediction_engines.shadow` (on by default when set); any other engine's lines start off but stay in the key | same |
| Accuracy / backtest views | `primary_engine`, `shadow_engine` in the payload: tiles lead with the primary and name the comparison engine | `prediction_accuracy.html` `renderTiles`, `nd_debug.js` |
| Next 24 hours path and its text, briefing/Next Action sentences built from the timeline | `TimelineSnapshot.selected_path` (the primary's path) in `_choose_path` | `_timeline_snapshot`, `operation_timeline._choose_path` |
| Night card | v4 only, labelled "from v4": figures when v4 runs (primary or shadow) and the bedroom model is at least medium confidence; v4 off -> 503 `reason: v4_off` and the card says so | `night_strategy.api_payload`, `_timeline_night_apply` |

The nat-vent plan (cached once per cycle, read by the briefing, the Next Automation card and the engine) is computed from the CONTROL path (the primary's, see [Control feed](#control-feed-r6b)), so the text and the house agree.

## Control feed (R6b)

Occupant view: the prediction engine chosen in Settings (v4 by default) decides when the AC is switched on ahead of the house overheating (the ceiling guard), when the time-of-use bank starts, how deep the setback is sized and when the windows are planned to close. While it has not finished learning your house, all of that behaves as on a fresh install. The comparison engine only draws a line.

**Design.** The coordinator asks the registry for the two things the decisions read and hands them over as plain values; no decision module imports the registry or names an engine.

| Piece | Where | What it is |
|---|---|---|
| `prediction_engines.control_model(source, config, base)` | registry | The v3-shaped thermal-model dict the RATE reads use. v3 primary: `base` itself (the learning engine's object, identity). Usable primary: `base` with the engine's rate scalars laid over it (`k_active_heat`, `k_active_cool`, `heating_rate_f_per_hour`, `cooling_rate_f_per_hour`, `k_passive`, `confidence`, `confidence_k_passive`, `k_passive_via_bridge`); swing, solar phase and the vent rates stay v3's observations. No usable fit (or an engine without the control methods): `base` with those scalars blanked (`CONTROL_BLANK_MODEL`) = a home that has not finished learning. |
| `prediction_engines.control_path(source, config, now)` | registry | The primary's forward path of the CA-read line this cycle (`primary_path(engine.control_path(...))`), or `None`. |
| `resample_path(points, grid, anchor)` | registry | The engine's 15-minute points on the grid the decisions expect: the hourly forecast times plus each pair's midpoint (v3's grid), degrees F, `[{"ts", "temp"}]`, linear, no extrapolation (v4's 24 h horizon ends the list), anchored at (now, the live CA reading). |
| `Coordinator.control_thermal_model(base)` / `control_predicted_indoor()` | coordinator | Live providers. `control_predicted_indoor()` is v3's `_last_predicted_indoor` (read live) while `_control_path` is `None` (v3 primary), else the control path. The engine reads the model through `engine._control_model()` (a provider the coordinator installs; a bare engine reads `_thermal_model`). |
| `Coordinator._refresh_control_prediction()` | coordinator | Runs in the cycle right after v3's curve is rebuilt and BEFORE the nat-vent plan and every `apply_classification()` (main cycle and daily briefing). Calls the primary's pre-decision cycle (`_control_cyclers`, v4: `_v4_control_cycle`), resamples, stores `_control_path`, logs the episode. |
| `_v4_control_cycle()` | coordinator | One v4 predict in the executor from the LIVE readings (`_v4_live_row`) and the same inputs as the shadow cycle (`_v4_cycle_kwargs`, shared). It writes no mass state, archive or chart extras (the post-chart shadow cycle still owns those). |

**Which curve, and why the no-AC baseline.** The ceiling guard and the nat-vent plan ask "when does the house leave the band on its own?". The v4 PLAN path (setpoints at both band edges) holds the room at the edge, so it can never show a breach. The control path is therefore v4's baseline: the same predict with no setpoints and the planned windows (`v4_shadow.run_cycle(baseline=True)`), the house's own drift. v3's curve answers the same question on the days these decisions apply (an `off` day is passive in `_build_predicted_indoor_future`).

**Which sensor.** The `_ca` line (the reading Climate Advisor itself uses: the thermostat room/companion reading, or the sleep sensor inside the sleep window), else the model's primary node (`primary_path`). Every path consumer compares it with `indoor` = `_get_indoor_temp_f()` and the ceiling/floor the band arms on that same reading, which is also what v3's curve started from. Awake with no sleep sensor `_ca` is the thermostat node.

**Units and timing.** Degrees F internally (as v3). v3 gives hourly + 30-minute midpoints; v4 gives 15-minute points per sensor: resampled onto v3's grid so `find_temperature_crossing` still pairs timestamps with the hourly outdoor curve. The path is computed in the same cycle before the decision; a control path older than 45 minutes (`CONTROL_PATH_MAX_AGE_S`) is not trusted. Out-of-cycle callers (the 5-minute reactive tick, the setpoint re-assert, the startup coalesce) read the last cycle's path, as they always read the last cycle's v3 curve. Measured cost: 3.5 ms per cycle headless (one executor round trip plus the real predictor), well under the 50 ms budget; `predict()` itself is about 1 ms.

**Rates from v4 (`predictor_v4.control_rates`).** `k_active_heat`/`k_active_cool` are the net gain of one hour of continuous heating/cooling measured by rolling v4's own integrator (four 15-minute steps, no leak, no sun) from a settled room, so the hidden-mass lag is included (about 0.8 x `q_h` for the fitted Zone 1 house); `k_passive` is `-a_o`; confidence maps `low` -> `low`, `ok` -> `medium`. v3's own `k_active_*` are observed gross rates, so the two are comparable but not equal: the lead times and the setback depth differ by that factor, bounded by the existing clamps (`[30, 240]` min lead, `MAX_SETBACK_DEPTH_F`).

The Next 24 hours list reads the same feed (R6 cards, X6): its window periods are judged on `control_thermal_model()` (v3 primary: v3 model; usable v4: v4 scalars; still learning: blanked), so the list, the open step's "until about", the close step, the Next Action card and the briefing Windows row name one close time; the "default plan" note now lists the time-of-use bank and the window close time too.

### Decision inventory

| Decision | Reads | How often | Primary supplies | No usable primary fit (= today's "learning not finished") |
|---|---|---|---|---|
| ODE ceiling guard (`apply_classification` -> `classification_fsm` -> `decide_ode_ceiling_guard`) | path (breach scan), `k_passive`, `confidence_k_passive`, bridge flag, `k_active_cool` (lead) | every 30-min cycle, daily briefing, the 5-min reactive tick, setpoint re-assert, startup coalesce | the no-AC path of the `_ca` line; v4 rates | rates blank -> `MODEL_INELIGIBLE`: the guard stays quiet (the comfort-edge selector still reacts to a real breach) |
| Nat-vent plan (`compute_nat_vent_plan` -> `nat_vent_cutoff`, `ceiling_breach_time`, `evening_open_time`) -> engine `_nat_vent_cutoff` (`_is_within_planned_window_period`) | path (and the outdoor curve) | every cycle + briefing | the same path | the schedule estimate (flat on an off day) -> no early close, the static window close applies |
| Next Automation nat-vent start prediction (`_predict_nat_vent_start`, timeline `NAT_VENT_START`) | path + the real start gate | when the card/timeline is built | the same path (display of a control event) | schedule estimate |
| Nat-vent start veto (`_build_nat_vent_start_veto_inputs`: forecast peak, floor imminent) | `confidence`, `k_passive` | each nat-vent start attempt | v4 `k_passive`, `confidence` | confidence `none` -> the floor-imminent guard is skipped, as on a fresh install |
| Time-of-use bank lead time (`scheduler.resolve_tou_phase`, `resolve_tou_away_vacation_phase`) | `k_active_heat`/`k_active_cool`; away/vacation also `k_passive` + `confidence` | every cycle | v4 rates | `default_tou_lead_minutes` (45) |
| Adaptive setback depth (`compute_bedtime_setback` in `_compute_target_band_schedule` and the briefing) | `heating_rate_f_per_hour`/`cooling_rate_f_per_hour`, `confidence` | every cycle, briefing, chart | v4 rates | default depth (`DEFAULT_SETBACK_DEPTH_*`) |
| Chart "effective target" regime walk (display of the above) | path + model | on chart request | with a non-v3 primary: the control path and model | follows the same fallback |

**Not decisions on a predicted path (unchanged, v3's observations):** the thermostat swing (`swing_*_f`), the solar phase offset, the vent-window/fan rates, the comfort-edge selector, the plan's band edges, the sleep setpoints (`select_comfort_band` arms the configured values; the adaptive setback depth only sizes the plan/briefing band, issue #1366).

### Fallback, switching, observability

* **No usable fit** = `usable(primary)` false (confidence `none`, no model, fit failed) or the engine lacks `control_model`/`control_path`. The rate reads see `CONTROL_BLANK_MODEL`; the path is the schedule estimate (`_build_predicted_indoor_future(..., thermal_model={})`). One INFO per episode: "Prediction engine v4 has no usable fit yet (no usable fit): pre-heat, ..."; the next cycle with a fit logs "Decisions now use prediction engine v4 (was: defaults)". The payload's `control` block carries `{engine, source: "<id>"|"defaults", ready, text}`; the Status chart note appends `control.text`.
* **v3 -> v4 with no v4 model:** after the reload the cycle starts the first fit from chart-log history (`_v4_start_fit("first")`, startup coalescing over); decisions use the defaults until the fit lands, then v4 at the next 30-minute cycle. **v4 -> v3:** immediate (the next decision reads v3's curve and model).
* **A failed pre-decision predict** (exception, reading missing) leaves the schedule estimate as the path for that cycle with a WARNING; the rates stay the primary's if its fit is usable. This holds for a failure ANYWHERE in `_refresh_control_prediction` (Issue #1230 R6, X2): with a non-v3 primary the outer handler stores the schedule estimate (or `[]` if even that fails), never `None`; and `control_predicted_indoor()` returns `None` (no curve: the guard is `NOT_APPLICABLE`) before the first refresh. v3's curve reaches a decision only while v3 is the primary.
* **The guard's write (R6 X1):** the guard writes only when the thermostat is not already cooling at or below the guard ceiling; the R6b "same ceiling within 2 h" memory was replaced by that state test ([08 §6c](08-COMPUTATION-REFERENCE.md)). One rule for v3 and v4.
* **Structural guards** (`tests/test_v4_shadow_structural.py`): every `predicted_indoor=` handed to a decision is `self.control_predicted_indoor()`; the shadow is never read by a decision path (the control-feed methods name no shadow identifier; the registry's control functions never call `shadow_id`/`running_ids`); no decision module names the registry or the settings.
* **Goldens** (signed, owner delegation O6): `tools/simulations/golden/r6b_*.json`, one pair (v4 primary / v3 twin) per decision plus the fallback scenario, and (R6 X1) `r6b_warm_off_day_no_redundant_guard_writes__v3/__v4` (exact mode/setpoint writes over a warm off day; the older "guard follows the primary" and "holds without resending" cards were demoted to unit tests because the comfort band arms the same cool before the guard runs, so the guard can never be the writer there: `tests/test_ceiling_guard_state_skip_r6.py`); owner cards in `ca-group-briefs/1230-r6-golden-cards.md`. Tier B: the signed r6b goldens run through real Home Assistant in `tests_ha/test_goldens_realha.py` (the separate pending-folder file was removed when the cards were signed). A/B of recorded days: `tools/primary_engine_ab.py`.

## No gate

DP-8 (backtest gate) no longer shows or hides anything: `gate_summary.ready`, the C11 `show_numbers` gate and the "v4 shown after it passes its accuracy check" note are gone. The preview page's table is headed "How each model has scored" with descriptive "v4 beats v3" / "v4 beats no change" columns.

## Adding an engine

1. Implement the contract and `register()` it (module import is enough).
2. It appears in both dropdowns, validates, counts toward "at most two", shows in `prediction_engines.engines`, and its archive is read through `past()`.
3. To draw its lines on the chart, give `chart_v2.js` a series for its id (the chart still names v3/v4 series); everything else is registry driven.
