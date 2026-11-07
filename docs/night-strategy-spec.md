<!-- Nav: ← [Predictor v4](predictor-v4-spec.md) | → [night_strategy.py](../custom_components/climate_advisor/night_strategy.py) [plan_night#L835](../custom_components/climate_advisor/night_strategy.py#L835) [apply_night_strategy#L1099](../custom_components/climate_advisor/night_strategy.py#L1099) [grade_rates#L638](../custom_components/climate_advisor/night_strategy.py#L638) | ↔ [v4 Shadow](v4-shadow-spec.md) [Computation Reference](08-COMPUTATION-REFERENCE.md) [Fan Lifecycle](fan-lifecycle-spec.md) [Nat-Vent Lifecycle](nat-vent-lifecycle-spec.md) -->

# Night Strategy (bedroom) - Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does the occupant get each evening? | One recommendation for the bedroom tonight ("open the bedroom windows until about 1 AM, then close up; CA keeps the heat for the bedroom at 66°F") with the trade-off against the alternatives, and only options CA has evidence for. Advice only: CA never starts a fan for a strategy (DP-7). | [Scope](#scope), `headline()` / `tradeoffs()` |
| Which options exist per fan mode? | `whole_house_fan`: closed / windows 2 h / windows all night / fan 2 h / fan until the bedroom reaches floor + 1 / fan then AC. `hvac_fan`: closed / windows 2 h / windows all night / blower 2 h. No fan: closed + windows. `both` or anything else: closed only, never planned for (Issue #1175). | [Options](#options-dp-3), `OPTIONS_BY_FAN_MODE` |
| How is the best option chosen? | Lowest score = F-hours outside the sleep band beyond the thermostat swing + hours of HVAC (DP-2: 1 F-hour = 1 h, a constant). Scores within 0.05 tie; a tie goes to fewer occupant actions, then less HVAC, then the gentler option (closed, windows, fan). | [Scoring](#scoring-dp-2), `rank()` |
| When is an option hidden? | When any rate its own simulation used is below medium confidence: model not `ok`, rate frozen in the fit, effect not resolved, too few hours of history, bedroom sensor under 72 h of readings, or its option class not validated by the recorded-night replay. Hidden options carry the reasons in `NightStrategy.hidden` (debug) and are never in the occupant payload. | [Confidence gate](#confidence-gate), `grade_rates()` |
| What does "validated" mean? | `tools/night_strategy_replay.py`: v4 fitted before each recorded night, the night predicted under the windows / fan that actually happened, compared with the recorded sensor. A class needs at least 3 nights, mean error at most 1.5 F and better than "nothing changes". | [Validation](#validation-by-replay), [Debugging Guide](09-DEBUGGING-GUIDE.md#night-strategy-replay-toolsnight_strategy_replaypy-issue-1230-r4) |
| How does it reach the timeline? | `apply_night_strategy(timeline, strategy) -> Timeline` (pure, idempotent): one `NIGHT_STRATEGY` step (actor CA, says only what CA holds), `WINDOWS_*` steps (actor YOU), `FAN_*` steps (actor YOU unless CA's nat-vent already runs that fan). | [Timeline steps](#timeline-steps-r3-contract) |
| Is it wired into the coordinator? | Not in R4. The module is pure and headless; the coordinator/API/frontend wiring is done at merge time. | [Wiring at merge](#wiring-at-merge) |

## Scope

- **File:** `custom_components/climate_advisor/night_strategy.py` (pure Python; `predictor_v4`, `const`, `temperature`; no `hass`, no clock, no I/O)
- **Entry points:** `plan_night(NightInputs) -> NightStrategy`, `apply_night_strategy(timeline, strategy)`, `evidence_from_samples(samples)`, `validation_status()`, `headline()`, `tradeoffs()`, `replay_recorded()` (replay tool only)
- **Tool:** `tools/night_strategy_replay.py` (validation replay and `--tonight` planner)
- **Scope facts (claude.md, #1197):** only `whole_house_fan` and `hvac_fan` get fan options; `both` is never planned for. A blower press under `whole_house_fan` is the occupant's own equipment (not a CA fan).
- **Not covered:** the v4 model ([Predictor v4](predictor-v4-spec.md)), the timeline builder and its sentence table (R3a, `operation_timeline.py`), the Tonight card / comparison page (R5), and any automation that acts on a strategy (a later release behind a setting, if DP-7 says so).

## Model Of The Night

Physics is the v4 model, one 15-minute step at a time through `predictor_v4.rollout` with the heat/cool duty the controller below chose (so the equations exist once). The planner adds the part `predict()` cannot express for a night:

1. **Thermostat** reads the hallway (primary node). Start thresholds, burst lengths and reading quantisation come from `model.thermostat`.
2. **Sleep-band outer loop (#1194)**: when the target is a bedroom node, the setpoint is nudged by `REMOTE_OFFSET_STEP_F` up to `REMOTE_OFFSET_CAP_F` while the bedroom is more than the swing outside the band (heat: below the floor; cool: above the ceiling) and stepped back down when it is back inside.
3. **HVAC pause**: while windows are open or the whole-house fan runs, HVAC does not run (owner rules 2 and 3; the 600 s debounce is ignored, a conservative simplification). The HVAC blower is the HVAC fan: it does not pause HVAC.
4. **Fan and windows** are inputs to the leak (`a_w`, `a_f`, `b_w`); the bedroom feels the fan only through the hallway coupling `b_h` (the model has no direct fan term for extra nodes).

The start state (`readings`, `mass_f`) is the state at bedtime. Before bedtime the caller passes the plan's predicted state (the timeline path) or rolls the model forward (`tools/night_strategy_replay.py --tonight` does).

## Options (DP-3)

| Key | Class | Windows | Fan | Rule |
|---|---|---|---|---|
| `closed` | closed | closed | off | Baseline, always listed. CA heats/cools as needed. 0 actions. |
| `windows_2h` | windows | open | off | First `window_hours` (default 2 h), then close. 2 actions. |
| `windows_all_night` | windows | open | off | Open at bedtime, stay open until wake. 1 action. |
| `fan_2h` | fan | open | whole-house | First 2 h, then close up. 2 actions. |
| `fan_until_target` | fan | open | whole-house | Until the bedroom reaches floor + 1 F, or outdoor is no longer 2 F cooler than the bedroom. 2 actions. |
| `fan_then_ac` | fan | open | whole-house | Until the bedroom is under ceiling - 1 F (same outdoor rule), then close and let the AC finish. Needs cooling. 2 actions. |
| `blower_2h` | blower | closed | blower | `hvac_fan` only: HVAC fan for 2 h, HVAC not paused. 2 actions. |

A non-closed option that would not do anything tonight (the bedroom never needs the fan) is listed with `runs = False`, counts 0 actions and is never the pick. "Windows only" is offered only once its effect is resolved and validated (DP-3); on Zone 1 (fitted `a_w` 0.008/h against `a_o` 0.037/h) it is hidden.

## Scoring (DP-2)

```
discomfort_fh = sum over steps of max(0, floor - swing_heat - T, T - ceiling - swing_cool) * 0.25 h      # T = target sensor
score         = discomfort_fh * 1.0  +  hvac_minutes / 60                                                # 1 F-hour = 1 h of HVAC
```

`swing_*` default to `THERMAL_SWING_DEFAULT_F` (the learned thermostat swing when the caller has it). Ties: within `TIE_EPS` = 0.05 of the best score. The weight is a constant in this release (DP-2 decided: not a setting).

## Confidence Gate

An option is **offered** when its grade is `medium` or `high`; `closed` is always listed (the do-nothing baseline) but with `show_numbers = False` when its own grade is `low`, so the surface shows the plan without predicted figures. The grade is the LOWEST over the rates the option's own simulation used:

| Used by the simulation | Needs for `medium` | `high` needs |
|---|---|---|
| anything | v4 `confidence == "ok"` (>= 10 days, heating and cooling both seen, skill over "no change") | - |
| heating (`q_h`) | `h/q_h` not in `fit.frozen`; >= 4 h of heating in the history | 3x the hours |
| cooling (`q_c`) | `h/q_c` not in `fit.frozen` (AC rate learned, not assumed); >= 4 h of cooling | 3x the hours |
| whole-house fan / blower (`a_f`) | `h/a_f` not frozen; `a_f` >= 0.10/h; >= 12 h of fan running | 3x the hours |
| windows only (`a_w`, `b_w`) | `h/a_w` not frozen; `a_w` >= max(0.01, 0.5 x `a_o`); >= 24 h of windows open; bedroom `n/<j>/b_w` not frozen | 3x the hours |
| bedroom node (target is not the primary) | the sensor is a model node; >= 72 h of its own readings; `n/<j>/q_b` not frozen when HVAC ran | 3x the hours |
| option class (replay) | `NightValidation`: >= 3 nights, MAE <= 1.5 F, MAE < "no change" MAE. `closed` is only gated when a record exists. | >= 10 nights, MAE <= 1.0 F, replay not provisional |

A provisional replay (`provisional: true`) caps every grade at `medium`. `Evidence` comes from `evidence_from_samples(samples)` (hours of windows open, fan, heat, cool and per-sensor readings in the v4 sample history); `fit.frozen` entries are `h/<param>` and `n/<node index>/<param>` strings.

## Validation By Replay

`tools/night_strategy_replay.py` walks forward by ISO week: fit v4 on chart_log rows before the Monday, then for each recorded night (local 20:30 to 06:30, unbroken 15-minute readings) predict from the bedtime reading with the RECORDED outdoor, window, fan and heat/cool duty (variant A, the gate) and compare with the recorded hallway at every step. Variant C replays the same night through the planner's own controller from the recorded setpoints (only nights with a recorded setpoint). A night is excluded when the fit it used had the class's rate frozen. Output JSON feeds `NightValidation.from_dict`. Results are PROVISIONAL until R1b's model fix lands.

Latest Zone 1 result (203 of 208 nights, 2026-04..11, full-history fits, i.e. what the shadow fits today): closed 27 nights MAE 1.39 F; windows 25 nights 1.42 F; fan 150 nights 1.79 F (over the 1.5 F limit: the fan class is NOT validated). With fits limited to the last 30 days: closed 1.47 F, windows 1.22 F, fan 1.30 F (all validated). The chart_log has no bedroom history before 2026-11-06, so only the hallway is validated.

## Timeline Steps (R3 contract)

`apply_night_strategy(timeline, strategy)` returns a new `Timeline` (never mutates; applying twice equals applying once):

| Step | Actor | Text rule |
|---|---|---|
| `NIGHT_STRATEGY` at bedtime | CA | Says only what CA holds ("CA holds the bedroom between 66°F and 72°F tonight; nothing for you to do."). Never an occupant instruction: Next Automation must be automation-executed (#849). `confidence = "low"` when the pick is a low-grade baseline. |
| `WINDOWS_OPEN` / `WINDOWS_CLOSE` | YOU, `lead_minutes = 30` (DP-4) | Always YOU: CA has no window actuator. A condition-based close says "about". |
| `FAN_ON` / `FAN_OFF` | YOU | **Actor rule:** in R4 CA does not run a fan for a strategy (DP-7), so a fan step is advice with actor YOU. Exception: when the timeline already holds a CA nat-vent session (`NAT_VENT_START`/`NAT_VENT_STOP`, actor CA) that starts within 20 minutes of the fan span's start and does not stop before its end, the fan runs on CA's own rule and no `FAN_*` step is added (`NIGHT_STRATEGY.meta.fan_by_ca_natvent = true`). A partly covered span keeps the YOU steps. |

Existing YOU window/fan steps inside the night span are replaced by the chosen option's (a "closed" pick removes only the opens, so a generic "close at bedtime" survives). The target sensor's path points in the night are replaced by the option's path (`model = "v4"`). YOU steps are only added when the pick's grade is offered; all reasons are `night_strategy.*`; texts come from `NIGHT_SENTENCES` (to be registered in R3a's sentence table at merge, plan section 9).

## Pre-conditions

1. `NightInputs.model` is a `V4Model` (any confidence; `none` offers only the baseline). `readings` holds the primary sensor and, for a bedroom target, that sensor's reading at `start`.
2. `outdoor` is tonight's corrected forecast (the caller applies the v4 correction; `predictor_v4._corrected_outdoor` for a raw forecast).
3. `start < end`, aware datetimes; stepping is in absolute time (DST-safe).

## Post-conditions

1. `plan_night` always returns the `closed` option when the primary reading exists; `recommended` is the first offered option that runs, else `closed`.
2. `NightStrategy.to_dict()` never contains a hidden option; `include_debug=True` adds them with their reasons.
3. Every offered option carries a grade >= `medium`, except the baseline `closed`.
4. Same inputs, same output (no randomness, no clock).

## Invariants

1. No fan option exists for `both` or an unknown fan mode (scope fact).
2. The gate looks at what the option's own simulation used; an option that never runs the fan never depends on `a_f`.
3. `apply_night_strategy` never adds a CA-actor step that tells the occupant to do something.
4. Hidden options never reach `to_dict()` without `include_debug`.

## Error Conditions

| Failure | Handling | Caller receives |
|---|---|---|
| Primary reading missing / no forecast points | Option cannot be simulated | Listed in `hidden` with "could not simulate"; no `closed` either, so `recommended = None` |
| Bedroom sensor unavailable or not a model node | Target falls back to the primary | `debug.target_fallback = true`; every option graded low except nothing offered beyond `closed` |
| Model confidence `none` / `low` | Everything graded low | Baseline only, `show_numbers = false` |
| Timeline module lacks a member | `AttributeError` in `apply_night_strategy` | Caller (wiring) catches; the strategy is advice, never required |

## Draft Scenarios (DP-11)

`tools/simulations/pending/night_strategy_*.json.draft` (whole-house fan, HVAC fan, whole-house fan unvalidated): unsigned drafts with owner cards and the three-exercise answers. The `.draft` suffix keeps them out of `tools/simulate.py` (the production engine has no night-strategy decision to assert, and a scenario can only assert what production does). `tests/test_night_strategy.py::test_draft_scenario_matches_the_engine` runs each against the module. Never signed or promoted from R4.

## Wiring At Merge

Not done in R4: (1) the coordinator builds `NightInputs` each evening (sleep band at the sleep sensor, `evidence_from_samples`, the stored `NightValidation`, the v4 model, the corrected forecast, the state at bedtime from the timeline path) and calls `plan_night` then `apply_night_strategy` after the timeline is built; (2) the nightly job stores the replay result next to the v4 model file; (3) the sentences in `NIGHT_SENTENCES` move into the R3a sentence table; (4) `_simulate`'s thermostat emulation is replaced by the predictor's own once R1b changes it (hysteretic stop, minimum run); (5) re-run `tools/night_strategy_replay.py` after R1b.

## Code Reference

`custom_components/climate_advisor/night_strategy.py`: `Evidence`, `NightValidation`, `NightInputs`, `StrategyOption`, `NightStrategy` (data); `OPTIONS_BY_FAN_MODE` L339; `_simulate` L412; `evidence_from_samples` L603; `validation_status` L622; `grade_rates` L638; `rank` L820; `plan_night` L835; `tradeoffs` L910; `headline` L950; `NIGHT_SENTENCES` L1021; `apply_night_strategy` L1099; `replay_recorded` L1247.
`tools/night_strategy_replay.py`: `find_nights`, `classify`, `replay_night`, `run`, `tonight`.
Tests: `tests/test_night_strategy.py` (physics, ranking, gate, fan modes, hidden options, timeline steps, draft scenarios).
