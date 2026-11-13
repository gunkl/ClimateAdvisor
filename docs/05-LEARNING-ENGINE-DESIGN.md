<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) → [Computation Reference](08-COMPUTATION-REFERENCE.md) -->

# Climate Advisor — Learning Engine Design

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What are the five parallel observation types and what does each measure? | `hvac_heat` → k_active_heat; `hvac_cool` → k_active_cool; `passive_decay` → k_passive; `vent_window_decay` → k_vent_window (windows open, fan/WHF physically off); `vent_fan_decay` → k_vent_fan (windows open, fan/WHF physically running; a session paused between fan cycles is neither, Issue #1505); `solar_gain` → k_solar. All five can run concurrently. `fan_only_decay`/`k_vent` retired (Issue #587) — confirmed dead code, never affected a live forecast. | [§v3 Parallel Observation Architecture](05-LEARNING-ENGINE-DESIGN.md#v3-parallel-observation-architecture-issue-121) |
| What is the v3 ODE formula and what does each parameter mean? | `dT/dt = (k_passive + k_vent_eff)*(T_out − T_in) + k_solar*solar_factor + Q_hvac`; k_passive (hr⁻¹, negative) is envelope decay rate, k_active is HVAC contribution (°F/hr). Forecast-time `k_vent_eff` substitution still keys on `k_vent_window` only (windows open) — `k_vent_fan` is learned/displayed but not yet wired into per-hour forecast selection (Issue #587 scope boundary, not an oversight). | [§Physics Model](05-LEARNING-ENGINE-DESIGN.md#physics-model-issue-114-v2--issue-121-v3) |
| How are k_passive and k_active extracted from observations? | `k_active` (heating or cooling per hour) is learned by the **pooled cycle model** (Issues #1512, #1516): every heating or cooling cycle is recorded with the room readings around it (`climate_advisor_cycles.json`), and all recent cycles of a mode are fitted together, so the rate, the leak and the coast after the equipment stops are separated instead of the room's drift after a run being mistaken for the rate. It starts from a prior (typical house, or the home's stored rate weighted by its evidence), counts a cycle only when the room moved at least three sensor steps, grades how sure it is (`none`/`low`/`medium`/`high`; a whole-degree sensor never reaches `high`), and lets old cycles give way only to newer ones (a cycle's weight halves for every 20 newer informative cycles, never with time alone). `k_passive` (the closed-house leak rate) is learned only from closed-house data (passive windows); the cycle model's own leak (`k_p_cycle`) is a diagnostic and never drives a decision. `k_solar` uses the same per-interval-subtraction pattern (`compute_k_solar`, Issue #587 Defect B). | [§Parameter Extraction](05-LEARNING-ENGINE-DESIGN.md#parameter-extraction) |
| What does `get_thermal_model()` return and how is confidence graded? | Returns k_passive, k_active_heat, k_active_cool, k_vent_window, k_vent_fan, k_solar, plus confidence grades ("none"/"low"/"medium"/"high") graded independently for HVAC (the cycle fit's grade and the evidence behind the heat/cool rates: the pre-upgrade count, which gives way only as new cycles replace or contradict it, never with time alone, Issue #1512) and passive obs (vent/solar counts explicitly excluded from passive confidence, Issue #587). Physics activates when either > "none". | [§get_thermal_model() → dict](05-LEARNING-ENGINE-DESIGN.md#get_thermal_model---dict) |
| Is there a gate bridge (`k_vent_window` standing in for `k_passive`)? | No. It was removed in Issue #1512: the open-window rate is a different physical quantity from the closed-house rate, so using it as a stand-in mis-sized every heat-rate calculation and the forecast of homes that had only window data. Physics prediction needs a real closed-house `k_passive` (from passive windows); until then the forecast uses the schedule fallback. | [§Gate Bridge (removed)](05-LEARNING-ENGINE-DESIGN.md#gate-bridge-self-healing-issue-126-phase-a--removed-in-issue-1512) |
| What patterns trigger learning suggestions and how many days of data are required? | Six patterns (low window compliance, frequent overrides, high runtime, short departures, comfort violations, frequent door pauses) are evaluated after 14+ days; suggestions repeat unless dismissed, with a 7-day cooldown. | [§Suggestion Generation](05-LEARNING-ENGINE-DESIGN.md#suggestion-generation) |
| Where is the full Tier 3 spec for the v3 thermal model — pre/post/invariants, OLS contracts, EWMA table? | The Territory spec covers all observation lifecycles, all OLS solvers (including `compute_k_passive_endpoint`/`compute_k_solar`), commit routing, EWMA alpha table, rolling window constraints, the pooled cycle model (Issues #1512/#1516), swing detection, and the Issue #587 vent-split/retirement rationale. | [Thermal Model v3 — Territory Spec](thermal-model-v3-spec.md) |

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
- Minutes spent outside the target range in effect (comfort violations; planned sleep/away setbacks are not violations, Issue #1107)
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
**Unit handling (Issue #1050):** the suggestion's detail line ("mostly up by about 1°C ...") is worded in the user's unit. `LearningEngine.__init__(storage_path, entry_id="", temp_unit="fahrenheit")` receives the configured `temp_unit` from the coordinator and formats the average override magnitude with `format_temp_delta()`; stored override records are always internal °F (`old_setpoint_f` / `new_setpoint_f` are real °F floats, so a 21 -> 22 °C change is stored as 69.8 -> 71.6 and reads back as "+1°C"). History written before this fix with mixed-unit records is not migrated; the 14-day window ages it out.

### Pattern: High Runtime on Mild/Warm Days

**Detection:** Average HVAC runtime > 120 minutes on mild/warm day types.
**Suggestion:** "Would you like to add more door/window sensors, or adjust the setpoints for mild days?"
**Root cause:** Usually indicates doors/windows being left open without sensors, or setpoints that are too aggressive for the house's thermal characteristics.

### Pattern: Short Departures

**Detection:** More than 5 departures of 30–45 minutes in 14 days.
**Suggestion:** "Would you like to shorten the setback delay from 15 minutes to 5 minutes, or skip setback for departures under 1 hour?"
**Insight:** The current 15-minute delay means the setback barely takes effect before the person returns, wasting the recovery energy. Options: faster setback (saves more) or skip setback (saves the recovery penalty).

### Pattern: Comfort Violations

**Detection:** Indoor temp outside the target range in effect (see [Comfort Violations](#comfort-violations-comfort_violations_minutes)) for > 30 minutes on 5+ of the last 14 days. Since Issue #1107 a planned sleep/away setback no longer counts, so this pattern fires only when the house misses the band CA was actually holding — it no longer recommends weaker setbacks merely because setbacks happened.
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
- `k_vent_window` (hr⁻¹, negative) — the TOTAL decay rate toward outdoor when windows are open and the fan/WHF is physically **off** (it replaces `k_passive` for those hours; it is not added to it)
- `k_vent_fan` (hr⁻¹, negative) — the TOTAL decay rate when windows are open and the fan/WHF is physically **running**. The v3 forecast curve still keys `k_vent_eff` on `k_vent_window` only (Issue #587 boundary); the operation timeline uses `k_vent_fan` for window hours where the nat-vent gate would run the fan (Issue #1505, [Operation timeline: window candidates](operation-timeline-spec.md#window-candidates))
- `k_solar` (°F/hr per unit solar factor) — solar gain contribution
- `k_vent_eff` = `k_vent_window` when windows are open, 0 otherwise (forecast-time substitution keys on `k_vent_window` only, per the scope boundary above)
- `solar_factor` = sinusoidal 0 → 1 → 0 over daylight hours (local 8:00–18:00)
- `Q_hvac` = `k_active_heat` when heating, `k_active_cool` when cooling, 0 otherwise

**Retired (Issue #587):** `k_vent` (hr⁻¹, negative) — additional decay rate from HVAC fan circulation only, sourced from the now-retired `fan_only_decay` observation type. Confirmed dead code before removal: the sole ODE consumer (`_simulate_indoor_physics_v3`) was always called with `ventilation_active=False` hardcoded, so `k_vent` never affected a live forecast prediction on any install. See the Territory spec's [§k_vent / fan_only_decay retirement](thermal-model-v3-spec.md#k_vent--fan_only_decay-retirement-issue-587) for the full rationale, including why `k_vent_window`'s accumulated EWMA is deliberately *not* reset on upgrade and why vent/solar observation counts are excluded from envelope confidence grading.

**Analytical ODE solution** used for prediction and simulation:

```
T(t+dt) = T_outdoor + (T - T_outdoor) * exp(k_p * dt) + (Q/k_p) * (exp(k_p * dt) - 1)
```

**Why the run's own edges (Issues #1512, #1516):** The post-heat decay fit this model once used asked what the room does AFTER the equipment stops. That is wrong for the occupant's questions. A furnace keeps pushing warm air (and the plenum keeps radiating) for minutes after it shuts off, so the room often keeps warming; after an AC run the room warms. A "decay" requirement therefore rejected or mis-fitted exactly the furnaces and cooling runs that matter. The rate is now read from the run itself, in one line of arithmetic at the off edge (see [§Parameter Extraction](#parameter-extraction)), so what happens after the equipment stops is excluded by construction.

### v3 Parallel Observation Architecture (Issue #121)

The v2 architecture had a single `PendingThermalEvent` state machine — only one observation could accumulate at a time. v3 replaces this with a `_pending_observations: dict[str, PendingObservation]` dict keyed by observation type string. All types can run concurrently; each targets a different thermal parameter.

#### Observation Types

*Updated Issue #587: `fan_only_decay` retired outright (confirmed dead — see [§Physics Model](#physics-model-issue-114-v2--issue-121-v3) above); `ventilated_decay` split into `vent_window_decay`/`vent_fan_decay` keyed on fan/WHF state, via one shared trigger table (`_VENT_SPLIT_TYPES`) and one shared commit/abandon method (`_evaluate_vent_split_observation`) rather than duplicated per-type logic — see the Territory spec's [§k_vent / fan_only_decay retirement](thermal-model-v3-spec.md#k_vent--fan_only_decay-retirement-issue-587) for full rationale.*

| Type | Trigger conditions | Target parameter | Min samples |
|---|---|---|---|
| `hvac_heat` | `hvac_action = "heating"` | `k_active_heat` (the pooled cycle model, refitted from the stored cycles - throttled, see the cycle model; never `k_passive`) | cycle ≥ 3 min and ≥ 3 sensor steps of movement |
| `hvac_cool` | `hvac_action = "cooling"` | `k_active_cool` (same model) | same gate |
| `passive_decay` | HVAC off, fan off, windows closed, `\|ΔT\|` ≥ 3°F (indoor vs outdoor) | `k_passive` | 30 |
| `vent_window_decay` | Window/door sensor open, fan/WHF **off**, HVAC off | `k_vent_window` | 20 |
| `vent_fan_decay` | Window/door sensor open, fan/WHF **on**, HVAC off | `k_vent_fan` | 20 |
| `solar_gain` | HVAC off, fan off, windows closed, `T_in > T_out`, daytime (local 8:00–18:00) | `k_solar` (subtracts `k_passive`'s conductive share first — Issue #587 Defect B) | 20 |

**HVAC contamination rule:** When HVAC starts, all non-HVAC observations in progress are committed (if sufficient samples) or abandoned. HVAC operation contaminates passive/vent/solar signals.

**No plateau guard, no post-run window (Issue #1512):** the old guard (commit only if the room fell ≥ 0.3 °F after the peak) and the 30-minute post-heat stabilization wait are gone. The run is committed, or rejected with a stated reason code, at the off edge. The committed `peak_indoor_f` is the run's extremum (highest reading for heat, lowest for cool).

#### Sampling Cadence (Issue #122 H1 — Decimation)

The coordinator polls every 30 seconds. Sampling all slow decay phenomena at poll rate yields noise — the temperature change between two adjacent samples is dominated by sensor quantisation, not the signal. Per-type sample gates enforce a minimum wall-clock interval between recorded samples:

| Type | Sample interval | Rationale |
|---|---|---|
| `hvac_heat` / `hvac_cool` (active phase) | Every poll (no gate) | Active HVAC is fast; all samples needed |
| `passive_decay` | 5 min (`THERMAL_PASSIVE_SAMPLE_INTERVAL_S`) | Slow drift; 5-min samples give clean signal |
| `vent_window_decay` | 5 min (`THERMAL_PASSIVE_SAMPLE_INTERVAL_S`) | Window-open drift is slow |
| `vent_fan_decay` | 5 min (`THERMAL_PASSIVE_SAMPLE_INTERVAL_S`) | Same rationale as vent_window_decay; retired `fan_only_decay`'s separate 2-min gate is not carried over |
| `solar_gain` | 5 min (`THERMAL_SOLAR_SAMPLE_INTERVAL_S`) | Solar ramp is slow; 5-min gives adequate resolution |

The gate is stored as `"last_sample_time"` in the observation dict. Section A of `_sample_all_observations()` checks `elapsed_since_last >= _interval_s` before appending a sample.

**Convergence improvement:** With 5-min decimation, a 6-hour passive overnight window yields ~72 samples vs. the noise-ridden 720 samples at 30-s poll rate. The 30-sample minimum (`THERMAL_PASSIVE_MIN_SAMPLES`) is calibrated to 5-min intervals, requiring ~2.5 hours of clean signal.

#### Rolling-Window Commits (Issue #122 H2)

Long observation windows are accurate but slow to accumulate. Rolling commits break each long observation into overlapping 30-minute windows. After `THERMAL_ROLLING_WINDOW_MINUTES (30 min)` elapses, the observation is committed and immediately restarted (if conditions still hold on the next poll).

Rolling commit rules (in `_commit_rolling_window_obs()`):
- Minimum 3 samples in the window (absolute floor)
- For `passive_decay` and `solar_gain`: total indoor ΔT ≥ `THERMAL_ROLLING_MIN_DELTA_T_F (0.2°F)` — prevents noise-fitting on near-flat data
- For `vent_window_decay` and `vent_fan_decay`: the ΔT guard is skipped (`skip_delta_guard=True`); the signal guarantee for those types is the indoor–outdoor differential (checked by the trigger condition), not the temperature trend

**Convergence impact:** Rolling windows produce ~16 `passive_decay` commits per overnight period (6 hours ÷ 30 min) vs. 1 per full-night commit in v2. The model reaches 5% accuracy in ~4 nights (α = 0.05 EWMA) vs. ~60 nights before.

#### Wall-Clock Abandon Timeout (Issue #122 H4)

`vent_window_decay` and `vent_fan_decay` (formerly `ventilated_decay` and the retired `fan_only_decay`) have a 60-minute wall-clock abandon limit (`THERMAL_DECAY_MAX_WINDOW_MINUTES`). If 60 minutes elapse without the rolling-window commit threshold being met and without the signal crossing the minimum ΔT guard, the observation is abandoned with reason `"max_window_elapsed_low_signal"`. This prevents stale, low-signal observations from persisting indefinitely when a window is left open in near-equilibrium conditions.

`passive_decay` and `solar_gain` do not have this wall-clock timeout — they rely on rolling commits to bound window length.

### Observation Lifecycle Methods

| Method | Owner | Role |
|---|---|---|
| `_start_hvac_observation(session_mode)` | `coordinator.py` | Begins `hvac_heat` or `hvac_cool`; abandons/commits non-HVAC obs; records the on-edge indoor reading |
| `_start_decay_observation(obs_type)` | `coordinator.py` | Creates `passive_decay`, `vent_window_decay`, `vent_fan_decay`, or `solar_gain` observation dict |
| `_sample_all_observations()` | `coordinator.py` | Section A: samples all active obs with per-type decimation gate. Section B: checks trigger conditions (vent split via `_VENT_SPLIT_TYPES` table) and starts new non-HVAC obs. Section C: evaluates exit conditions and calls commit/abandon per type (`_evaluate_vent_split_observation` for both vent types) |
| `_end_hvac_active_phase(obs_type)` | `coordinator.py` | At the off edge closes the pending HVAC observation (no rate is committed here any more: the cycle recorder closes the cycle and the cycle model refits, Issues #1512/#1516) |
| `CycleRecorder` / `CycleStore` | `cycle_recorder.py`, `cycle_store.py` | Records every heating or cooling cycle with the room readings, outdoor readings and flags around it, and stores it in `climate_advisor_cycles.json` (see [§The cycle model](#the-cycle-model-issues-1512-1516)) |
| `fit_cycles(cycles, mode, prior, now_ts)` | `cycle_model.py` | The pooled cycle model (pure math): each closed cycle is judged alone at once, the full refit per mode is throttled (30 min / 3 new eligible cycles / first eligible cycle / startup); `learning.apply_cycle_fit` stores the result |
| `_commit_observation(obs_type)` | `coordinator.py` | Passes observation dict to `learning._commit_event_from_dict()`; pops from `_pending_observations` |
| `_commit_observation_if_sufficient(obs_type, abandon_reason)` | `coordinator.py` | Commits if `len(samples) >= min_samples` (with short-window ΔT guard), else abandons |
| `_commit_rolling_window_obs(obs_type, obs)` | `coordinator.py` | Commits a 30-min rolling window slice; pops observation so it can be restarted |
| `_abandon_observation(obs_type, reason)` | `coordinator.py` | Discards pending observation; logs WARNING with type, reason, sample count, and ΔT |
| `_update_thermal_model_cache(obs)` | `learning.py` | Applies committed observation to EWMA cache; routes each mode to the correct parameter |

**Pre-heat buffer (removed, Issue #1512):** it existed to feed a closed-house fit from the minutes before a run and a post-run decay fit. The cycle recorder keeps the readings around each cycle itself, so nothing consumes it. A heating/cooling session that starts while the indoor (or outdoor) reading is unavailable is still not started and records an `indoor_unavailable` rejection (Issue #1035) — see [thermal-model-v3-spec.md](thermal-model-v3-spec.md#indoor-sensor-outage-handling-issue-1035).

### Parameter Extraction

<a id="the-cycle-model-issues-1512-1516"></a>
**`k_active` — the pooled cycle model (Issues #1512, #1516).** What the person living in the home sees: a heating and cooling rate that is never worse than the typical-house default, gets better with every informative cycle, says honestly how sure it is, and does not jump at upgrade. Evidence behind the design (poc2 backtests, #1512/#1516): a single run on a whole-degree sensor cannot identify the rate (a 5-8 minute run moves the room by one step or none), a per-run formula therefore swings wildly on exactly the homes that matter, and a duty-balance fit over 30-minute chart_log rows failed on real data. Pooling every cycle with a prior and an honest grade fixes both.

*What is recorded.* `cycle_recorder.CycleRecorder` (pure state machine) turns the thermostat's on/off edges, every reading of the temperature sensor the home uses (with seconds), outdoor readings, window/fan/nat-vent flags, setpoint changes and gaps into cycle dicts. A cycle closes at the next on edge, 45 minutes after off, a mode switch, a setpoint change, or a gap (a restart, or the thermostat going unavailable: **not** treated as an off edge). A cycle cut by a restart is kept with `cut=...` and only its clean pre-gap part is fitted (in-flight state is still not persisted, #1437).

<a id="blower-around-the-cycle-issue-1512-reopened"></a>
*The blower around the cycle (Issue #1512 reopened, owner 2026-11-12).* What the occupant experiences: on the owner's Ecobee every heating run is followed by `heating -> idle` and, 1 s later, `hvac_action: fan` for 2 minutes; the room kept rising 67.1 -> 68.4 °F after the stop, and 0.9.31 recorded none of it (the `fan` read as running, `_cycle_dir` mapped it to `heat`, so the real cycle closed at the stop with no coast and a 2-minute phantom run was rejected as too short), so pre-heat could not allow for the coast and the room overshot. Now: once the thermostat has reported `heating` or `cooling` (`coordinator._thermostat_reports_action`, learned from the thermostat and persisted as `thermostat_reports_action`), its `fan` in a heat/cool mode is the blower alone, never a heating or cooling run (`coordinator._fan_is_blower_only`, `hvac_action.thermostat_reports_running(..., fan_is_blower=True)`). This covers every sequence, heat and cool alike: overrun `heating -> idle -> fan -> idle` (Ecobee) and `heating -> fan -> idle`, lead-in `idle -> fan -> heating`, and Off mid-run followed by overrun (`fan` whatever the mode). The fan period belongs to the cycle it follows (the coast is recorded straight through it: no edge, no exclusion flag, because the exclusion flag is Climate Advisor's own fan or nat-vent, not the thermostat's blower) or precedes (the lead-in readings are the run's starting level; the run starts at the burner/compressor). The legacy `hvac_heat`/`hvac_cool` observation, the runtime timer and the session no longer start on the blower. `fan_only` mode is unchanged (still a fan_only session, #912), and a thermostat that never reports heating/cooling keeps the #93 mode fallback (its `fan` is all it reports). On the very first run after install, before the thermostat has ever reported `heating`, a lead-in is still read as the start; when `heating` follows a `fan`-opened cycle, the cycle's start moves to the burner (`CycleRecorder.reanchor_on`). The post-run blower also counts as the #618 post-compressor phase for the fan reconcile when it arrives as `idle -> fan` within `BLOWER_OVERRUN_LINK_S` (600 s) of the stop (its mirror: it was judged only on `heating/cooling -> fan`), its origin is recorded as `post_heat_tail`, and an Off-mode overrun is not a state contradiction. Whole-house-fan homes are unchanged: the thermostat blower is not the whole-house fan (#1197/#1499/#1500).

*Which sensor is heard (Issue #1512 verifier finding 1).* The readings come from whatever entity the band reading uses right now (`coordinator._primary_indoor_entity()`): the dedicated sensor / `input_number` through its own listener; on the default `climate_fallback` source the thermostat's `current_temperature`, or, while the resolver uses it (#1230 R1b), the thermostat's finer companion sensor, which has its own listener (every companion candidate is subscribed and re-synced on the 5-minute tick; only the one in use is recorded). Each change is recorded only when its entity IS the band reading's, so the thermostat's attribute updates and the companion's ticks never double-record, and one cycle never mixes two sensors (the recorder keeps a cycle's first entity).

*A setpoint change keeps the run (finding 3).* A commanded change reaches HA as the setpoint first and the action a few seconds later (CA's own moves too), so a setpoint change during a run ends the run at the change and keeps the run portion: an off edge (or mode switch) within `CYCLE_SETPOINT_OFF_WINDOW_S` (60 s), or in the same event, is the run's real end (`t_off`), and nothing after it is recorded (no post-change coast); if the equipment keeps running past the window the cycle closes at the change with no off edge (its run ends at its last sample, no coast or off-edge tail invented) and a continuation cycle opens at the change, chasing the new setpoint. A change while coasting (off edge already seen) closes the cycle there: run and coast so far kept. All carry `cut="setpoint_change"` and are fitted like any cycle; only a run portion too short to qualify is reported as `cycle_setpoint_change`.

*The cycle file.* `cycle_store.CycleStore` keeps the cycles per zone in `climate_advisor_cycles.json` (separate from the chart_log: [chart-log-spec.md](chart-log-spec.md#relationship-to-the-cycle-file)); retention is evidence-based (Issue #1512 reopened, owner 2026-11-12: "wouldn't want to age the data out if it was more correct"): each stored cycle carries its weight in the last pooled fit (`w`, written by every refit through `CycleStore.set_weights`; eligible cycles their information weight, the pool's other cycles 0, a just-recorded cycle counts as full weight); a cycle more than 90 days older than the newest cycle of its mode leaves only when that weight fell below `CYCLE_RETAIN_MIN_WEIGHT` (0.01: displaced by newer information, or contradicted), and the memory backstop (at most 3000 cycles, half per mode, so a busy cooling summer never pushes last winter's heating cycles out) evicts the mode's LOWEST-weight cycle first (ties oldest first). A long accurate run therefore stays however many short pulses follow, a contradicted one leaves, and a quiet season prunes nothing, atomic write, mode 0600, never raises. Readings are kept at their **native resolution** (finding 6): each series stores integers with the fewest decimal places (0-3) that represent it exactly, so a whole-degree thermostat stores whole degrees and a 0.01 °C sensor (0.018 °F steps) keeps every step (format v2, delta-encoded; a v1 file with fixed centi-degrees is migrated on load; downgrades are not supported). In memory each cycle is compact (`array('i')` columns and a flag `bytes`): about 29 bytes per reading including the per-cycle overhead, against about 175 for lists of Python objects (3000 cycles of 40 readings: 3.4 MB instead of 21 MB). The file is written periodically (`CYCLE_STORE_SAVE_INTERVAL_S`) and at HA stop / unload (under the cycle lock, so an older snapshot can never land last). A crash may lose the most recent cycles (the learning file is saved after every refit, the cycle file only periodically). The next refit then works from the cycles that were saved, exactly as if the lost ones had never happened: the current fit is not held, because a fit has left the pool only when the cycles it was fitted on (`t_first` .. `t_last` of its eligible cycles, same sensor) are all gone (a sensor swap or retention), not when its newest few are missing; and the survivors are not new evidence (`t_seen`). Before the final verification of #1512 the test looked at the newest cycle only, so a crash made the fit the prior of a refit over its own surviving cycles (evidence 40.75 instead of 22.0, the rate answering a real change about half as fast). Startup recovers from a missing, partial or corrupt file without an error (unreadable rows dropped, duplicate ids kept once), and nothing is counted twice: a cycle is new evidence only when it is newer than any an earlier fit saw (`t_seen`).

*The model* (`cycle_model.fit_cycles`, pure, shared with `tools/thermal_replay.py --cycles` and `tools/predict_backtest.py`). Per mode, level-mode joint fit:

```
T_j = a_cycle + q·Q_j + k·K_j + c·C_j(tau)
```

`a_cycle` is each cycle's own starting level (so the room's start does not matter), `q` the rate while the equipment runs (°F/h), `k` the leak, `c·tau/60` (`coast_area_f`) how far the room keeps going after the equipment stops (positive: keeps warming after heat or cooling after cool; negative: gives some back; 0: stops dead). `tau` (1.5-20 min) is profiled on a grid. Earlier cycles' off-edge tails are superposed back 120 minutes, so a cycle that starts while the room is still coasting is not blamed. Robustness: Hampel cleaning of spikes (half-window 5 min, at least 5 readings, threshold `max(3.5·1.4826·MAD, 2·res, 0.5 °F)`), a restart rule (after a gap longer than `max(2.5 min, 2.5×` the median step) leading readings that deviate more than `max(2·res, 0.8 °F)` from the following 8-minute median are dropped), Huber IRLS (threshold 1.345·scale) with scale `max(1.4826·MAD/√max(dof, 0.2), max(0.3·res, 0.05 °F))`, MAD = median |residual|, `dof` = the residual degrees-of-freedom fraction `max(N − cycles − parameters, 1) / N` over the N readings (poc2 `est5`; an earlier text here said `0.6·res`, which the code never used), bounds after the fit.

*The prior (never worse than the default).* A ridge prior pulls `(q, k, c|tau)` toward a starting guess by that guess's own uncertainty, so the first cycle already counts and a poor cycle can only move the answer a little. Fresh home: the typical house, `q_heat 8 ± 5 °F/h`, `q_cool −8.83 ± 5.5`, `k −0.03 ± 0.06`, coast area `0 ± 2 °F`. Existing home: its stored rates weighted by the evidence still behind them (`rate_prior` with `n`, scaled by its `keep` share, see the rule below; `cycle_model.prior_from_cache`), so nothing jumps at upgrade and nothing drifts in a quiet season. Held fit (finding 4): when the cycles behind the last usable fit are not in the pool (a sensor swap: the pool is the newest sensor's cycles; or the first cycle after a season longer than the store's 90 days, which takes the old cycles with it) the previous fit is the q prior (`cycle_model.held_candidate`: its variance inflated by `1/keep`, combined with the typical house; the legacy value is not counted again, the held posterior already carries it) and is kept as `cycle_fit_<mode>["held"]` with `keep` 1.0. On a v3 zone, while no new fit is usable, the rate stays at that q (`learning._held_rate`: `keep·q_held + (1−keep)·aged`, so exactly q_held until new cycles displace it), never snapping back and never fading with time; new cycles lower `keep` by the rule below, the new fit blending toward its own cycles by their share, and the held fit is dropped once `keep < THERMAL_EVIDENCE_MIN_KEEP` (0.01: about 133 agreeing or 33 contradicting new cycles). The old "dropped after about 140 days" rule is gone.

**Evidence fades only on new evidence (owner decision 2026-11-11).** Evidence behind a learned rate shrinks only when newer measured evidence replaces or contradicts it, never with the passage of time alone: a house does not change over a quiet season, so with no informative new cycle the rate the decisions use, and the confidence in it, stay exactly as they are. One rule for heat and cool, v3 and v4 zones: carried evidence (the pre-upgrade `rate_prior` count of a rate, a held cycle fit) holds a share `keep` (1.0 at first) that each new eligible cycle lowers, `keep ← keep · 0.5 ** (new / THERMAL_EVIDENCE_HALF_N)` with `THERMAL_EVIDENCE_HALF_N` = 20 cycles, or with `THERMAL_EVIDENCE_HALF_N_CONTRADICT` = 5 when the fit contradicts the value it was weighed against (`|q − prior_q| > 2·prior_sd_q`); "new" means newer than any eligible cycle an earlier fit saw (`cycle_fit_<mode>["t_seen"]`), so a refit of the same cycles or a restart never counts twice, and after a crash that lost the newest cycles the refit over the survivors neither holds the fit nor counts them again. Inside the cycle model a cycle's weight halves when the newer cycles' INFORMATION reaches 20 times its own (below), and the cycle file keeps a cycle by its weight, not its age (above: past 90 days back from the mode's newest cycle only a weightless cycle leaves; the cap evicts the lowest weight).

<a id="weight-by-information-issue-1512-reopened"></a>**Weight by information, not count (Issue #1512 reopened, owner 2026-11-12: "long runs that improve accuracy keep their value longer than a short run").** A cycle's information `I` is the Fisher information it alone carries about `q` once its coast is profiled out (`cycle_model._cycle_information`: the run-hours' spread over its readings less the part the coast response at tau 4 min could also explain; a 5-minute pulse is nearly indistinguishable from a coast and carries little, a 40-minute recovery a lot). Cycle `j`'s weight is `0.5 ** (ΣI_newer / (20 · I_j))` (`information_weights`): with equal cycles exactly the old `0.5 ** (newer / 20)`; a long accurate run keeps about 90 % of its weight after 40 short pulses that together carry twice its information (the count rule had it at a quarter), and a short pulse yields quickly to a long run. Contradiction still speeds displacement: after the first fit each cycle's own q (the pooled fit plus the projection of its residuals on its run-hours) is compared with the precision-pooled q of the cycles newer than it, and a cycle they contradict by more than three combined sds (one test per cycle, so two would flag agreeing cycles by chance) yields at 5 instead of 20 (`contradicted_by_newer`; one reweighting and one refit, deterministic). At most 60 cycles are fitted: the 60 with the largest weights (a long run is not pushed out by a run of pulses). The carried evidence's `keep` uses the same unit: the new cycles count `ΣI_new / I_ref`, `I_ref` the pool's median eligible cycle (`info_ref`; a held fit uses its own pool's `info_ref`), so an average new cycle counts 1 as before, a long accurate one more, a short pulse a fraction (`learning._new_information`; an older result without information counts cycles). Still evidence only: no clock, deterministic. k_passive's pre-upgrade evidence follows the same rule: its `keep` halves for every 20 new passive observations (each a single fit: always the agreeing rate). Old values still age out (age out, never reset): the clock is new evidence, not the calendar. The stored observations behind the aged rates follow the same rule (Issue #1523): each one's weight halves per 20 newer observations of its own rate, and each kind (passive, vent_window, vent_fan, solar, heat, cool) keeps its own share of the 200 kept, its own oldest leaving first, so a kind that goes quiet for a season (no closed-house window all summer) keeps its observations and the rate they give, and the first fits after it move the rate gradually. No outdoor-dependent capacity term and no thermal-mass model are used (tested in poc2, not shipped); a solar regressor exists behind a constant and is OFF.

*Which cycles count (gates).* The sensor's resolution is inferred per entity as the most common non-zero step between consecutive readings (reset when the sensor is swapped). A cycle is **eligible** only when it ran at least 3 minutes and the readings moved by at least **3 resolution steps** within `[on, off + 3 min]` (one extreme dropped each side). With no eligible cycle the prior is returned unchanged (grade `none`). On coarse sensors (step ≥ 0.5 °F) `k` is pinned at the prior; on fine sensors it is pinned until 3 eligible cycles. If the reading reached its final level at least 10 minutes before the off edge, the effective off edge is the moment it reached it (plateau trim). Coarse pools with few eligible cycles have their variance inflated by `(3/ne)²`. At most 60 eligible cycles are used (the 60 with the largest weights), each weighted by newer information (D35, above; no calendar clock). Windows open, fan or nat-vent during the cycle exclude it from the fit (the exclusion codes below); a setpoint change only ends the cycle (above); solar is not excluded (poc: exclusion destroys the cooling data).

<a id="sensor-lag-issue-1512-reopened"></a>*Sensor lag (Issue #1512 reopened).* A room sensor reads the air through a first-order lag of a minute or more. With lag the reading's rise is delayed after the on edge and catches up after the off edge; 0.9.31 read that catch-up as "coast", so a big system's short runs looked 22-37 % weaker on a fine sensor (graded `high`). A true coast does not delay the start, so the two separate: the run-hours `Q` and the coast response `C` are passed through the lag in closed form (`_run_hours`, `_coast`), the lag is profiled jointly with tau on `THERMAL_CYCLE_LAGS` (0, 1, 2, 3, 4.5 min) and, with it, an edge-report delay on `THERMAL_CYCLE_EDGE_DELAYS` (0, 0.5, 1 min: the thermostat reports its on/off edge after the equipment changed, as a polled or cloud thermostat can (the dev sim's `edge_report_delay_s`); the run is then shifted earlier; a delay carries a prior penalty of 4 in −2 log units, so it is used only when the data show it), all Bayesian-averaged together (`timing_grid`; `edge_delay_min` reported), so `q` is fitted net of the lag and the spread between lags is part of `sd_q`. The lag is a property of the sensor, so it is pooled over heating and cooling: the refit passes the other mode's own lag profile on the same sensor (`lag_evidence`; each mode returns only its own data's profile, `lag_profile`, so they never echo). Identified when its posterior sd ≤ 0.6 min (`lag_identified`). Pi CPU: every (delay, lag) point on two taus first, the full tau grid only for points with ≥ 0.5 % of the posterior; the k-identifiability check below refits at the chosen point only (60 cycles: about 1 s on a desktop against 0.29 s before). The q bounds are 0.5-25 °F/h (were 15: an 18 °F/h oversized system used to be clamped at 15 and graded high).

*How sure (grade).* `er_q = 1 − posterior variance / prior variance`. `none`: no eligible cycle. Coarse sensor: `low`, or `medium` when `er_q ≥ 0.95` and at least 2 eligible cycles; **never `high`**. Fine sensor: `medium` when `er_q ≥ 0.6` and `ne ≥ 2`, `high` when `er_q ≥ 0.95` and `ne ≥ 4`, else `low`. The grade reflects information (Issue #1512 reopened, `grade_caps`): with `k` pinned, the q uncertainty `k`'s prior sd causes (`q_sd_from_k`, from the normal equations against the K column) is added to `sd_q`, and when `k` is only the typical-house value (`k_source` `typical`: no learned `k_passive` with evidence ≥ 1) and that part is more than 10 % of |q| (`k_dependent`), the grade is `low` at most (`k_typical_prior`). With `k` fitted but not learned elsewhere, a profile check refits with `k` pinned at the fitted value and at a plausible alternative (the typical prior two sds leakier, −0.15, or a third of it, −0.01); when the data fit the alternative about as well (within 3 sd, `(Δsse)/(σ²·deff) < 9`) but its q differs by more than 10 %, q depends on an unlearned k (`q_if_k_alt`, `k_dependent`). Real-HA finding: a long, slow undersized run in mild air (q is a small difference of climb and leak) was graded `medium` 31 % low before this check. The prior's `k` is now the CURRENT learned `k_passive` (with the larger of the carried and the passive-counter evidence), not the upgrade-time value. Until the lag is identified, a pool whose median eligible run is shorter than 3 × 3 min cannot be `high` (`lag_unidentified`).

<a id="whole-degree-sensors-issue-1512-reopened"></a>*Whole- and half-degree sensors (Issue #1512 reopened, item 5).* In a maintenance cycle the visible rise is the thermostat's swing (about 1 °F) whatever the system's size; a bigger system only makes the run shorter. A sensor that reports whole degrees (resolution ≥ 0.9 °F, `sensor_class` `whole`) or half degrees (≥ 0.45 °F, `half`) therefore sees one or two steps per normal cycle, below the 3-step gate: normal cycles cannot teach the rate however many there are. Setback recoveries (several degrees) still can. Analysis (the house-model row), Debug and the AI context then say so in plain words, with what is used instead (`cycle_model.coarse_sensor_notice`: `rate_in_use` `measured` from longer runs, `learned` (the rate learned before, carried) or `default` (a typical home's); there are no user-configured heating/cooling rates) and that a finer sensor (for example the thermostat's companion temperature sensor) fixes it. Exposed on the cycle rows (`coarse_sensor`) and stored as `sensor_class` on the fit.

<a id="what-each-home-can-expect"></a>**What each home can expect** (production `fit_cycles` on `tests/test_cycle_model_1512.py` `synth`: truth k −0.04, outdoor ~40 °F, 2-min sensor lag, typical-house prior, 30 cycles; `grid3.py` re-run on 0.9.32; error of q and grade):

| System (heating rate) | Sensor | Maintenance cycles (1 °F swing) | Setback recovery (5 °F) |
|---|---|---|---|
| Undersized, 2 °F/h (68-min runs) | whole (1 °F) | cannot learn (1 step/cycle): default kept | 16 %, `low` (k typical) |
| | half (0.5 °F) | cannot learn (2 steps) | 15 %, `low` (k typical) |
| | fine (0.1-0.18 °F) | 0-6 %, `high` | 1-2 %, `high` |
| Undersized, 4 °F/h (21 min) | whole | cannot learn | 10 %, `low` |
| | half | rarely (a few cycles reach 3 steps), `low` | 7 %, `low` |
| | fine | 0-1 %, `high` | 0 %, `high` |
| Extreme-weather, 6 °F/h (13 min) | whole | cannot learn | 3 %, `low` |
| | half | cannot learn | 5 %, `low` |
| | fine | 2 %, `high` | 0 %, `high` |
| Normal, 10 °F/h (7 min) | whole | cannot learn | 2 %, `low` |
| | half | cannot learn | 3 %, `low` |
| | fine | 2-4 %, `high` (0.9.31: −22 %, `high`) | 0 %, `high` |
| Normal, 15 °F/h (4 min) | whole | cannot learn (0-1 step) | 2 %, `low` |
| | half | cannot learn | 1 %, `low` |
| | fine | 6-8 %, `high`/`medium` with 23-30 cycles; 11-16 %, `medium` with 8-10 (0.9.31: −37 %, `high`; an earlier 0.9.32 draft showed 0 % only because q was clamped at the old 15 °F/h bound) | 1 %, `high` |

<a id="real-ha-check"></a>**Real-HA check** (`tests_ha/test_cycle_learning_home_types_1512.py`, the real coordinator against the dev thermostat sim's HVAC profiles, 8 simulated hours, automation off, a 1 °F swing, a flat forecast so Climate Advisor's outdoor reading is the sim's own outdoor air (50 °F, cool 95 °F); the sim's house leaks at k −0.15 /h, about 4 times the table's). The sim reports each on/off edge at the tick it is decided, as a directly connected thermostat does; its `edge_report_delay_s` models a cloud-polled one (until Issue #1512 reopened every edge read 30 s late, a tick-order artifact the fit measured as a 30 s delay). The tests assert these tolerances:

| Point | Measured (0.9.32) | Asserted |
|---|---|---|
| Heat, normal 12 °F/h, tenths (companion), lag 0, no overrun | 11.77 (−2 %), `high` | within 5 %, `medium`/`high` |
| Same, edges reported 30 s late (`edge_report_delay_s` 30) | 11.93 (−0.6 %), `high`, edge delay 0.5 min found | within 5 % |
| Heat, normal 12, tenths, lag 2 min, 2-min overrun | 10.89 (−9 %), `high`; one cycle per run, coast recorded through the blower | within 10 %, no phantom run |
| Same, learned k −0.15 | 10.89 (−9 %), `high` | within 10 % |
| Cool, normal 12, tenths, lag 0, 2-min overrun | −11.77 (−2 %), `high`, no phantom run | within 5 % |
| Heat, oversized 18, tenths, lag 2 | 18.26 (+1 %), `high` (with the late edges: −10 %, `medium`) | within 10 % |
| Heat, undersized 2, tenths, mild air (64 °F), k typical | 1.61 (−19 %), `low` (`k_typical_prior`) | `low` with the reason |
| Same, learned k −0.15 | 1.97 (−1.5 %), `medium`, 3 runs | within 10 % |
| Heat, normal 12, whole degrees, maintenance | prior kept (`none`), notice `whole`, rate in use `default` | never learns, notice |
| Heat, extreme-weather 5, half degrees, maintenance | prior kept (`none`), notice `half` | never learns, notice |
| Heat, normal 12, whole degrees, three 5 °F setback recoveries, k typical | 9.60 (−20 %), `low` (`k_typical_prior`) | `low`, under-reads, within 35 % (the k −0.15 row below) |
| Same, learned k −0.15 | 11.52 (−4 %), `medium` | at most `medium`, within 10 % |

The 2-min-lag points stay 9 % low: with 0.1 °F readings on 9.5-minute runs a 1-minute lag plus a small positive coast fits almost as well as the true 2 minutes (pinned at lag 2 the same cycles give 11.98); they did not improve when the edges came on time (−7 % then, part of it the late edges compensating). The real-HA tier runs a representative subset (undersized 2 / normal 12 / oversized 18 heat, normal 12 cool; tenths / whole / half; lag 0 / 2; overrun 0 / 2; edges on time / 30 s late; k typical / learned; maintenance / recovery): 13 cases in about 35 s of wall time.

Recoveries on a whole/half-degree sensor are `low` because `k` is pinned at the typical value and a long run depends on it; with a learned `k_passive` they reach `medium` (`gridk.py`, 30 recoveries: within 0-2 % at k −0.04, 2-7 % at k −0.15, where k pinned at the typical −0.03 leaves q 17-58 % low and graded `low`). A cooling home is the mirror. "Cannot learn" means the prior is returned unchanged (grade `none`): the rate the home already learned, or the typical default, stays in use, and the occupant is told why.

*Outputs per mode* (stored in `thermal_model_cache["cycle_fit_heat"|"cycle_fit_cool"]` with `fitted_at` and `applied`): `q`, `sd_q`, `k_p_cycle`, `sd_k`, `coast_area_f`, `tau`, `ne` (eligible cycles), `n_cycles`, `er_q`, `grade`, `resolution_f`, and the bookkeeping the confidence and the held fit need: `prior_q` / `prior_sd_q` (the prior the data was weighed against), `ne_w` (the eligible cycles' summed weights), `entity` (the pool's sensor), `t_last` (`t_on` of the newest eligible cycle), `t_seen` (the newest eligible cycle any fit has seen: what "new evidence" is measured from); plus `held` (above) when there is one. Issue #1512 reopened adds `sensor_class`, `k_source`, `k_dependent`, `q_sd_from_k`, `lag_min`, `lag_sd_min`, `lag_identified`, `lag_profile`, `lag_pooled`, `run_median_min`, `grade_caps`, `n_contradicted`, `info_ref`, `edge_delay_min`, `q_if_k_alt` and `model_version` (2). Upgrade continuity: a fit stored by an older model (`model_version` absent or lower) stays exactly as it is until a NEW eligible cycle arrives; the startup refit of the same stored cycles is not new evidence, so the rate, grade and confidence the decisions use do not move on day one (on the owner's Zone 1 snapshot the re-fit gives q 5.641 against the stored 5.640 anyway). The first new cycle brings the new model's fit, as any new evidence does.

*Who reads it.* `learning.apply_cycle_fit(mode, result, primary_is_v3)` always stores `cycle_fit_*`. On a **v3-primary** zone it also writes `q` into `k_active_heat`/`k_active_cool` (and the derived `heating/cooling_rate_f_per_hour`) and the HVAC confidence follows the grade, so decisions use the cycle model. On a **v4-primary** zone decisions keep v4's own control rates and `k_active_*` keep their aged values; the cycle model is *shown* beside v4 (Analysis: "decisions use v4's X; the cycle model measures Y"; Debug; AI context; the engines payload's `decisions` rows with `source: "cycle"`). It never touches `k_passive`: the decisions' leak rate stays the closed-house `k_passive` (passive chart_log fits plus the aged prior). Which engine is primary is read with `prediction_engines.primary_id(config)`, as `decision_rates_summary` does (finding 8). `observation_count_heat/cool` are the legacy counters and are never rewritten by the cycle model (finding 2; see the HVAC confidence below).

*When it refits (finding 7, Pi CPU).* Every closed cycle is stored and judged alone at once (a one-cycle fit, a few milliseconds: its rejection reason, and whether it is eligible). The full refit of its mode, in the executor over `store.cycles(mode)` (every stored cycle of the mode) with the prior from the cache (the pool is built, the prior computed and the model fitted in the executor, under the cycle lock), runs when `CYCLE_REFIT_MIN_NEW_ELIGIBLE` (3) new eligible cycles wait, when the mode has nothing learned yet (its first eligible cycle is used at once), or `CYCLE_REFIT_MIN_INTERVAL_S` (30 minutes) after the last refit (the 5-minute tick picks up a waiting refit). It always runs once at startup for every mode with stored cycles. A full refit costs about 0.3-1.8 s on a desktop since the sensor-lag profile (Issue #1512 reopened; it was 0.1-0.6 s; several times that on a Pi), plus one more fit when newer cycles contradict older ones, so this caps it at about two per mode per hour instead of one per cycle. learning.json is saved after a refit.

A cycle that teaches the model nothing records a rejection with a persisted code (surfaced by Analysis, Debug, AI, `tools/learning_db.py`, `tools/thermal_health.py`):

| Code | Occupant-level meaning |
|---|---|
| `cycle_too_short` | The cycle ran under 3 minutes: too short to measure |
| `cycle_signal_below_resolution` | The room moved fewer than three sensor steps: on a 1 °F sensor a short cycle is not a measurement (honest, not a failure) |
| `cycle_excluded_window_fan` | A window was open, or the fan ran, during the cycle |
| `cycle_setpoint_change` | The setpoint changed and the run before the change was too short to measure (a longer run portion is kept) |
| `cycle_gap_restart` | A restart cut the cycle (its clean pre-gap part is still used) |
| `cycle_thermostat_unavailable` | The thermostat went unavailable during the cycle |
| `cycle_no_off_edge` | The cycle had no end to measure to (no off edge and no usable run part) |
| `cycle_sensor_swap` | Recorded on an earlier indoor sensor than the one the home uses now (the pool starts again with the new sensor; the last fit is held, above) |
| `ols_bounds` | The fit fell outside the believable range (`THERMAL_K_ACTIVE_*`) |
| `new_session_started`, `fan_activated`, `hvac_mode_switch`, `indoor_unavailable` | Operational interruptions of an observation (older observation types and the settle logic) |
| `hvac_run_too_short`, `hvac_signal_below_resolution`, `no_k_passive` | **Historical.** Written by the retired per-run estimator; they stay in stored history (labels kept, marked old method) until they age out |

**Retired (Issues #1512/#1516 final design).** The per-run run-edge estimator (`compute_hvac_run_rate`, `record_hvac_run`) and the duty-balance fit (`hvac_rate_fit.py`, `record_duty_balance_fit`, `_run_duty_balance_chart_log_fit`) are gone: the cycle model supersedes both, and the duty-balance fit failed on real data. Their stored observations stay in `thermal_observations` and age out through `aged_rates` (no wipe, no reset).

**`k_passive` — the closed-house rate** comes only from closed-house data: passive-window fits (endpoint and block-OLS over chart_log, `THERMAL_MIN_*`, R² ≥ 0.2) (the retired duty-balance fit's old observations age out). Chart_log passive stretches only start after the house has settled from HVAC: `_ventilation_row_states` carries `settling_hvac` (a row within `THERMAL_COAST_MAX_S` of the off edge, the first non-heating/cooling row after a heating/cooling row; the coast intervals are excluded by this flag, not averaged into the fit), and the passive, windows-only and solar extractors treat it as not settled.

**Ageing instead of EWMA-forever (Issue #1512).** `k_active_heat`, `k_active_cool` and `k_passive` are recomputed from the retained `thermal_observations` (each kind keeps its own share of the 200, its own oldest leaving first, none by date: Issue #1523) after every commit and on load: a weighted median with weight = grade weight (high 1.0, medium 0.6, low 0.3) × method weight × `0.5 ** (newer / THERMAL_EVIDENCE_HALF_N)` (20), `newer` being the observations of the same rate that arrived after it (Issue #1523: was `0.5 ** (age_days / 21)`). Observations of the retired methods (`run_edge`, `duty_balance`) and chart_log passive fits from `endpoint`/`block_ols` weigh 1.0 and age out on the same clock; legacy observations (no `method`: the old post-fit and proxy commits) weigh `THERMAL_LEGACY_OBS_WEIGHT` (0.25). Nothing is wiped and nothing resets: the first load stores the current cached values as `rate_prior`, one pseudo-observation per rate that carries the evidence behind it: its weight is `THERMAL_LEGACY_OBS_WEIGHT` × `n` × its newer-evidence share (`k_passive`: the prior's `keep`, halved per 20 new passive observations; heat/cool: halved per 20 retained observations of that rate stamped after it; with no new observation nothing moves), where `n` is the observation count behind the cached value at upgrade (`observation_count_heat` for `k_active_heat`, `observation_count_cool` for `k_active_cool`, passive + heat + cool for `k_passive`, since all of those fed the old EWMA). A weight of 0.25 for a value backed by hundreds of observations would let a handful of new fits yank it (on one real DB `k_passive` moved −0.048 → −0.026 on first load); with `n` the first load changes nothing, and the prior gives way as newer observations accumulate. With no observations and no prior the cached value is kept. The other EWMA fields (solar, vent, swing) are unchanged; `observation_count_heat/cool` stay the legacy counters (the cycle model never rewrites them; the HVAC confidence reads the evidence carried instead, see `get_thermal_model()` below), and a held cycle fit takes part in the heat/cool ageing on a v3 zone (see the cycle model above).

**Issue #1505: the live vent observations no longer fit their own samples.** Like `passive_decay` since Issue #137, `vent_window_decay` / `vent_fan_decay` only mark the stretch: when it ends (fan started, stopped or paused, windows closed, heating/cooling started) the chart_log fit (`_run_vent_*_chart_log_fit`, endpoint + block-OLS) learns from every window of the last two days not already recorded (#1476 dedupe); at the rolling-window cap the observation restarts without a fit (its window is still growing). Both chart_log extractors read one row rule, `_ventilation_row_states`: the physical fan (`fan_running`, legacy `fan` only on pre-#331 rows), a paused session is neither regime, and a stretch starts only after `THERMAL_VENT_POST_FAN_SETTLE_MINUTES` (120) after the fan stops and `THERMAL_POST_WINDOW_CLOSE_SETTLE_MINUTES` (30, closed-up fits) after the windows close; the solar guard (no window starting or ending 08:00-19:59) applies to both. Why: on Zone 1 the live learner had stored windows-only -0.150/h against a measured ~-0.02 (paused sessions counted as fan, daytime and post-fan-stop stretches fitted, 5 % EWMA never recovering). A one-time re-learn (`vent_k_relearn_1505`) drops the old vent fits, re-fits 90 days of chart_log and seeds each rate with the median of its fits (`LearningEngine.seed_vent_rates_from_fits`; an EWMA replayed from empty is mostly its first fit).

Historically, `k_vent_window` (vent_window_decay) and `k_vent_fan` (vent_fan_decay) used the same OLS formula as `k_passive` on their respective sample sets — 1-param by default, or 2-param (`compute_k_env_solar`) when solar-factor variation across the window is sufficient to separate ventilation from solar gain. Both vent types share this identical estimator logic; they differ only in which fan/WHF state triggered the observation. `k_vent` (retired `fan_only_decay`, Issue #587) previously used the same 1-param formula but is no longer computed anywhere.

**`compute_k_solar` pattern (Issue #587 Defect B):** `k_solar` is extracted from `solar_gain` observations using the same per-interval-subtraction pattern `compute_k_active` already established for HVAC contribution:
```
k_solar_i = rate_i - k_passive * delta_i
```
averaged across intervals, bounded to `[0.0, THERMAL_K_SOLAR_MAX_F_PER_HR]`. This replaced the pre-#587 formula, which computed only a raw mean rate (`(T_last − T_first) / total_hours`) with no subtraction — crediting ordinary conduction toward a warm outdoor as if it were solar gain. `k_passive` is read from the current cached thermal model (`thermal_model_cache["k_passive"]`) as the subtrahend; if no `k_passive` is cached yet (fresh install), the observation is rejected with `REJECT_NO_K_PASSIVE` rather than falling back to the old uncorrected rate. See the Territory spec's [§compute_k_solar()](thermal-model-v3-spec.md#compute_k_solar) for the full contract.

**Confidence grades and EWMA alpha:**

| Grade | k_passive condition | k_active condition | EWMA alpha |
|---|---|---|---|
| `"high"` | R² ≥ 0.7 and ≥ 10 post samples | R² ≥ 0.7 | 0.30 |
| `"medium"` | R² ≥ 0.4 and ≥ 5 post samples | R² ≥ 0.4 | 0.15 |
| `"low"` | R² ≥ 0.2 (minimum) | any passing sanity bounds | 0.05 |
| rejected | R² < 0.2 or < 10 post samples | — | observation discarded |

The cycle model is graded by how much the evidence moved it from its prior (`er_q`, see [§Parameter Extraction](#parameter-extraction)), not by R². Old run-edge observations were graded `medium` for runs ≥ 15 min, else `low`, and old duty-balance observations by the fit's `r_squared`; they stay in the retained history and age out.

Rolling-window commits are always committed with `force_grade="low"` (α = 0.05), reflecting the shorter sample window. (The EWMA alpha applies to the non-HVAC fields; heating, cooling and closed-house rates use the ageing weighted median above, Issue #1512, and the grade weight there is high 1.0 / medium 0.6 / low 0.3.)

### `_update_thermal_model_cache()` Routing (Issue #122 E6 fix)

Each committed observation updates the EWMA cache in `learning.py`. The routing by `hvac_mode` field:

| `hvac_mode` value | Updates | Counter incremented |
|---|---|---|
| `"heat"` | `k_active_heat` (if `k_active` present; ageing weighted median, Issue #1512). Legacy `k_passive` values on old observations age out at the legacy weight; old run-edge observations carry `k_passive: None` | `observation_count_heat` |
| `"cool"` | `k_active_cool` (same; Issue #1516) | `observation_count_cool` |
| `"passive"` | `k_passive` only | `observation_count_passive` |
| `"vent_window"` | `k_vent_window` (from `k_passive` field of the obs); `k_solar` too when `two_param=True` | `observation_count_vent_window` |
| `"vent_fan"` | `k_vent_fan` (from `k_passive` field of the obs); `k_solar` too when `two_param=True` | `observation_count_vent_fan` |
| `"solar"` | `k_solar` (from `k_solar` field of the obs) | `observation_count_solar` |

**Retired (Issue #587):** `"fan_only"` mode previously updated `k_vent` (from the obs's `k_passive` field) and `observation_count_fan_only`. Confirmed dead code before removal — see the Territory spec's [§k_vent / fan_only_decay retirement](thermal-model-v3-spec.md#k_vent--fan_only_decay-retirement-issue-587). `"ventilated"` (pre-#587 tag for the single, fan-state-agnostic `ventilated_decay` type) is renamed/split into `"vent_window"`/`"vent_fan"` above — a rename, not a reuse, since the semantics genuinely narrowed (fan-state-agnostic → fan-off/fan-on specifically); old persisted observations tagged `"ventilated"`/`"fan_only"` are inert historical data.

**E6 fix (Issue #122):** The `passive` branch no longer writes `k_p` to `cache["k_vent"]` (the pre-retirement ventilation parameter). Before this fix, `passive_decay` observations were incorrectly populating the ventilation parameter.

**Confidence-exclusion decision (Issue #587):** `_grade_passive_confidence()` counts only `observation_count_passive` + `observation_count_heat` + `observation_count_cool` toward `confidence_k_passive`. `observation_count_vent_window`/`observation_count_vent_fan`/`observation_count_solar` are explicitly excluded — window-open and fan-driven ventilation decay are not close-enough proxies for envelope-only decay to safely fold into that confidence count (unlike the old `fan_only_decay`, whose fan-only-no-windows regime was a defensible approximation of passive decay). This is an intentional behavior change: houses that previously got confidence credit from `fan_only_decay` observations show a slightly lower `confidence_k_passive` after upgrade.

### Adaptive 2-Param Ventilated OLS (Issue #126)

*Historical section — describes the design as of Issue #126, when `ventilated_decay` was still a single fan-state-agnostic type. Issue #587 later split it into `vent_window_decay`/`vent_fan_decay`; the 2-param solar-separation logic described here is unchanged and applies identically to both split types (shared, not duplicated) — see [§k_vent / fan_only_decay retirement](thermal-model-v3-spec.md#k_vent--fan_only_decay-retirement-issue-587) for the split's own rationale.*

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
| D5 | Self-healing via conf_passive guard vs. data wipe | Guard (non-destructive) — **the bridge half was removed in Issue #1512** (see D27); the no-wipe principle stands | Wipe loses accumulated k_vent_window and solar history |
| D6 | 2-param result writes k_solar only vs. also k_vent_window | Both updated | k_env_vent is a cleaner estimate of ventilated decay (solar contamination removed); discarding it in favour of the 1-param result would lose information |
| D7 | k_solar from ventilated 2-param usable in all prediction paths | Yes | k_solar is a model parameter, not obs-type-specific; the ODE applies it whenever solar conditions and window state warrant |

#### Gate Bridge Self-Healing (Issue #126 Phase A) — removed in Issue #1512

This section described promoting `k_vent_window` to a stand-in `k_passive` for homes with only ventilated observations. It is **gone**: the open-window rate is a different physical quantity from the closed-house leak rate, and borrowing it mis-sized both the heat-rate calculation (the old D17 proxy) and the forecast (`physics_eligibility`, `operation_timeline.model_rates`, `_build_predicted_indoor_future`). `physics_eligibility` now needs a real closed-house `k_passive` and returns the 2-tuple `(eligible, k_passive)`: the former third `via_bridge` value was removed along with the flag (a deliberate deviation from the original spec, which kept the 3-tuple shape; every caller was updated). Window-open forecast hours still use `k_vent_window` as the window rate, as before. For a home with no closed-house rate yet, the occupant gets the schedule fallback until passive windows learn one.

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

#### History (Issue #130) and what replaced it (Issues #1512, #1516)

Issue #130 fixed zero `k_active_heat` / `k_active_cool` observations by lowering the post-heat sample minimum, adding a single-point estimator and a `k_vent_window` proxy. Issues #1512/#1516 replaced the whole post-run machinery with the pooled cycle model (see [§Parameter Extraction](#parameter-extraction)). What this means for each former decision:

| Old decision (Issue #130) | Status | Why |
|----|----|----|
| D14 `THERMAL_MIN_POST_HEAT_SAMPLES` 10 → 4 | Removed | There is no post-heat phase to sample |
| D15 remove stabilization variance gate | Moot | No stabilization wait at all; cycles are recorded and fitted together |
| D16 30-min outdoor fallback (`_last_known_outdoor_f`) | Kept | The cycle's outdoor readings still need a value at the edges |
| D17 / D21 / D22 / D24 / D25 / D26 `k_vent_window` proxy, proxy-aware `n_post` and plateau-guard bypass | Removed | The proxy is gone (D27); the plateau guard and `n_post` gating are gone (D28) |
| D18 `thermal_replay --hvac` | Replaced | `--cycles` fits the stored cycles with production `cycle_model.fit_cycles`; `--hvac` keeps the swing replay only; no second copy of the rules |
| D19 / D20 single-point estimator, `post[0].ts` as end | Superseded | `compute_k_active_single_point` remains the arithmetic for `k_solar`; the rate comes from the cycle model |
| D23 `THERMAL_HVAC_MIN_SIGNAL_F = 0.5 °F` | Superseded | The cycle gate is three resolution steps of reading movement (D29) |

#### Decision Points (D27–D33, Issues #1512/#1516)

| D# | Decision | Rationale |
|----|----------|-----------|
| D27 | Remove the `k_vent_window` → `k_passive` bridge everywhere (learning, `physics_eligibility`, `operation_timeline.model_rates`, the forecast) | The open-window rate is not the closed-house rate; a stand-in mis-sized heat rates and forecasts. Owner decision |
| D28 | Delete the post-heat phase, plateau guard, 30-min stabilization wait, `THERMAL_HVAC_MIN_DECAY_F`, `THERMAL_MIN_DECAY_F`, `THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S`, the pre-heat buffer and the post-heat fan-pulse grace | Furnaces coast and AC runs are followed by warming; "decay after the run" rejected or mis-fit the homes that matter (#1512, #1516). Rate definition = steady heating/cooling per hour (`q`), with the coast modelled separately (D34) |
| D29 | A cycle counts only when it ran ≥ 3 min and the readings moved ≥ 3 resolution steps (`cycle_model`); the old 5-min / `max(0.5 °F, 2 × resolution)` per-run gate is retired | A 1.0 °F sensor cannot show a half-degree; pooling lets many small cycles add up while single ones are not trusted |
| D30 | The cycle model never writes `k_passive`; its own leak `k_p_cycle` is a diagnostic | The closed-house rate must come only from closed-house data |
| D31 | Duty-balance fit retired | Failed on real data (30-min rows, quantised sensors); the cycle model reads the readings themselves |
| D32 | Ageing: recompute the three rates from retained observations (weighted median, weight halved per 20 newer observations of the same rate since Issue #1523 (was a 21-day half-life), legacy weight 0.25); current values kept as an aging prior weighted by the evidence behind them (`0.25 × n`); no wipe, no one-time re-learn | Nothing jumps on upgrade (the prior outweighs a handful of new fits) and gives way as newer observations arrive (the weights share one clock, so with no new observation nothing moves); nothing falls to defaults |
| D33 | Every rejection carries a persisted code (`cycle_too_short`, `cycle_signal_below_resolution`, `cycle_excluded_window_fan`, `cycle_setpoint_change`, `cycle_gap_restart`, `cycle_thermostat_unavailable`, plus the operational ones; the old `hvac_*` run codes stay as history) plus values | "Rejected cycles say why" — surfaces (Analysis, Debug, AI, `tools/learning_db.py`, `tools/thermal_health.py`) show them |
| D34 | Pooled cycle model with a prior, honest grade, old cycles giving way to newer ones (owner decision 2026-11-11: never with time alone); v3 zones' decisions read `q`, v4 zones show it beside v4; coast area modelled; no thermal mass, no outdoor-dependent capacity | poc2: never worse than the typical-house default, converges on fine data, says `low` on whole-degree short cycles. Owner decisions, Issues #1512/#1516 |
| D35 | Evidence fades only on new evidence (owner decision 2026-11-11): carried evidence (pre-upgrade `rate_prior` count, held cycle fit) keeps a `keep` share halved per 20 new agreeing / 5 contradicting eligible cycles' worth of INFORMATION (Issue #1512 reopened, owner 2026-11-12: new cycles count `ΣI_new / I_ref`, an average cycle 1; k_passive: per 20 new passive observations); a cycle's weight halves when the newer cycles' information reaches 20 × its own (5 × when they contradict it), and the 60 heaviest are fitted, so a long accurate run outlives many short pulses; the cycle file keeps a cycle by that weight (past 90 days only a cycle under 0.01 leaves, the cap evicts the lowest weight); the cycle file keeps 90 days back from the mode's newest cycle; no time cutoff in confidence grading; stored observations (Issue #1523): weight halves per 20 newer observations of the same rate, each kind keeps its own share of the 200, none leaves with time; the primary solar phase offset gives way only to new AC-duty days (Issue #1524); v4 (Issue #1525): a mode its 30-day window cannot grade keeps the previous fit's gain, thermostat offsets and grade, displaced by the mode's new runs (share halved per 20, per 5 contradicting), window / fan / node rates with no data in the window keep their learned value, and the night-strategy replay's classes and evidence hours do not fall as nights leave its span | A house does not change over a quiet season: with no informative new cycle the rate the decisions use is still the best evidence. The time-based fade switched off the nat-vent thermal-floor veto and adaptive thermal behaviour of a whole-degree home after about 3-4 months and snapped a held rate back after a season (Issue #1512 verifier); a summer of vent-only fits evicted every closed-house fit and put k_passive back to its pre-upgrade value, then the first autumn fit replaced it in one step (Issue #1523) |

#### The cycle model and the old single-point estimator

`compute_k_active_single_point()` remains the arithmetic for `k_solar`. The heating/cooling rate no longer uses it: `cycle_model.fit_cycles` does (see [§Parameter Extraction](#parameter-extraction)). The old per-run gates (`THERMAL_HVAC_RUN_MIN_MINUTES`, `THERMAL_HVAC_MIN_SIGNAL_F`) are retired with the estimator.

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

*Historical (Issue #130, post-heat era): these counts measured the old estimator. The cycle model (Issues #1512/#1516) rejects with named codes instead; `thermal_replay --cycles` fits the stored cycles with the production model.*

After Phases A–D, `thermal_replay --hvac --days 60` committed 24/47 detected cycles (51%).
The 23 remaining rejections are data-quality limits of chart_log 1°F resolution:
- 16 signal=0 rejections: setpoint-maintenance cycles where `T_start = T_peak` at 1°F resolution
- 5 data artifacts: sub-2-minute elapsed times from timestamp coincidences or thermostat glitches
- 2 borderline: k_active estimates just above the 15.0 °F/hr sanity bound

The live coordinator samples the configured primary indoor reading at the exact HVAC state-change
moment. With a dedicated sensor that is its native resolution (the owner's 0.18°F
`sensor.santa_maria_hallway_current_temperature` when configured); on the default `climate_fallback`
source it is the thermostat's own `current_temperature`, whole-degree on many thermostats (Issue #1233
doc fix: the owner's hallway runs on the whole-degree climate entity). The exact-moment sampling is the
advantage over chart_log's 30-minute rows, not a finer sensor.

### Thermostat Swing Detection (Issue #102, redefined by Issue #1234)

**What it measures:** the thermostat's OWN hysteresis - how far past the setpoint the room is
allowed to drift before the equipment starts. It is one-sided and heat/cool are independent:

```
swing_heat_f = setpoint - indoor    at the real heat-on transition (hvac_action -> heating)
swing_cool_f = indoor - setpoint    at the real cool-on transition (hvac_action -> cooling)
```

(Through v0.8.33 it was `abs(T_end - T_start) / 2` - half the temperature rise per run, learned
only on the k-fit commit path. That is not the deadband: steady cycles read 0 and were dropped,
CA's own setpoint jumps supplied most samples, and falling-temperature and phantom sessions
polluted it; the owner's thermostat heats ~1.4 F below setpoint while 0.59 F was learned. It was
the original #102 design, not a regression.)

**Where it is sampled:** `coordinator._track_swing_edges()` on every thermostat state change,
independent of the k_active observation pipeline. A candidate opens at a real compressor-active
`hvac_action` edge (setpoint + the finest indoor reading) and is judged when the cycle ends. The
pure rules live in ONE module, `custom_components/climate_advisor/swing.py`
(`evaluate_swing_cycle`), shared by the coordinator and `tools/thermal_replay.py`.

**Thermostats that never report `hvac_action`:** the swing is sampled ONLY at real `hvac_action`
heating/cooling edges, so a thermostat that does not report `hvac_action` (or only ever reports
`fan`/`idle`) never produces a sample. Its swing stays at `THERMAL_SWING_DEFAULT_F` (1.5 F, shown as
"estimated") - the mode-fallback session cannot say where the equipment really started.

**Exclusions (no sample):**
- on-transition within `THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S` (120 s) after a setpoint **or HVAC
  mode** change - a jump by CA or the occupant, not hysteresis. Exactly the window length is sampled;
  there is no "setpoint unchanged for N minutes" rule;
- phantom sessions: an `hvac_mode` change while `hvac_action` positively reads `idle`/`off` has no
  real edge, so it opens no swing cycle and (coordinator `_async_thermostat_changed`) starts no HVAC
  observation either (also stops k_active pollution);
- `hvac_action` flapping: an on-transition within `THERMAL_SWING_REARM_WINDOW_S` (60 s) of the
  previous off-transition in the same direction (heating -> idle -> heating) is the same run, not a new
  drift to the trigger point;
- wrong-direction runs (heat whose temperature fell, cool whose temperature rose) - no `abs()`;
- non-positive samples (on at/inside the setpoint) and values outside
  `[THERMAL_SWING_MIN_F, THERMAL_SWING_MAX_F]`.

**Reading and quantisation:** the primary indoor reading (`IndoorTempReading.primary_value`, never the
sleep-window swap) is used. The reading granularity is inferred from the readings themselves
(`swing.infer_resolution_f` over the window's on/off readings, in the configured unit): whole F -> 1.0,
half F -> 0.5, whole C -> 1.8 F, half C -> 0.9 F, anything finer (e.g. an Ecobee's tenths, even on the
`climate_fallback` source) -> 0.0. With fewer than 3 distinct readings it falls back to what the source
implies: a configured sensor / input_number is fine (0.0), `climate_fallback` is one whole degree of the
configured unit (`THERMAL_SWING_WHOLE_DEGREE_RESOLUTION_F` = 1.0 F, x1.8 = 1.8 F on Celsius). A quantised
sample is `setpoint - k` with the true value within +/- resolution/2. The cache keeps
`swing_{heat,cool}_resolution_f`; it is metadata, the value is never shifted.

**Estimator:** the median of the last `THERMAL_SWING_ESTIMATOR_WINDOW` (15) accepted samples (robust to
the odd long-recovery outlier, no alpha to tune, follows a changed thermostat setting within ~8 cycles),
with two rules added by Issue #1477 (`swing.summarize_swing_samples`):

- **Finest readings decide** (`swing.finest_resolution_samples`): when the window holds samples of
  different `resolution_f`, only the finest tier is used. A whole-degree sample only says the true swing is
  within +/- 0.5 of `setpoint - k`; on Zone 1 five 1.0 samples from the thermostat's whole-degree display
  out-voted two 1.44 samples from its finer sensor (the plain median was 1.0). Nothing is dropped from the
  stored window, so the coarse samples decide again once no finer one is left in it (reversible; a
  weighting by resolution would make a 0.0 sample infinitely heavy, i.e. the same rule with more to tune).
  A "finer" tier whose own readings infer no finer than the rest (a configured sensor reporting whole
  degrees) does not count as finer; samples with no recorded resolution count as the coarsest.
- **Rounded up to the next half degree F** (owner decision 2026-11-08, `swing.round_swing_up_f`):
  `swing_{heat,cool}_f = ceil(median * 2) / 2` (1.44 -> 1.5, 1.0 -> 1.0, 1.01 -> 1.5), with a
  `THERMAL_SWING_ROUND_TOLERANCE_F` (1e-6 F) so float noise (1.0000001) does not round up. Heat and cool alike;
  the default (1.5 F) is already on the grid. A swing that is too small makes CA expect the heat or AC sooner
  than the thermostat starts. The unrounded median is kept as `swing_{heat,cool}_raw_f` (diagnostics only:
  `learning_db.py --swing`, the AI context; never a decision input). A Celsius install shows the stored value
  as a delta, x 5/9 with no offset (1.5 F -> 0.83 C, two decimals in Celsius so it is not printed as 0.8 C).
- **Which value each consumer reads** (owner decision 2026-11-08, PR #1482: "i want the bedroom loop on the measured
  swing, and the incident detection on the rounded up swing (to reduce the number of false incidents)"):
  - **measured** (`swing.measured_swing_f`: `swing_*_raw_f`, else `swing_*_f` when no samples are retained, else the
    1.5 F default) - the #1194 bedroom outer loop (`AutomationEngine._remote_swing_f`) and its emulation in the
    night strategy (`NightInputs.loop_swing_*_f`). It acts on comfort.
  - **rounded up** (`swing_*_f`) - every detector (comfort incidents, violation minutes, the #1050 stall alert, the
    incident payload's tolerance, the AI cross-validation) and every thermostat-model use (the direction rule and
    windows advice, the night strategy's and the plan list's discomfort beyond the swing).
  - both are shown: chart_data `thermal_model.swing_{heat,cool}` (rounded) and `swing_{heat,cool}_raw` (measured), the
    Thermal model card and the Analysis row ("1.5 °F (measured 1.44 °F)").
- **Corrected on load** (`swing.refresh_swing_cache`, called by `LearningEngine.load_state`): the stored
  value is recomputed from the retained samples with the current estimator (a value with no samples is just
  rounded up), so an update corrects it at once instead of at the next accepted cycle. Counts, audit and
  reject counters are untouched; idempotent; one INFO line per changed mode.

**Kept separately (never mixed into the swing):** `swing_{heat,cool}_off_point_f` - the stop point
(`setpoint - indoor` at heat-off, same sign convention; recorded only if the setpoint was unchanged
during the run). Coast overshoot (Issue #1233) is collected separately - see "Coast overshoot" below;
the observation record keeps `setpoint_f` and `peak_indoor_f` for a later consumer such as #1194.

**Storage:** `thermal_model_cache` - `swing_heat_f`/`swing_cool_f` (median of the finest samples, rounded up to
0.5 F), `swing_*_raw_f` (that median unrounded, Issue #1477),
`swing_*_samples` (the retained window), `swing_*_resolution_f`, `swing_*_off_point_f`,
`observation_count_swing_*` (accepted samples ever), `swing_definition_version`, and (Issue #1233)
`swing_*_audit`, `swing_*_reject_counts`, `coast_*_samples`, `coast_*_f`, `coast_*_past_setpoint_f`,
`observation_count_coast_*` (all additive: older code ignores them, so the change is downgrade-safe).

**Debugging a learned value (Issue #1233):** a homeowner can check the swing against their own
thermostat. Every judged cycle - accepted or rejected - is kept with the inputs that decided it:

- `thermal_model_cache.swing_{heat,cool}_audit` - the newest `THERMAL_SWING_AUDIT_CAP` (50) records per mode:
  `ts` (start), `mode`, `accepted`, `reason` (`ok` or one reject code), `value_f` (accepted value, or the
  would-be value of a rejected cycle when its inputs allow), `setpoint_f`, `setpoint_age_s` (seconds since the
  last setpoint/mode change at the start; `None` = unknown), `on_indoor_f`, `off_indoor_f`, `indoor_entity`
  (the primary sensor or the climate entity behind the reading), `resolution_f`, `reading_unit`, `trigger`
  (`hvac_action`; the only trigger since #1234) and `since_prev_off_s`. Built by
  `swing.build_swing_audit_record`, folded by `swing.record_swing_audit`.
- `swing_{heat,cool}_reject_counts` - cumulative per-reason counters (not bounded by the audit window).
- The reject codes are exactly what `swing.evaluate_swing_cycle` returns, in check order:
  `missing_input`, `setpoint_change`, `short_restart`, `wrong_direction`, `non_positive`, `out_of_bounds`
  (`swing.SWING_REJECT_REASONS`).
- One log line per cycle, with the zone (`zone=<entry id>`, else the climate entity): INFO
  `Swing sample accepted: zone=... mode=... value=... setpoint=... setpoint_age_s=... on=... off=... source=...
  resolution=... off_point=... median <before>-><after> (n=<retained>)`; DEBUG `Swing sample rejected: zone=...
  mode=... reason=... setpoint=... setpoint_age_s=... on=... off=... source=...`. The HVAC observation start
  (`Thermal HVAC observation started`, `_start_hvac_observation:`, `... not started`) and commit
  (`Thermal observation committed`, `Thermal obs 2-param PRIMARY commit`) lines carry `zone=` too.
- `get_thermal_model()` returns `swing_definition` (one line: how far past the setpoint the thermostat lets the
  room drift before starting the equipment) and `swing_debug` (`recent` records, `reject_counts`, window
  counts per mode); the chart payload (`thermal_model.swing_debug`, display units) and the investigator's
  LEARNING - THERMAL MODEL section carry the same.
- `python tools/learning_db.py --swing [--last N] [--entry-id ID]` prints the per-cycle table, the reject
  histogram, the median recomputed with `swing.summarize_swing_samples` (flagging a stored value that
  disagrees) and the coast section. `python tools/swing_audit.py` measures the real firing offsets from
  recorder history (`ha_logs.py --history`) or a saved chart_log, judging every cycle with the same
  `swing.evaluate_swing_cycle`, and prints them next to the learned value. Its coast replays the same history
  through the live coast driver (`swing.coast_thermostat_event` / `swing.coast_feed`), so it is the coast the
  integration records, not an approximation; setpoint-change detection and the setpoint read are shared too
  (`swing.setpoint_or_mode_changed`, `swing.pick_setpoint`, `SETPOINT_CHANGE_ATTRS`).

**Coast overshoot (Issue #1233) - kept apart from the swing.** After a THERMOSTAT-CHOSEN stop (a real
heating/cooling -> idle `hvac_action` edge with no setpoint/mode change in the same event, nor within
`THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S` (120 s) before it - many thermostats report the command and the stop in
two state changes; `swing.coast_stop_was_commanded`, the mirror of the swing's on-edge rule) the coordinator
opens a coast window with the off reading as baseline (`swing.coast_thermostat_event`, called by
`_track_swing_edges`; the one driver the coordinator and `tools/swing_audit.py` share). Every later indoor update - a
thermostat state change, or a tick of the dedicated indoor sensor - folds into it (`swing.coast_feed`; heat keeps
the running peak, cool the running trough, `swing.coast_extreme`). The window ends at the first of: the
equipment starting again (`next_on`), a setpoint or HVAC-mode change (`setpoint_change`), or
`THERMAL_COAST_MAX_S` (1800 s, `timeout`). The reading that accompanies the ending event is not coast.
`swing.build_coast_sample` judges it: no post-off reading -> no sample (`no_post_readings`); a fall-back
after a heat stop is a coast of 0.0; more than `THERMAL_COAST_MAX_F` (5 F) is weather or another actor
(`out_of_bounds`). Stored in `coast_{heat,cool}_samples` (newest `THERMAL_COAST_ESTIMATOR_WINDOW` = 15),
`coast_{heat,cool}_f` (median `coast_f` = distance beyond the off reading),
`coast_{heat,cool}_past_setpoint_f` and `observation_count_coast_{heat,cool}`. A window cut short by the next
run or a setpoint change is a lower bound (each sample records `ended_by`). The swing and the off-point are
never touched by it. Open windows are not persisted (a restart drops them). `tools/thermal_replay.py` does
NOT backfill coast: chart_log keeps `indoor_primary` only on `hvac_action_change` rows, and the 30-minute
poll rows between them carry no primary reading, so the peak after a stop cannot be reconstructed (it would
learn a falsely small coast).

**Migration:** the learning DB has no schema version; `swing_definition_version` in the cache is the
marker. A cache without the current value (everything learned before v0.8.34) has its swing values,
counts and samples nulled on load (`swing.reset_swing_cache`), falling back to the default until
re-learned.

**Default:** `THERMAL_SWING_DEFAULT_F = 1.5 F` - displayed as "estimated" until a sample exists.

**Confidence tiers (per mode, by `observation_count_swing_*`):**
| Count | Grade |
|---|---|
| 0 | "none" |
| 1-2 | "low" |
| 3-9 | "medium" |
| 10+ | "high" |

**Backfill:** `python tools/thermal_replay.py --hvac --days 60 --write` pairs chart_log
`hvac_action_change` rows and judges them with the same `evaluate_swing_cycle`. It reads ONLY the rows'
`indoor_primary` (the same always-primary reading live uses; `indoor` is the comfort-facing value and
carries the sleep-window bedroom swap). Rows without it - every chart_log written before v0.8.34 - give no
sample; live re-learns after the migration, so an empty backfill on old data is the safe outcome. The
same flap guard applies; the resolution is inferred from the readings (unit unknown, so Fahrenheit then
Celsius grids; 0.0 when too few distinct readings). chart_log has no
exact setpoint-change time and no HVAC-mode field, so replay treats a start as a jump unless its
setpoint is confirmed by every earlier row back to one at least `THERMAL_SWING_SETPOINT_CHANGE_WINDOW_S`
(120 s) older - stricter than live, never looser (just the previous row is not enough: the 30-min
poll row and the `override` row are written seconds after a change and already carry the new
setpoint). `rebuild_model_cache()` never recomputes swing from observations (a legacy `swing_f` cannot
re-poison it) and carries the existing swing fields over - including the Issue #1233 audit trail
(`swing_*_audit`, `swing_*_reject_counts`) and the coast keys, which are not rebuilt from observations either.

**Decisions:** D27 (setpoint-agnostic formula) and D28 (min(active_samples) cool trough) are
superseded by Issue #1234; D29 (swing in `--hvac` mode) and D30 (rows hidden until first HVAC obs)
stand.

### `record_thermal_observation(obs: dict) -> None`

Called by `learning._commit_event_from_dict()` after parameter extraction. Appends the observation to the history (each kind keeps its own share of `THERMAL_OBS_CAP`, `THERMAL_OBS_KIND_CAPS`; only its own kind's oldest leaves, never by date: Issue #1523) and calls `_update_thermal_model_cache()`. State is saved to disk immediately after each commit — HA restarts mid-day do not lose accumulated observations.

### Predicted Indoor Temperature — Band Schedule Alignment (Issue #119)

`_build_predicted_indoor_future()` in `coordinator.py` accepts a `classification` parameter alongside `thermal_model`. When both are provided:

- The target band schedule is computed once via `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now, thermal_model, classification)` (no `setback_modifier` since Issue #1076) before iterating forecast hours (pre-computed, not per-hour — Issue #119 B3 fix).
- Sleep temperatures in the band are derived from `compute_bedtime_setback(config, thermal_model, classification)`, used by the chart band and the briefing. The automation engine arms the raw configured sleep temperatures instead (`select_comfort_band()`), and since Issue #1366 an explicit sleep temperature is returned unclamped, so the two agree even when it is set beyond the away setback (this closes the #1089 gap).
- Occupancy mode is propagated: away today → setback setpoints for today's hours only; vacation → deep setback for all forecast days.

When `thermal_model` or `classification` is absent, the band falls back to static sleep-temp defaults and the ramp interpolation path is used for the indoor prediction curve.

### `get_thermal_model() -> dict`

Returns the current accumulated thermal model from `thermal_model_cache`.

**Output dict structure (v3):**

```python
{
    # Core physics parameters
    "k_passive": float | None,  # envelope decay rate (hr⁻¹, negative)
    "k_active_heat": float | None,  # HVAC heating contribution (°F/hr, positive)
    "k_active_cool": float | None,  # HVAC cooling contribution (°F/hr, negative)
    "k_vent_window": float | None,  # window-open, fan/WHF-off ventilation decay rate (hr⁻¹, negative)
    "k_vent_fan": float | None,  # window-open, fan/WHF-on ventilation decay rate (hr⁻¹, negative);
    # learned/displayed only — not wired into forecast per-hour selection
    # (Issue #587 scope boundary)
    "k_solar": float | None,  # solar gain coefficient (°F/hr per unit solar factor)
    # Confidence
    "confidence": str,  # HVAC confidence: "none"|"low"|"medium"|"high" (heat+cool obs count)
    "confidence_k_passive": str,  # passive confidence: "none"|"low"|"medium"|"high" (passive+heat+cool
    # obs count only — vent/solar counts excluded, Issue #587)
    "confidence_k_hvac": str,  # same as "confidence" — explicit alias
    # Observation counts
    "observation_count_heat": int,
    "observation_count_cool": int,
    "observation_count_total": int,
    "observation_count_passive": int,
    "observation_count_vent_window": int,
    "observation_count_vent_fan": int,
    "observation_count_solar": int,
    # Retired (Issue #587): "k_vent" and "observation_count_fan_only" are no longer
    # present in this dict at all — confirmed absent, not lingering as None.
    # Legacy compatibility fields
    "heating_rate_f_per_hour": float | None,  # = abs(k_active_heat), rounded to 2dp
    "cooling_rate_f_per_hour": float | None,  # = abs(k_active_cool), rounded to 2dp
    # Diagnostics
    "avg_r_squared_passive": float | None,
    "last_observation_date": str | None,
}
```

`confidence` (HVAC) is graded by the evidence the heat/cool rates carry (`thermal_confidence.grade_hvac_confidence`, Issue #1512; owner decision 2026-11-11: evidence gives way only to newer evidence, never to time alone). One rule for heat and cool, v3 and v4 zones. Per mode the cycle model speaks through its calibrated grade: the current fit's grade when it has eligible cycles; a held fit's grade while it stands in (no eligible cycle now, e.g. after a sensor swap) or while it is the prior of a fit that agrees with it (a few new cycles against a strong held prior give a small `er_q`; that is not low confidence). So a whole-degree sensor's own cycles never reach `high`. Beside it, the count tier of the legacy evidence `rate_prior["k_active_<mode>"]["n"] × keep` summed over heat and cool (the raw `observation_count_<mode>` for a mode with no prior entry): `"none"` (< 5, `MIN_THERMAL_OBSERVATIONS`), `"low"` (5–9), `"medium"` (10–19, `THERMAL_HVAC_CONF_MEDIUM`), `"high"` (20+, `THERMAL_HVAC_CONF_HIGH`); a mode's legacy evidence counts while its current fit agrees with the prior it was weighed against (`|q − prior_q| ≤ 2·prior_sd_q`) or it has no eligible cycle, and data that contradicts the old value drops it (that mode's fit grade then speaks). The result is the better of the two. There is no time cutoff anywhere: an applied fit keeps grading until newer evidence replaces it. So an upgrade never drops a tier, and a home whose sensor cannot measure the rate (the verified install: whole-degree short cycles, 65 heat + 286 cool observations, no eligible cycle) stays "high" indefinitely; a fine-sensor home idle for a season stays where it was. `get_thermal_model()`'s `observation_count_heat/cool` report the evidence carried (`hvac_evidence_count`: legacy `n × keep` plus the fit's `ne_w`, plus a held fit's `ne_w × keep` while it stands in or is the prior), rounded, live: not cumulative counters; they fall as new cycles displace old evidence and stay flat through a quiet season.

`confidence_k_passive` is graded by the evidence that still backs `k_passive`: `observation_count_passive` (passive fits, and the retired duty-balance fit's old observations) plus the pre-upgrade evidence `rate_prior["k_passive"]["n"] × keep` when the cache has a prior (heat/cool cycles never write `k_passive`, so new ones add nothing), or passive + heat + cool for a cache with no prior yet (pre-upgrade shape). `keep` halves for every `THERMAL_EVIDENCE_HALF_N` (20) new passive observations and never with time alone (owner decision 2026-11-11), so confidence neither cliffs on upgrade nor fades over a season without a passive window. Tiers: `"none"` (< 5), `"low"` (5–14), `"medium"` (15–29), `"high"` (30+) (`THERMAL_PASSIVE_CONF_*`). Physics prediction activates when either confidence is > `"none"` and a real closed-house `k_passive` < 0 exists (the gate bridge was removed in Issue #1512, so `k_vent_window` no longer stands in for it) — enabling prediction on homes with zero HVAC cycles recorded (passive-only households).

**Single implementation (Issue #1277):** the grades and the physics-eligibility rule live in the pure module `thermal_confidence.py` (no Home Assistant import), used by `learning.py`, `coordinator._build_predicted_indoor_future` and, loaded by path, by `tools/engine_status.py`, `tools/learning_db.py --model` and `tools/thermal_replay.py`. They are always computed live from the counts; the persisted `thermal_model_cache` no longer stores `confidence_k_passive` / `confidence_k_hvac` (they were seeded `"none"` and never refreshed, so the tools reported physics ineligible while the engine used it; `load_state()` drops them).

The legacy `heating_rate_f_per_hour` and `cooling_rate_f_per_hour` fields are kept for backward compatibility with `compute_bedtime_setback()` and `_compute_ramp_hours()`.

### `LearningState` Fields

| Field | Type | Description |
|---|---|---|
| `thermal_model_cache` | `dict \| None` | EWMA-accumulated model parameters for all six observation types |

**No startup recovery of in-flight observations (Issue #1437):** A heating or cooling run in progress during an HA restart or zone reload teaches the thermal model only from the part after the restart, by design (Issue #1437). In-flight observations (`coordinator._pending_observations`) live in memory only and are never persisted; after the restart the Issue #96 late start (`_initialize_hvac_session_from_current_state`) opens a fresh observation from startup. The pre-restart part is never fitted because the thermostat keeps running while HA is down and Climate Advisor cannot see whether the run stopped and restarted in the gap, so a curve fitted across it would be invalid: a shorter clean observation beats a longer contaminated one. Non-HVAC decay observations restart on the next poll if their conditions still hold. The retired `LearningState.pending_observations` field (written only before Issue #133) and the v2 `pending_thermal_event` field are dropped silently on load if an old learning file still holds them, and are not written again.

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
    "high_bias": float,  # mean forecast high error in °F (positive = forecast runs warm)
    "low_bias": float,  # mean forecast low error in °F (positive = forecast runs warm)
    "confidence": str,  # "none" | "low" | "medium" | "high"
    "sample_count": int,  # number of days used to compute the bias
}
```

**Confidence levels:** same thresholds as `get_thermal_model()` (3/10/20 observations).

### Suggestion Keys for Thermal Learning

| Suggestion Key | Detection | Suggestion Text |
|---|---|---|
| `thermal_model_ready` | `get_thermal_model()` confidence transitions from `"none"` to `"low"` or better for both heat and cool | "Your home's thermal profile is ready. Bedtime setback depth and pre-heat timing are now tuned to your home's actual heating performance." |
| `forecast_bias_significant` | `get_weather_bias()` high or low bias magnitude exceeds 3°F at `"medium"` or higher confidence (threshold is internal °F) | "Your weather service appears to run [warm/cool] by about X on average. Climate Advisor has adjusted its timing calculations to compensate." -- X is `format_temp_delta(magnitude, temp_unit, 1)`, so a Celsius home sees °C (Issue #1050) |

### Sensor Attributes (Compliance Sensor)

The following attributes are added to `sensor.climate_advisor_comfort_score` when the thermal model is available:

| Attribute | Type | Description |
|---|---|---|
| `thermal_heating_rate` | `float \| None` | `k_active_heat` in user's configured unit per hour (`None` if no data) |
| `thermal_cooling_rate` | `float \| None` | `abs(k_active_cool)` in user's configured unit per hour (`None` if no data) |
| `thermal_confidence` | `str` | Confidence level: `"none"`, `"low"`, `"medium"`, or `"high"` |
| `thermal_observation_count` | `int` | Evidence behind the heating + cooling rates (`observation_count_heat` + `_cool` of `get_thermal_model()`, Issue #1512): not a cumulative counter; older evidence gives way as new cycles replace it, never with time alone |
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
    "status": "active",  # or "collecting_data"
    "days_recorded": 28,
    "window_compliance": 0.35,  # 35% of recommended days
    "avg_daily_hvac_runtime_minutes": 145.5,
    "comfort_score": 0.92,  # 92% of time in comfort range
    "total_manual_overrides": 8,
    "pending_suggestions": 2,
}
```

### Metric Definitions

#### Comfort Violations (`comfort_violations_minutes`)
**Unit:** minutes
**What it means:** Time during the day when the indoor temperature was outside the **target
range in effect at that moment** — the same time-varying target band the chart draws
(awake: `comfort_heat`–`comfort_cool`, widened by aggressive savings when on; sleep: `sleep_heat`–`sleep_cool`
with the wake/sleep ramps; away/vacation: the setback range). Issue #1107: before 0.8.1 it was always
the static `comfort_heat`–`comfort_cool` range, so CA's own planned setbacks counted as discomfort.
Issue #1258: a minute is also not counted while indoor is no more than the thermostat's own learned swing past the band edge and the thermostat is in the matching mode (`coordinator._is_tolerated_comfort_deviation()`; 1.5 °F default, 0.5 °F floor, 2.5 °F cap; Off or the opposite mode count as before).
The band is read via `coordinator._comfort_score_band_now()` → `_target_band_lower_upper_now()`
(the per-cycle cached band, Issue #514). **During a time-of-use bank (Issue #1407, decision C22, reversible)** a
minute counts only when indoor is outside BOTH the hold (what the thermostat is told, `tou_bank_band()`: pre-cool
[min(floor, target), target], pre-heat [target, max(ceiling, target)]) and the schedule band (home, sleep, Away or
Vacation, `AutomationEngine._resolved_comfort_band()`), each past the thermostat's swing
(`coordinator._comfort_score_bank_bands()`, read from the control source `_tou_bank_in_force()`; a deferred bank
keeps the cached band). A pre-cool pulling the house from 74 °F toward 68 °F is inside the 68–76 °F comfort range,
so the person is comfortable and those minutes do not count or feed the `comfort_violations` suggestion; a house
outside both is a real miss. The cached band draws a bank in the #786 shape (target on the opposite edge: an Away
pre-heat to 63 °F reads 60–63 °F), which counted a house at 70 °F, inside its Away band, as uncomfortable. Score
only: no decision reads it, and its one caller is pinned (`tests/test_comfort_score_tou_bank_1407.py`). While no band has been resolved yet (`(None, None)`: before
the first cycle's band, or a weather source with no hourly forecast) it falls back to the static
`comfort_heat`–`comfort_cool` range. The nat-vent tolerance gate (Issue #411) is judged against the
same resolved range. History is not rewritten; the 14-day window turns over within 14 days.
**How it accumulates:** The coordinator checks indoor temperature on every data update.
Each check adds the **actual elapsed time** since the previous check (capped at 30 minutes)
when the temperature is outside the target range in effect. This prevents double-counting when the
coordinator is refreshed more frequently than the 30-minute scheduled interval (e.g., after
door/window events or automation revisits).
**Maximum per day:** 1440 minutes (24 hours). Values above 1440 in historical records
indicate data recorded before v0.2.x (fixed-30-min bug) and should be disregarded.

#### Comfort Score (`comfort_score`)
**Formula:** `1 − (sum of daily violation_minutes / (days_recorded × 1440))`
**Range:** 0.0 (always outside the target range in effect) to 1.0 (always within it), reported as a percentage
**Example:** 3 days, total 2160 violation minutes → `1 − (2160 / 4320)` = 0.50 = 50%
**Sensor:** `sensor.climate_advisor_comfort_score` reports this as a percentage (0–100%)
**Trigger:** More than 5 days with over 30 violation minutes triggers the `comfort_violations`
suggestion, which reduces setback aggressiveness and starts morning pre-heat 15 minutes earlier.
