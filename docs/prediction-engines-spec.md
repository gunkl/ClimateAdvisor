<!-- Nav: ← [v4 Shadow Spec](v4-shadow-spec.md) | → [prediction_engines.py](../custom_components/climate_advisor/prediction_engines.py) [engine_state](../custom_components/climate_advisor/prediction_engines.py) -->

# Prediction Engines (primary / shadow) - Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant choose? | Settings -> Prediction engine (`primary_engine`, default **v4**) and an optional Comparison engine (`shadow_engine`, default **none**). The primary's line is the Status chart's main prediction, the shadow's is drawn for comparison, the Next 24 hours path and its text follow the primary. | [Settings](#settings) |
| How many engines run? | At most two: the primary and the optional shadow. An engine that is neither is not fitted, not predicted and not archived. v3 is the 0.9.4 exception: it drives every control decision, so it always runs. | [Which engines run](#which-engines-run) |
| What does an engine have to provide? | The `PredictionEngine` contract: `id`, `label`, `state_file`, `archive_file`, `fit(history)`, `predict(state, horizon_hours, plan_inputs)`, `confidence(state)`, `state_of(source)`, `future_path(source)`, `past(...)`. | [Contract](#contract) |
| How do I add a v5? | `prediction_engines.register(V5Engine())`. The options form, validation, "at most two", the chart payload's engine catalogue and the archive lookups read the registry; `tests/test_prediction_engines.py` proves it with a fake v5. | [Adding an engine](#adding-an-engine) |
| What shows while the primary is still learning? | The plain schedule estimate (the engine-agnostic no-model path of `_build_predicted_indoor_future`, called with no thermal model), labelled "Prediction: learning - showing the schedule estimate", or "Learning: v4 is fitting from your history" while a fit runs. Never the other engine. Logged once per episode. | [Fallback](#fallback-while-the-primary-is-learning) |
| Does the choice change how the house runs? | No. In 0.9.4 every decision (pre-heat, ceiling guard, setback, nat-vent, fan) uses v3 only. R6 wires the primary into those with goldens per decision. | [Control boundary](#control-boundary) |
| Is DP-8 a gate? | No. The backtest and the preview page are information. Nothing is hidden or shown because of a pass mark. | [No gate](#no-gate) |

## Settings

| Key | Values | Default | Notes |
|---|---|---|---|
| `primary_engine` | any registered id (`v3`, `v4`) | `v4` (upgrades and new installs; read with this default, no migration) | an unknown value reads as the default with one WARNING |
| `shadow_engine` | `none` or a registered id | `none` | must differ from the primary: the options form (Learning & Behavior) rejects it with `shadow_same_as_primary`; at read time an equal or unknown value reads as `none` with one WARNING |

Both live in `CONFIG_METADATA` (with defaults, so the dashboard Settings tab lists them under Learning & prediction), the options-flow `advanced` step, and `strings.json` / `translations/en.json`. Saving raises the usual "reload needed" repair issue; the new choice applies after the reload.

## Which engines run

`prediction_engines.running_ids(config)` = `[primary, shadow?, v3]`. `Coordinator.engine_running(id)` is what the v4 orchestration asks: `_v4_shadow_cycle`, `_v4_start_fit` (nightly and first fit), `_v4_start_night_replay`, `_timeline_night_apply` and `v4_chart_series` all return early while v4 is not running. Switching the primary to v4 while it was off: after the reload the cycle finds no model, starts the first fit from chart_log history (executor, under the domain-wide fit lock), and the display shows the learning state until the fit lands, then v4 takes over.

## Contract

```text
id, label, state_file, archive_file
fit(history) -> state                       blocking; v4: v4_shadow.run_fit, v3: the learning engine's thermal model
predict(state, horizon_h, plan_inputs)      {sensor: [(ts, temp_f)]}; "_ca" = the line CA reads
confidence(state) -> none|low|medium|high   "none" = still learning
state_of(source) / future_path(source)      what the coordinator holds / its newest cached prediction
past(source, sensor, lead, start, end)      {slot_epoch: temp_f} the engine said `lead` ahead (the archive)
```

`usable(engine, source)` is true once `confidence` is above `none`. `engine_state(source, config)` builds the payload block (`primary`, `shadow`, per-engine `running/primary/shadow/usable/confidence`, `primary_state`, `fallback`, `fitting`, `text`).

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

The briefing's own v3-curve computations (the nat-vent plan the engine consumes) stay v3: they are shared with control.

## Control boundary

`tests/test_v4_shadow_structural.py` (AST) asserts: no module outside the registry, `prediction_accuracy.py` (an echo), `const.py`, `config_flow.py` and `coordinator.py` names `prediction_engines`, `primary_engine` or `shadow_engine`; the coordinator reads them only in the display methods and the `_v4*`/`v4_*` orchestration; every `predicted_indoor=` handed to the engine or the nat-vent plan is v3's `_last_predicted_indoor`; the timeline's `selected_path` is read only by `_choose_path`.

## No gate

DP-8 (backtest gate) no longer shows or hides anything: `gate_summary.ready`, the C11 `show_numbers` gate and the "v4 shown after it passes its accuracy check" note are gone. The preview page's table is headed "How each model has scored" with descriptive "v4 beats v3" / "v4 beats no change" columns.

## Adding an engine

1. Implement the contract and `register()` it (module import is enough).
2. It appears in both dropdowns, validates, counts toward "at most two", shows in `prediction_engines.engines`, and its archive is read through `past()`.
3. To draw its lines on the chart, give `chart_v2.js` a series for its id (the chart still names v3/v4 series); everything else is registry driven.
