<!-- Nav: ← [docs/00-PROJECT-INSTRUCTIONS.md] | → [operation_timeline.py | tools/timeline_replay.py | coordinator.py `_timeline_*`] | ↔ [docs/v4-shadow-spec.md | docs/08-COMPUTATION-REFERENCE.md] -->

# Operation Timeline (Issue #1230 R3a/R3b) — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What is the operation timeline and who reads it today? | One ordered list of what CA will do and what the occupant should do over the next 24-48 h, plus the predicted indoor path under that plan. Since R3b it is AUTHORITATIVE for what the occupant is told: Next Action, Next Automation, the briefing's window times and "Today's Steps", the Analysis "Tomorrow (plan)" column and `GET /api/climate_advisor/timeline` all read it. No decision (automation, classifier, learning) reads it. | [Authoritative surfaces (R3b)](#authoritative-surfaces-r3b), [Invariants](#invariants) |
| Which steps can it contain and what performs each? | A closed enum (`StepKind`); every emitted member names the code path CA uses. Only windows are "you" steps. | [Step kinds](#step-kinds) |
| When is the day's timeline regenerated? | A new local day, an occupancy change, or a material change: classification, band move >= 0.5 F, fan mode, pause, override/grace, automation switch, or a forecast shift (12 h mean >= 2 F, or 24 h high/low >= 3 F). Otherwise the day's steps stay as promised; a window close may only move earlier, within its own window period. | [Latch and regeneration](#latch-and-regeneration) |
| When is a window period offered? | Only if the same direction rule as the live Next Action allows it, the learned window effect is validated (>= 50 ventilated observations, >= 1.5x the envelope rate), and simulating the plan with it saves >= 10 min HVAC or >= 0.5 F-h of discomfort without costing net score (1 F-h = 1 h HVAC, DP-2). | [Window candidates](#window-candidates) |
| What does the frozen output look like? | `Timeline` / `TimelineStep` / `PathPoint` and `to_dict`/`from_dict`, `TIMELINE_SCHEMA_VERSION = 1`. | [Output contract](#output-contract) |
| How do I check the timeline against history? | `tools/timeline_replay.py` regenerates it for every recorded day of a zone and diffs it with the chart_log and learning DB. | [Replay](#replay) |

## Scope

- **Files:** `custom_components/climate_advisor/operation_timeline.py` (the pure module), `coordinator.py` (`_timeline_*`, `timeline_debug`, five fixed hook points), `automation.py::band_resolver_for`, `temperature.py::outdoor_direction_ok_for_thermostat`, `tools/timeline_replay.py`.
- **Entry points:** `build_timeline(snapshot)`, `TimelineLatch.update(snapshot, force=None)`.
- Covered further down: the surfaces that read it ([Authoritative surfaces (R3b)](#authoritative-surfaces-r3b), `timeline_surfaces.py`), the night strategy's steps (R6, applied by `night_strategy.apply_night_strategy`; model and scoring in the [Night Strategy Spec](night-strategy-spec.md)) and the primary engine's path (`TimelineSnapshot.selected_path`, below). The sentence table is shared with them.

## Output contract

Frozen by the coordinator (`/home/dev/ca-group-briefs/1230-r3-timeline-contract.md`). `Timeline(schema_version, zone, generated_at, day, horizon_end, steps, path, latched, regen_reason)`; `TimelineStep(ts, actor, kind, text, reason, target_f, mode, confidence, lead_minutes, source, meta)`; `PathPoint(ts, temp_f, sensor, model)`. `reason` is the sentence-table rule id, `text` the sentence, `confidence` is `ok` or `low` (the three-level grade is `meta["grade"]`), `lead_minutes` is 30 for "you" steps (DP-4). `meta` carries `id`, `status` (`planned`/`held`/`done`), `after` (a fan start follows the window opening) and `facts`.

`DetailedTimeline` (a `Timeline` subclass) adds the builder's working data: `path_source`, `band`, `runs`, `candidates`, `summary`, `grade`, `hvac_minutes`, `hvac_hourly`, `dropped`, `generation`, `regenerated_for`. `to_dict` is the contract only; `detail_dict` is the extra, shown under `operation_timeline.detail` in the debug payload. Temperatures are internal degrees F in the payload; sentences use the user's unit.

## Step kinds

Appended to the contract by R3a: `resume_schedule`. Emitted members and their code path:

| Kind | Actor | Performed by |
|---|---|---|
| `wake_band` | ca | `automation.handle_morning_wakeup` -> `select_comfort_band(awake)` -> `_apply_comfort_band` |
| `sleep_band` | ca | `automation.handle_bedtime` -> `select_comfort_band(in_sleep_window)` -> `_apply_comfort_band` |
| `mode_change` | ca | the day plan's hvac_mode changes (`apply_classification`), placed at that day's wake |
| `setback_start` | ca | `handle_occupancy_away` / `handle_occupancy_vacation` (no end step: arrival is an event) |
| `precool_start` | ca | `handle_pre_cool` (weather) / `apply_tou_precondition` (TOU) |
| `preheat_start` | ca | `apply_tou_precondition(mode=heat)` |
| `nat_vent_start` / `nat_vent_stop` | ca | `check_natural_vent_conditions` -> `_activate_fan` / `_deactivate_fan`; follows the window opening |
| `resume_schedule` | ca | grace expiry -> `_apply_current_scheduled_state("grace_expired")` |
| `windows_open` / `windows_close` | you | CA has no window actuator |

Contract members R3a does not emit, and why: `setpoint_change` (every setpoint CA writes belongs to a named cause; a causeless one would be invented), `fan_on`/`fan_off` (the only planned fan runs are nat-vent sessions), `setback_end`/`occupancy` (arrivals and departures are unscheduled). (`night_strategy` is not emitted by the builder; since R6 `apply_night_strategy` adds it, with `windows_*`/`fan_*` advice steps, to the held timeline each cycle. R3b appended `briefing_send` and `ceiling_guard` and emits `setpoint_change` for one cause, see [Authoritative surfaces](#authoritative-surfaces-r3b).) A predicted heat/AC run is not a step: the thermostat starts it. It is listed in `runs`.

## Pre-conditions

1. All inputs are in `TimelineSnapshot` (local-aware datetimes, internal degrees F). The band comes from the injected `band_at(instant, occupancy)` - production passes `automation.band_resolver_for(classification, config)`, i.e. `select_comfort_band`, so band maths exists once.
2. The path sources are optional and tried in order by `_choose_path`: the PRIMARY engine's path (`selected_path`, `selected_path_model`, set by `coordinator._timeline_snapshot` from Settings -> Prediction engine; used only while the timeline holds the same window plan for today as the engine), the `path_fn` callback (`_build_predicted_indoor_future` with the windows replanned, model `v3`/`v3_replanned`), the v3 path, then the internal simulation (the v3 ODE step, model `sim`). While the primary has no usable fit no `selected_path` is passed (the default path stays).

## Post-conditions

1. Steps are sorted by `ts` (CA before "you" at the same instant). A step whose sentence facts are missing is dropped and its rule id listed in `dropped`; nothing is guessed.
2. A "you" step carries `confidence="low"` when its window effect is not validated (contract amendment, decision C13: an engine-recommended period is kept, graded low); the surfaces show it as "may help". A scanned period is only offered when its effect is validated.
3. A window period never runs into the sleep window (nobody is awake to close it): it ends at bedtime (`windows_close.bedtime`).

## Invariants

1. **No decision reads it (R3a; since R3b the occupant-facing surfaces do).** `tests/test_operation_timeline_structural.py` checks on the source (AST) that only the pure modules (`operation_timeline.py`, `timeline_surfaces.py`), the coordinator's build wiring and surface readers, and the briefing/API through the surface module or the coordinator mention it; each wiring point is a discarded call, the shadow methods write only `self._timeline_*` and command nothing, the notified events only mark the plan stale, and the modules import no engine, classifier, briefing, coordinator or Home Assistant code.
2. **One direction rule.** Planned window hours are judged by `temperature.outdoor_direction_ok_for_thermostat`, the same function `coordinator._outdoor_direction_ok` (the live Next Action card, #1276) now calls, with the swing margin for the mode the thermostat is expected to hold.
3. **One ODE.** `ode_step` and `solar_factor` are tested equal to `_simulate_indoor_physics_v3` and `_solar_factor`.
4. **#1276 invariant (for R3b):** Next Action must equal `Timeline.first_user_step()`.

## Window candidates

Candidates: the classification's window span (the nat-vent plan's ODE cutoff replaces the static close), the hot-day morning/evening opportunity spans, and scanned periods from the forecast: free cooling (baseline house above the ceiling, outdoor >= 2 F cooler than the house) and free heating (09:00-17:00, outdoor >= 1.5 F warmer, house in the lower half of the band). Each is trimmed to the hours where the direction rule holds and closed before the house drifts to a band edge or at bedtime, then simulated with and without (equipment paused while a window is open, as the door/window pause does). Gates: window effect validated (`window_effect_grade`), then `hvac_saved >= 10 min` or `discomfort_saved >= 0.5 F-h`, and `score = hvac_hours + 1.0 * F-h` must improve. Candidates are accepted greedily by net score. Rejected candidates stay in `candidates` with the reason (`unvalidated`, `direction`, `no_benefit`, `overlaps`). A window already open at generation gets only its close (`open_now`).

**Primary-engine rates and one story (Issue #1230 R6 cards, X6).** The window candidates are judged on the rates the decisions read (`coordinator.control_thermal_model()`: v3 primary = v3 model, usable v4 = v4 scalars over it, v4 still learning = blanked so only the engine's recommendation stands, graded low), never on v3's model behind a v4 primary. The TOU pre-cool/pre-heat step now carries `target_f` and `mode` (the bank setpoint) so the Next Automation card names it ("Pre-cool for TOU schedule (68°F)") instead of skipping to bedtime. A latched window-open step (`windows_open.*`) is re-worded after the latch merge so its "until about X" always names its period's current close (`_refresh_open_until`); time, id and status are untouched.

## Latch and regeneration

`TimelineLatch.update` rebuilds a candidate every cycle (cheap) and compares a `PlanSignature` with the latched one. Regeneration reasons: `first`, `new_day`, `classification`, `window_plan`, `occupancy`, `fan_mode`, `band`, `pause`, `override`, `automation`, `forecast`. "Material": band edge moved >= 0.5 F at any boundary ahead (compared as functions of time, so a boundary sliding into the past is not a change); forecast mean change of the next 12 h >= 2 F (`MATERIAL_FORECAST_MEAN_F`) or the next-24-h high or low moved >= 3 F (`MATERIAL_FORECAST_PEAK_F`). Without a material change the latched steps are kept (past ones marked `done`), except that a window close may only move EARLIER, and only to a fresh close of the same window period (after the latched window step before it; each fresh close replaces at most one, so tonight's bedtime close never stands in for tomorrow morning's close: `_merge_latched`); path, band, runs and candidates are always fresh. The coordinator also forces `occupancy` on an occupancy toggle and re-evaluates within 5 minutes after override/grace/pause events (`TIMELINE_TRIGGER_EVENTS` mark the plan stale; the 5-minute tick re-runs it).

## Shadow wiring

`ClimateAdvisorCoordinator._timeline_shadow_run(trigger)` runs after the v4 hook each 30-minute cycle, on occupancy change, and from the 5-minute tick when stale. Synchronous (about 3-12 ms per build over 48 h; `last_run.ms` in the payload), never raises (first failure WARNING, repeats DEBUG), commands nothing. `get_debug_state()["operation_timeline"]` returns the contract dict plus `shadow`, `unit`, `last_run` and `detail`. The sensor paths per room (`sensor_paths`) exist in the snapshot but the coordinator passes none in R3a.

## Error Conditions

- No classification yet, or a partially built coordinator: no timeline, no error.
- No learned envelope rate: no simulation, so no window periods, runs or sim path; band steps and the v3 path still appear.
- A broken `path_fn`: the next path source is used (v3 path, then the simulation).

## Replay

`tools/timeline_replay.py --data-dir DIR --all-zones [--json out.json]`. For each recorded day it builds the timeline as at 00:05 of that day and diffs it with chart_log rows and the learning record: matched steps (timing in minutes), timeline-only steps (classed), CA-only events (classed), predicted vs actual equipment (hour precision/recall, minutes), path MAE. Inputs are hindsight (documented in the tool's docstring): config inferred from the logged band ramps, logged forecast (lead 0), the DB's current thermal model, pre-cool read back from the logged band.

### Results (7 zones, 2026-11-06 copies; 89 + 63 + 58 + 58 + 51 + 49 + 49 days)

| Zone | Days | Match rate | Band step timing (median / max) | Equipment hours precision / recall | Path MAE |
|---|---|---|---|---|---|
| Zone 1 (real) | 89 | 0.23 | 5 / 45 min | 0.28 / 0.49 | 1.6 F |
| Simulated 2 | 63 | 0.96 | 5 / 5 | 0.82 / 0.68 | 2.6 F |
| Simulated Cold | 58 | 1.00 | 0.5 / 5 | 0.89 / 0.95 | 1.3 F |
| Simulated Hot | 58 | 1.00 | 0.3 / 28 | no equipment | 0.4 F |
| Simulated wild | 51 | 1.00 | 5 / 5 | 0.86 / 0.70 | 1.2 F |
| Simulated TOU Away | 49 | 0.91 | 3 / 5 | 0.21 / 0.07 | 4.7 F |
| Simulated TOU Vacation | 49 | 0.95 | 2.5 / 5 | 0.18 / 0.06 | 4.8 F |

Zone 1's low match rate is classed, not unexplained: of 204 timeline steps, 67 wake/bedtime steps fell where the thermostat was Off because a window was open (`paused_by_open_window`, the 600 s debounce rule), 70 where a nat-vent session owned the HVAC (`deferred_nat_vent_owns_hvac`: `handle_bedtime` and the wake path leave a running fan session alone), and 8 under a live override. See the classes below.

| Difference class | Meaning | Disposition |
|---|---|---|
| `paused_by_open_window`, `deferred_nat_vent_owns_hvac`, `override_held`, `thermostat_off_no_setpoint`, `setpoint_already_there` | The step is right but the engine did not write a setpoint (window pause, fan session, override, Off thermostat, already at the value). | Explained: documented CA behaviour. The step stays; R3b may qualify the sentence from the live state. |
| `edge_switch_nearest_edge_arming` | The setpoint flips between floor and ceiling as the room moves (#1066 nearest-edge arming). | Explained: reactive, not plannable from a day-start snapshot. The step reports the edge expected at that time. |
| `occupant_adjustment`, `occupant_change_expired_ca_restores` | An occupant used the thermostat (often +1 F at wake, DP-12) and CA restored its band at grace expiry. | Explained. The restore is a `resume_schedule` step once the override is live. |
| `band_edge_change` | A band-edge-to-band-edge setpoint move no day-start step names (away/return, TOU, sleep edge). | Explained for occupancy zones: arrivals/departures are unscheduled events. |
| `nat_vent_session_asleep_night_strategy` | Whole-house-fan sessions that start while the occupant sleeps. | Out of scope: night strategy is R4. No "you" step can be asked at night. |
| `nat_vent_session_awake_not_in_timeline`, `occupant_opened_windows_awake_unadvised`, `occupant_followed_engine_windows_timeline_closed`, `engine_recommended_windows_timeline_closed` | The engine recommends windows (and the occupant often opens them) on days the timeline says keep closed. | **Design difference to decide.** Today `windows_recommended` is the classifier's "outside air could help the range"; the timeline requires the plan to be better with the window (plan section 6). On 62 of 89 Zone 1 days the engine recommended windows and the timeline offered them on 4. Owner review before R3b. |
| `unexplained` | Zone 1: 9 staged summer AC setpoint steps (79 -> 75 -> 74, 82 -> 78) have no step; the occupancy-scenario zones add 20 and 9 of the same kind. | Listed as a finding; likely the ceiling guard / nat-vent AC assist, needs its own check. |

Equipment: hour precision/recall is informational. Zone 1 is low because v3 does not model the thermostat's short bursts (plan section 3: 4 h error about 1.6 F); the simulated zones, whose equipment follows the model, reach 0.82-0.89 precision. The two occupancy zones are scored at day-start occupancy only.

## Authoritative surfaces (R3b)

Occupant view: the card that says "Open windows at 10:00 AM", the card that says what the thermostat does next, the briefing's window times and step list, tomorrow's plan on the Analysis tab and the dashboard's timeline are one plan. Issue #1276 (the card inviting a window the plan never held) cannot recur between them: the surface rules live in `custom_components/climate_advisor/timeline_surfaces.py` (pure; `window_card_text`, `next_automation_candidates`, `operations_lines`, `timeline_windows`, `timeline_api_payload`).

| Surface | Reads from the timeline | Live guard kept on top (not plan) |
|---|---|---|
| Next Action (`coordinator._compute_next_action`) | the window step for today (`card_user_step`): "Open windows at T" / "Close windows by T"; inside a period "Open windows - outdoor's cooler now" | occupancy messages, the ONE live direction rule (`_outdoor_direction_ok`) turns an "open now" step into a HELD step worded "Keep windows closed ..." when the reading disagrees (below), windows-physically-open close guidance, cold/hot-day guidance. With no window step today no branch invites a window: when outdoor air would help a house above its ceiling the card says "Opening windows would help now - outdoor (o) is cooler than indoor (i). Climate Advisor didn't plan a window period today, so this one is your call." (it never starts "Open windows", which is reserved for a step the plan holds). |
| Next Automation (`_compute_next_automation_action`) | earliest automation-executed CA step ahead: briefing send, wake/bedtime band, pre-cool/pre-heat (weather, TOU), ceiling-guard AC start, the engine's nat-vent start prediction; labels in `NEXT_AUTOMATION_LABELS` (ontology-scanned) | startup coalescing, "Waiting for classification..." |
| Briefing (`generate_briefing(timeline=)`) | window close/open times (`timeline_windows`), warm/mild advice only when a period exists, a "Today's Steps" section (the step sentences) | classification-owned prose is unchanged |
| Analysis tomorrow (`tomorrow_plan`) | window times from tomorrow's window steps, day facts from `detail.days` | none |
| REST `GET /api/climate_advisor/timeline?zone=` | the contract dict (units converted, `unit` added, each step's `text_short` added) + `detail` | ambiguous zone -> 400 like `prediction_accuracy`; 503 before the first classification |
| Status "Next 24 hours" list and the chart plan overlay (classic and new dashboard, R6fe: `frontend/chart_v2.js`) | `steps` (now to +24 h, not `done`, no `briefing_send`; "you" steps with their `lead_minutes` reminder, `confidence: "low"` shown as "may help"; each row shows the step's `text_short`, falling back to `text`) and `path` (a `v4` path only when the chart payload says v4 is ready) | none: display only; empty / "not classified yet" (503) / error states. See [Architecture Reference](02-ARCHITECTURE-REFERENCE.md#status-tab-shared-chart-module-plan-list-and-plan-overlay-issue-1230-r2-r3-r5-r6fe) |

**Plan-list row wording (Issue #1399).** The list shows each step's time (with the weekday on another day) and who acts in their own columns, so the row's sentence must not repeat them. `timeline_surfaces.plan_list_text` derives the row wording from the step's final `text` (after any hold or veto rewrite): it drops the leading time (`operation_timeline._fmt_time` + ": "), drops a leading "CA" on a CA step (a few openings are rewritten, for example "bedtime, CA switches" -> "Bedtime: switches"), and capitalises the first letter. "8:30 PM: bedtime, CA switches to the sleep band, 66F to 72F, ..." becomes "Bedtime: switches to the sleep band, 66F to 72F, ...". A sentence that does not open with a time (a veto rewrite, a summary) is left whole, and an occupant step keeps its "you" wording. The REST view adds it as `text_short`; it is not stored and not part of the frozen contract (no schema bump, so a latched plan survives the upgrade). The canonical `text` keeps its time for every surface that shows a step on its own (the briefing's steps, the cards, the chart plan marks and hover). `tests/test_timeline_plan_list_text.py` walks every table sentence that opens with a time or "CA" (both tables, F and C); a new such template must be given a row wording there.

**The live direction veto is part of the plan step (R3b redesign, owner decision 2026-11-06).** A planned window period can be running while the live reading says opening would work against the house (outdoor cooler than a house the heat holds at its floor; outdoor not cooler than indoor). `timeline_surfaces.apply_direction_veto` marks that open step `held` (`meta.hold` `held.direction_veto.heat` or `.warm`, `meta.hold_kind` `direction_veto`) and rewrites its text with the card's own sentence ("Keep windows closed to hold heat - outdoor (61F) would cool the house below its 68F floor."), so the plan list, the REST body and the briefing say what the card says; before, the list kept an unmarked "open the windows" step and the briefing said "Close by 9:00 PM" (Issue #1276 on a new surface). `coordinator._surface_timeline()` applies it on every read with the coordinator's own live readings (`live=False` returns the latched plan; the card applies the veto itself with the readings it was handed). It is never stored in the latch, it only touches the step the card is about (a later period stays a plan), and the close step is unchanged. The briefing's TLDR Windows row for a vetoed period reads `Closed for now - outdoor air would cool the house below its floor` (or `... isn't cooler than indoor yet`); a period still ahead reads `Open 10:00 AM - close by 5:00 PM` (both times, so a briefing-only reader knows to open), and `Close by 5:00 PM` once the open is behind. The plan list shows a held step without the "may help" qualifier. Held steps from an override, grace or door pause are unchanged (`held.suffix`, `held.pause`).

**Time-of-use bank in progress (Issue #1404).** No step is added for a bank that is already open (the step list, and so `ca_steps`, is unchanged). The REST body carries a view field `current_hold` (`coordinator.current_hold_payload()`: kind, mode, target, `ends`, heat/cool of the hold, the schedule band and the band after, `text`), and the plan list opens with a "Now" row (`data-kind="tou_bank"`) showing `text`, the sentence Status shows. Next Automation lists the bank's end as its own candidate (`NEXT_AUTOMATION_LABELS["tou_bank_end"]`, "High-cost period starts — {after}"). The away/vacation `setback_start` step quotes the setback band itself (`Segment.base_floor/base_ceiling`) when a bank overlay lies on its segment; it used to call the bank edge (60–77 °F) "the away band".

**Degraded path:** a coordinator with no timeline (partially built, no classification yet) keeps the previous computation in `_compute_next_action` / `_compute_next_automation_action` / `tomorrow_plan`. Production always has a timeline after the first classification (the cycle generates it before any surface: `_async_update_data_impl`; a standalone read refreshes a run older than 2 minutes).

**Decisions (owner review).** D1: window advice. The timeline adopts the engine's current recommendation (`windows_recommended`, window open/close, the ODE cutoff, the evening reopening) as the source of WINDOWS_OPEN/CLOSE steps, still filtered by the live direction rule. A validated window effect (>= 50 observations, >= 1.5x envelope rate) lets the simulation ADJUST timing (direction-trimmed span; `accepted` or `engine_no_benefit`); unvalidated keeps the engine's times, confidence `low` (so `you` steps CAN now be `low`: a contract departure, reported). Zone 1 replay: engine advised 62 of 89 days, timeline now offers 43 (was 4), occupant opened on 41. Scanned free-cooling/heating periods keep the strict gates. D2: regeneration thresholds unchanged (2 F / 12 h mean, 3 F / 24 h). D3: held wording has no "wait/paused/grace": "... It applies when the door or window closes." / "... It applies when CA resumes the schedule at T." D4 (new): the live fallback "Open windows or turn on the fan" (cooling needed, outdoor cooler, engine NOT recommending windows) contradicted the plan. It was first replaced by "Outdoor (o) is cooler than indoor (i), but today's plan has no window period." (a problem with nothing to do); the owner redesign (2026-11-06) says what the occupant can do and that it is unplanned: "Opening windows would help now - outdoor (o) is cooler than indoor (i). Climate Advisor didn't plan a window period today, so this one is your call." What the AC does meanwhile is the Status and Next Automation cards' job, not this card's (Status Card Ontology).

R6: the night strategy (`night_strategy.apply_night_strategy`) is applied to the held timeline after every latch update (`TimelineLatch.amend`); its steps are never latched (re-applied each cycle). The replay tool takes `windows_recommended` from the chart_log's LIVE flag at the plan moment (the day record holds the end of the day's state; a zone whose rows lack the flag falls back to the record): Zone 1 days the engine recommended windows 62 -> 65, match rates unchanged on all seven zones.

Step kinds appended by R3b: `briefing_send`, `ceiling_guard`; `setpoint_change` is now emitted for exactly one named cause, the away/vacation TOU window widening (Issue #899, `setpoint_change.tou_expansion_start/_end`). `NAT_VENT_START` also comes from the engine's start prediction (`natvent_start.predicted`). Live steps (pre-cool, ceiling guard, resume, predicted nat-vent) are re-read from the fresh build each cycle (`LIVE_KINDS`); the rest stay as latched. The weather pre-cool is not offered to an empty house. `detail.days` carries the per-day classification facts (today, tomorrow); tomorrow's engine window plan is in the steps.

**Invariant test (plan 11.8)** replaces the shadow-only restriction: `tests/test_timeline_surface_invariants.py` generates snapshots (day type x fan mode x occupancy x hour x readings x validated/unvalidated, hypothesis) and asserts card, Next Automation, briefing and endpoint agree with the same timeline; `tests/test_operation_timeline_structural.py` now guards WHO may read it (pure modules, coordinator readers, briefing via `timeline_surfaces`, api via the coordinator) and that the builder commands nothing; Tier A goldens (`tools/simulations/pending/r3b_*.json`) and the Tier B real-HA replay of three Zone 1 days (`tests_ha/test_timeline_surfaces_variant_days_1230.py`) check the real coordinator.

### Replay after R3b (7 zones, same copies)

Unexplained: 9 -> 0 in Zone 1 (the "staged summer AC steps" are occupant overrides after an away setback, in the learning record's `override_details`: no CA code path; class `occupant_override_after_setback`); occupancy zones 29 -> 3 (away/vacation TOU window widening, Issue #899, now a timeline step; class `tou_window_expansion` in the replay, which has no schedule input). Whole-house-fan sessions that start asleep are NOT scheduled by CA: nat-vent is reactive to an open window. Zone 1: 61 left open past bedtime (`nat_vent_session_window_left_open_past_bedtime`), 22 opened while asleep; R4 advice, not a CA step. Timeline-only steps rise because engine periods are offered when the occupant did not act (Zone 1: windows_open matched 11, occupant_did_not 35).
