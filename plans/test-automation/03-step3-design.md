# Step 3 design: one list of household promises (Issue #1128)

*Architect B, 2026-10-29. Design only; nothing is built and no production file was touched. Branch `feature/1125-test-automation` (origin/main v0.8.1).*

**Crux.** Write the household promises once, run them both inside production (log-only first, alert later) and after every step of every test run, without the promises turning into a second copy of the algorithms they guard.

**Sources read.** `01-assessment-and-options.md` (Step 3 + promises table), issue #1128, `docs/invariant-watchdog-brief.md`, `docs/comfort-edge-selection-spec.md`, `docs/grace-periods-spec.md`, `docs/sim-harness-spec.md`, `docs/activity-report-table.md`, `docs/06-LOGGING-GUIDELINES.md`; code: `invariant_watchdog.py`, `coordinator.py` (`_run_invariant_watchdog`, `_emit_event`, `_async_thermal_sample_tick`, `_async_command_fan_entity`, `_target_band_lower_upper_now`), `automation.py` (`_call_service_with_context`, `_call_climate`, `_record_commanded_mode`, `_append_mode_change`, `_resolved_comfort_band`, `select_comfort_band`, `awake_comfort_edges`, `_deactivate_fan`, `_recent_duplicate`), `control_quality.py`, `chart_log.py`, `tools/sim_harness/{run_production,fake_hass,build_engine,outcomes}.py`, `tools/simulate.py`, `tests/test_invariant_watchdog.py`, `tests/test_production_harness.py`.

---

## 0. Read this first

### 0.1 The design in seven lines

1. **One immutable input**, `PromiseInput` = latest `Reading` + a bounded 3 h window of `Reading`s, `Command`s and `Mark`s. It is built by one pure class, `PromiseJournal` (new file `promise_journal.py`, no Home Assistant imports), fed by *push* calls. Production feeds it from four places the coordinator already has (thermostat listener, the existing 5-minute tick, `_emit_event`, the engine's single command choke point). Tests feed it from the same *kinds* of source (state timeline, service-call log, CA event stream) through a `PromiseSource` adapter, so one pure check runs in both worlds.
2. **Eight pure check functions** in `invariant_watchdog.py`: `check(inp, cfg) -> list[Hit]`. No I/O, no clock reads, no engine objects. Each promise uses its own numbers (module `PromiseNumbers`), proven by a test to be looser than production's legal configurations.
3. **Ground truth first.** Readings (`hvac_action`, mode, indoor, fan physical state, contacts) and the *actual* service calls decide a violation. CA's own words (event stream) are used only where the promise is *about* CA's word (P4 override episodes) or for exclusions (occupancy, restart).
4. **Severity is a constant per promise** (`TEST_SEVERITY`, `HOME_LEVEL` in `invariant_watchdog.py`), not a user setting. Promotion log to alert = edit one constant after two quiet weeks, evidenced by persisted `promise_stats`.
5. **Test wiring** = one `PromiseProbe` called after every step by a new `Session` interface that both the FakeHass runner and Architect A's real-HA runner implement. Hard violations fail the test with a replay pointer; warnings go to a ratcheted report. `outcomes.py` is untouched.
6. **Random days** = a Hypothesis-drawn *plan* executed by a closed-loop driver (toy house responding to the real thermostat state), which writes out an ordinary scenario file (same format as goldens) as the replay artifact.
7. **Trial gate** = export pre-fix tree `d7209f4` (parent of the #1073 fix `a80d773`), overlay only the new promise and generator files, require the generator to find a P1 violation without being pointed at it, and find none on current code.

### 0.2 Findings that change the plan's inputs (read before the numbers)

| # | Finding | Evidence | Consequence in this design |
|---|---|---|---|
| K1 | **The confirmed "shallow = 3 F" is stricter than production's default, not looser.** Production holds a switch only while the house is at most the tolerance past the edge; the default tolerance is 2 F on warm, mild and cool days (1 F is the lowest a user may set). A correct furnace start at 2.5 F under the floor, 30 min after cooling, would violate a 3 F promise. | `comfort-edge-selection-spec.md` R1/R2 and the variable table; `const.py` `DEFAULT_COMFORT_DEADBAND_*` = 5/2/2/2/5, clamps 1-5 / 2-8 | P1 uses `S = 2.0` minus a 0.5 F reading slack (flag only if dip < 1.5 F), and `S_eff = min(2.0, smallest configured tolerance)`. **Starting number changes 3 -> 2 (needs the user's nod, section 13.1).** |
| K2 | **The day's natural side is exempt in production** (tolerance 0: heat on a cool/cold day, cool on a hot/warm day is never held). A promise worded only "after cooling, no heat" would fire on correct behavior every cold evening after a sunny afternoon. | R2 / D2=X in the same spec | Each P1 clause is gated by the day's **forecast high**: the heat clause applies only when `forecast_high >= threshold_mild + 3 F`, the cool clause only when `forecast_high < threshold_warm - 3 F` (thresholds read from the occupant's configuration as an applicability gate, the same pattern as the K1 tolerance gate). That is coarser than production's five day types, ignores classification hysteresis, and reads only a forecast number, never the classification object. (An outdoor-sensor exemption was considered and rejected: a warm day's dawn is cold outdoors but still cool-native.) |
| K3 | **The confirmed P4 exception list is wrong for thermostat overrides.** Bedtime and wake-up clear an *active fan* override (#1027, kept on purpose) but *skip* a thermostat override entirely (#204, #1119). The user's "(bedtime, wake-up, end-grace button)" therefore excuses fan overrides only. The docs list more legitimate endings (occupancy away/vacation, a newer occupant action, AC/WHF hard mutex, restart clean slate). | `grace-periods-spec.md` "What Clears a Manual Override" callsites 1-8 | P4 exception table is split by target (section 4.4). Needs the user's confirmation (13.2). |
| K4 | **Existing dedup would thrash with more than one promise.** `_run_invariant_watchdog` dedups with one key `"invariant_violation"` and signature `(name,)`; alternating violations of two promises would each look new. | `coordinator.py` `_run_invariant_watchdog`, `automation._recent_duplicate` (one signature slot per key) | Per-promise key `invariant_violation:<key>`; episodic promises dedup on a stable `Hit.key`, state promises keep the 300 s window (section 5.3). |
| K5 | **FakeHass has no `hvac_action` and no house.** The thermostat is perfectly obedient and indoor temperature is whatever the scenario injects. P1, P2 and P7 are about equipment running and the house responding. | `sim-harness-spec.md` "The fake thermostat never sets `hvac_action`" | Random days need a **closed-loop world** (toy house) driven *while* the engine runs; the realised injections are written out as the replay file (section 10). P7 is only meaningful in closed-loop runs. |
| K6 | **Invariant 1 of the watchdog brief ("no check reads internal flags") cannot hold for P4 and for exclusions.** P4 is a promise about CA keeping its own word. | `invariant-watchdog-brief.md` Invariants 1-2 | Amend the brief: P1, P2, P3, P5, P6, P7 decide on ground truth; P4 decides on ground truth *effect* (a command) against CA's declared episode; P8 is test-only. Landing needs the human sign-off the brief already requires (section 13.1). |
| K7 | **The watchdog runs once per 30-minute refresh** (plus on-demand refreshes), too coarse for a 45-minute rule. | `update_interval=timedelta(minutes=30)`; only call site is inside `_async_update_data_impl` | Evaluate on the existing **5-minute thermal tick** as well (one added line, section 6). |
| K8 | **The #1073 regression window is exactly `d7209f4..a80d773`.** `d7209f4` is the merge of PR #1072 (#1066 nearest-edge arming, no settling guard); `a80d773` is the fix. | `git rev-parse a80d773^` = `d7209f4d...`; `a80d773` commit message "Regression from #1066/#1072" | Trial gate target (section 11). |

### 0.3 Mirror items (called out explicitly, per the symmetry rule)

- **M1, in scope.** The settling guard is symmetric (R2: "identical for heat-after-cool and cool-after-heat"), but the confirmed P1 wording is one-directional. P1 is specified in both directions (`settling_flip` has a heat clause and a cool clause) so a future fix of one side cannot silently leave the other unwatched.
- **M2, proposed addition (not in the confirmed 8).** P5 is "no fan *start* while indoor is unreadable". Its mirror is "a fan *keeps running* blind". #1037's production guard is a 30-minute auto-stop with no promise behind it. Proposed as clause P5b (warning, log-only, 35 min). Landing needs the user's yes (13.3). #1043 (frozen-but-numeric indoor) is a different shape and is not covered.
- **M3, test-side only.** `_async_command_fan_entity` (coordinator) is a second command path that does not carry a `Context`. A static guard test (section 12, I8) forces every `climate`/`fan`/`switch` service call through the two known choke points.

---

## 1. Architecture

```
 PRODUCTION (ClimateAdvisorCoordinator)               TESTS (runner Session)
 ----------------------------------------             ----------------------------------------
 _async_thermostat_changed  -> Reading                FakeHass state timeline  -> Reading
 _async_thermal_sample_tick -> Reading (5-min)        FakeHass.action_log      -> Command
 engine _call_service_with_context -> Command         engine/coordinator event stream -> Mark
 coordinator._async_command_fan_entity -> Command     scenario "external" events -> occupant mark
 coordinator._emit_event    -> Mark                   real-HA runner: state_changed + call_service bus
        |  journal.record_*()                                |  PromiseProbe.step() -> source.poll()
        v                                                    v
                    PromiseJournal   (pure, bounded, in memory)
                              |  .window(now) -> PromiseInput (frozen)
                              v
            invariant_watchdog.run_promise_checks(inp, cfg, scope)   pure, no I/O
                              |  -> list[Hit]
              +---------------+-------------------+
       production sink                         test sink
   WARNING log / event / (alert: ERROR,        hard -> assertion failure + replay pointer
   Status line, notification), promise_stats   warning -> report, ratcheted vs baseline
```

Territory: `promise_journal.py` owns the *shape of the evidence*; `invariant_watchdog.py` owns *what counts as a broken promise*; `coordinator.py` owns *what to do about it at home*; `tools/promise_probe/` owns *what to do about it in tests*. None of them issues a service call. This keeps the module-boundary rules in CLAUDE.md and the brief's "pure leaf" invariant.

---

## 2. Input model

### 2.1 Records (all frozen, slotted dataclasses; every temperature is internal degrees F)

```python
@dataclass(frozen=True, slots=True)
class Reading:                       # one observation of reality
    ts: datetime                     # aware
    hvac_mode: str | None            # thermostat entity state; None = unavailable/unknown/missing
    hvac_action: str | None          # attribute as reported; None = thermostat does not report it
    indoor_f: float | None           # via the existing indoor resolver; None = unreadable
    outdoor_f: float | None
    forecast_high_f: float | None    # today's forecast high (the number the day classification carries); None = no classification yet
    setpoint_f: float | None         # single-setpoint "temperature" attribute, converted to F
    fan_on: bool | None              # WHF physical state (ground truth read); None = unknown / not configured
    contacts_open: bool | None       # any monitored door/window physically open; None = none configured

@dataclass(frozen=True, slots=True)
class Command:                       # one climate/fan command CA actually sent to the bus
    ts: datetime
    target: str                      # "thermostat" | "fan"   (no entity ids, ever)
    service: str                     # set_hvac_mode | set_temperature | set_fan_mode | turn_on | turn_off
    mode: str | None                 # requested hvac_mode (set_hvac_mode, or bundled in set_temperature)
    setpoint_f: float | None
    fan_on: bool | None              # for target "fan": turn_on -> True, turn_off -> False
    origin: str                      # "ca" | "external"  (tests only ever see "external" from scripted occupants)
    automation_enabled: bool         # the user's switch (coordinator.automation_enabled) at send time
    indoor_f: float | None           # the indoor reading CA had when it sent (hook-stamped); None = blind
    cause: str | None                # engine _mode_change_reason when known; used for log text only, never to excuse

@dataclass(frozen=True, slots=True)
class Mark:                          # CA's declared lifecycle word, table-mapped from its event stream
    ts: datetime
    kind: str                        # see EVENT_MARK_MAP below
    attrs: tuple[tuple[str, str | float | bool | None], ...]

@dataclass(frozen=True, slots=True)
class PromiseInput:
    now: datetime
    latest: Reading | None
    readings: tuple[Reading, ...]    # oldest -> newest, includes latest
    commands: tuple[Command, ...]
    marks: tuple[Mark, ...]
    episodes: tuple[Episode, ...]    # open override/grace episodes + those ended inside the window (derived, see 2.4)
```

`EVENT_MARK_MAP` is a literal table in `promise_journal.py` mapping CA event types to `Mark.kind`, with the payload keys read. Keys read, all verified in code or the activity catalog: `override_detected{detected_mode,source}`, `override_confirmed{mode,confirm_delay_seconds}`, `override_self_resolved{detected_mode,current_mode}`, `grace_started{source,duration_seconds,trigger}`, `grace_cancelled{reason,trigger,grace_source,remaining_minutes,protects_override,grace_end_time}`, `grace_expired{source,re_paused}`, `override_cleared{was_mode,old_setpoint_f,reason}`, `fan_override_cleared{reason,source,remote_timer_hours}`, `fan_manual_override{fan_before,fan_after,remote_timer_hours}`, `override_adopted`, `stuck_grace_recovered{reason}`, `occupancy_*`, `nat_vent_*`, `system_restarted`, `startup_coalesced`. A test asserts every mapped type exists in `ai_skills_context.EVENT_RENDERERS` so a renamed event fails loudly instead of silently blinding P4. *UNVERIFIED until I1:* exact payload of `occupancy_*` and `nat_vent_*` rows.

### 2.2 Sources, per world

| Record | Production source (touch point) | FakeHass runner (engine-only or coordinator mode) | Real-HA runner (Architect A) |
|---|---|---|---|
| `Reading` | `_async_thermostat_changed` (first lines, before any early return) and `_async_thermal_sample_tick` (5-minute heartbeat); built by a new pure-ish coordinator method `_promise_reading_now()` using `read_state_temp_f` and the existing indoor resolver | `_FakeStates` gets an additive `add_state_observer(cb)`; the adapter builds a `Reading` on every thermostat/fan/contact state write, plus one per probe tick. Old trees without the observer fall back to polling `states.get` at step boundaries | `state_changed` listener on `hass.bus` for the three entity roles, plus one per tick |
| `Command` | engine: new `_observe_command()` called in `_call_service_with_context` immediately before `hass.services.async_call` (below the observe-only gate, so it records only what is really sent); coordinator: one line in `_async_command_fan_entity` | `FakeHass.action_log` (already holds `domain, service, data, ts, context` for every call). `automation_enabled` is stamped from the scenario's `automation_toggle` events | `EVENT_CALL_SERVICE` on `hass.bus`, filtered to the three roles; `origin` from `Context` (CA commands carry the recorded CA contexts, scripted occupant calls carry a marked context) |
| `Mark` | `ClimateAdvisorCoordinator._emit_event` (one line after the append) | the engine `_emit_event_callback` list (`event_log`) or, in coordinator mode, `coordinator._event_log` | `coordinator._event_log`, read incrementally |
| occupant action | **derived inside the journal, not reported by anyone** (section 3.3) | same derivation | same derivation |

The derivation of an occupant action from `Reading`s and `Command`s alone means P1, P4 and P6 excuse an occupant without trusting CA's override detection, and the identical function runs in production and tests.

`Reading.forecast_high_f` comes from `coordinator._current_classification.today_high` in production and from `engine._current_classification.today_high` in the runner. Hand-written goldens that omit `today_high` get the runner default of 75.0 (`_build_classification_from_event`), which keeps the heat clause active and the cool clause silent; the generator emits real `classify_day()` output, so its values are meaningful.

### 2.3 Retention, caps, restart

| Store | Bound | Rule |
|---|---|---|
| readings | age 3 h, `maxlen` 400 | A reading is stored when a discrete field changed (mode, action, fan, contacts, indoor readable<->unreadable), or indoor moved at least 0.5 F, or at least 5 min passed since the last stored one. Otherwise dropped. |
| commands | age 3 h, `maxlen` 128 | |
| marks | age 3 h, `maxlen` 128 | |
| episodes | at most 8 open, plus ended ones inside the window | An RF-timer grace can last 8-12 h, so an *open* episode is never aged out by the 3 h window; it is kept until its end mark. |
| `seen_keys` (dedup) | LRU 256 | Hit keys already reported. |
| `promise_stats` (persisted) | 8 keys x 3 fields | `{key: {hits, first_ts, last_ts}}`, saved with the existing coordinator state, type-validated on load (`isinstance(dict)`), unknown keys dropped. |

CLAUDE.md "lists that grow MUST have a cap": all six are capped. **Restart:** the journal is deliberately *not* persisted. After a restart it is empty, every history-needing check sees "no data" and returns no violation (same rule as `check_ac_whf_mutex` for unknown inputs), so the only effect is a blind window of at most 3 h and never a false alarm. This matches the documented override/grace clean slate (#282). The test runner treats `simulate_restart` the same way (journal reset, `restart` mark). A future seed from the chart log (`hvac` per 30 min is enough for "cooling in the last 2 h") is possible through the same `record_reading` API but is **out of scope** for this step; Step 2's real-HA lifecycle tests own restart behavior.

### 2.4 Episodes (for P4)

`Episode(id, target, kind, start_ts, value, grace_end_ts, end_ts, end_reason)` is derived from marks by one pure function in `promise_journal.py`:

| Opens on | `target` / `kind` / `value` | Closes on (`end_reason`) |
|---|---|---|
| `override_detected` | thermostat / `override` / `detected_mode` (+ setpoint from the next reading) | `override_self_resolved`, `override_cleared{reason}`, `grace_expired`, `grace_cancelled{reason}`, `override_adopted` |
| `fan_manual_override` | fan / `override` / `fan_after` | `fan_override_cleared{reason}`, `override_cleared{reason}`, `grace_expired`, `grace_cancelled{reason}` |
| `grace_started{trigger:"fan_off"}` | fan / `fan_off` / off | `grace_expired`, `grace_cancelled{reason}` |
| `grace_started{...}` while an episode is open | sets `grace_end_ts = ts + duration_seconds` | |

An `override` episode with no grace timer (manual grace 0, owner decision #1119) stays open until an end mark.

---

## 3. Shared definitions used by several promises

### 3.1 `PromiseNumbers` (frozen, one place, each with its looseness rationale)

| Name | Value | Used by | Looser than production because |
|---|---|---|---|
| `P1_WINDOW_MIN` | 120 | P1 | Equals the default recency window; `W_eff = min(120, configured window) - 5` so a user who shortens the window cannot make the promise stricter than their own setting. |
| `P1_SHALLOW_F` | 2.0 (starting number was 3.0, see K1) | P1 | `S_eff = min(2.0, smallest of the five configured tolerances) - 0.5`; flags only a dip shallower than 1.5 F at defaults. |
| `READING_SLACK_F` / `TIME_SLACK_MIN` | 0.5 F / 5 min | all | Whole-degree thermostat readings and timer jitter. |
| `P1_NATIVE_MARGIN_F` | 3.0 | P1 | Heat clause active only if `forecast_high >= threshold_mild + 3`; cool clause only if `forecast_high < threshold_warm - 3`. Production's natural-side exemption (tolerance 0) applies on cool/cold and hot/warm days; the margin also covers the 2 F classification hysteresis, so the promise stays silent near every day-type boundary. |
| `P4_REVERSAL_SETPOINT_F` | 2.0 | P4 | Production's setpoint-match tolerance is 1.0 F (`OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F`). |
| `P5_BLIND_CONTINUE_MIN` (proposed P5b) | 35 | P5b | Production auto-stops at 30 min (`INDOOR_BLIND_FAN_STOP_S`) with up to 5 min latency. |
| `P6_MAX_FLIPS` / `P6_WINDOW_MIN` | 2 / 60 | P6 | Production's dwell lock would permit 6 per hour; breach switches bypass it. |
| `P7_MAX_MIN` / `P7_SLACK_F` / `P7_MAX_GAP_MIN` | 45 / 1.0 / 35 | P7 | See 4.7. |
| `P7_OVERSHOOT_EXPLAINED_F` / `P7_OVERSHOOT_WINDOW_MIN` | 5.0 / 120 | P7 | Largest default settling tolerance (hot/cold days) and the default recency window. |
| `OCCUPANT_ECHO_S` / `OCCUPANT_EXCUSE_MIN` | 45 / 30 | P1, P4, P6, P7 | Cloud thermostats echo a CA command about 25 s later (#1049). |
| `SURFACE_TOL_F` | 0.6 | P8 | Whole-degree display rounding. |

`tests/test_promise_numbers.py` walks the legal configuration grid (clamp ranges read from `const.py`: tolerances 1-8, window 15-360) and asserts, for every combination, that the effective promise bound is no stricter than the production hold bound. If someone later changes a default or a clamp so a promise becomes stricter than production, that test fails first. This is the executable form of "strictly looser than the algorithm".

### 3.2 `PromiseConfig`

Built by `PromiseConfig.from_config(config)` from the raw config dict only: `fan_mode`, `comfort_heat/cool`, `sleep_heat/cool`, `sleep_time`, `wake_time`, `aggressive_savings`, the five `comfort_deadband_*_f`, `comfort_family_recency_window_min`, `threshold_mild`, `threshold_warm`, `temp_unit`. It never imports `AutomationEngine` or any helper from `automation.py` (no `select_comfort_band`, no `_in_sleep_window`): the promise owns a six-line `in_sleep(t)` with midnight wrap and "malformed -> False". Four derived lookups, each choosing the value that keeps the promise looser for the check that uses it. Outside the 60 minutes either side of a sleep/wake boundary all four equal the configured value for that time (`sleep_*` inside the sleep window, `comfort_*` outside); inside the transition each takes the extreme of the two:

| Lookup | Transition value | Used by | Why this direction is the safe one |
|---|---|---|---|
| `floor_hi(t)` | higher floor | P1 heat clause | Legit furnace starts happen at `indoor < F_true - tolerance`; the flagged set is `indoor > F_ref - 1.5`; with `F_ref >= F_true` the two never overlap. |
| `ceiling_lo(t)` | lower ceiling | P1 cool clause | Mirror. |
| `floor_lo(t)` | lower floor | P7 range | A house is "outside" only below `floor - 1`; a lower floor flags less. |
| `ceiling_hi(t)` | higher ceiling | P7 range | Mirror. |

Away, vacation, aggressive savings and TOU expansion only ever *lower* the true floor and *raise* the true ceiling, so `floor_hi` is always at or above production's true floor and `floor_lo` at or below it (when `aggressive_savings` is on, P7 widens its range by a further 2.0 F). The promise can never be stricter than production on these axes.

**Celsius users (confirmed).** Every temperature in `Reading`/`Command`/`PromiseNumbers`/`Hit.evidence` is internal degrees F. Readings are built with `read_state_temp_f`/`to_fahrenheit` (the single sanctioned boundary, `temperature.py`), config values are already stored as F, and thresholds are F constants. Only `Hit.detail` (occupant text) is rendered in the user's unit via `format_temp`/`format_temp_delta` (a pure module). Log lines stay in F per the logging guideline. The generator gets a `temp_unit` axis (section 10) so this is exercised, not assumed.

### 3.3 Derived helpers (pure, in `invariant_watchdog.py`)

```python
def equipment(r: Reading) -> str | None:
    """heating | cooling | idle | fan | None(unknown). Uses hvac_action when present;
    otherwise infers from mode + setpoint with a 0.5 F swing (and marks the Hit 'inferred')."""

def occupant_actions(inp: PromiseInput) -> list[OccupantAction]:
    """A mode, setpoint (>= 0.5 F) or fan-state change between consecutive readings with NO
    matching CA Command to that target inside [reading.ts - OCCUPANT_ECHO_S, reading.ts + 5 s].
    Needs only readings + commands, so it is identical in production and tests."""
```

### 3.4 Result types and the one entry point

```python
@dataclass(frozen=True, slots=True)
class Hit:
    promise: str                       # PromiseSpec.key, e.g. "settling_flip"
    ts: datetime                       # when the broken promise happened (not when it was found)
    key: str                           # stable identity of this occurrence (episode / command / stretch start)
    detail: str                        # plain English, user unit, no entity ids
    evidence: tuple[tuple[str, str | float | bool | None], ...]   # numbers in F, fixed vocabulary
    kind: str                          # "event" | "state"

@dataclass(frozen=True, slots=True)
class PromiseSpec:
    number: int
    key: str
    check: Callable[[PromiseInput, PromiseConfig], list[Hit]]
    test_severity: str                 # "hard" | "warning"
    home_level: str | None             # "log" | "alert" | None (not checked at home)

PROMISES: tuple[PromiseSpec, ...] = (...)    # still a flat tuple, not a plugin registry

def run_promise_checks(inp: PromiseInput, cfg: PromiseConfig, *, scope: str) -> list[Hit]: ...
```

`scope` is `"home"` or `"test"` and only selects *which promises run* (`home_level is not None` at home; all in tests, P8 via its own path). A check that raises is caught per promise: at home it is logged once at WARNING and treated as no hit (graceful degradation); in tests it is re-raised so a broken check can never hide a real failure. The existing `check_ac_whf_mutex(...)` and `run_invariant_checks(...)` stay importable unchanged.

---

## 4. The eight promises

Format per promise: statement, ground-truth data, boundary condition, exclusions, why it is not the algorithm, tests/home levels. Test severity and home level start as in the confirmed table.

### 4.1 P1 `settling_flip`: after cooling, a shallow dip does not start the heat (and the mirror)

*Past bugs.* #821-#867, #1066, #1071, #1073 (fixed seven times, each fix closing one path).

**Data.** `readings` (equipment state, indoor, setpoint, forecast high), `occupant_actions`, open `override` episodes.

**Boundary (heat clause).** For each consecutive pair `(prev, cur)` where `equipment(prev) != "heating"` and `equipment(cur) == "heating"` (a furnace start; first reading in the window is skipped, no edge is knowable), let `F = floor_hi(cur.ts)` and `dip = F - cur.indoor_f` (positive below the floor). It is a violation when **all** hold:

1. cooling was seen in the last `W_eff` minutes: some `r` with `equipment(r) == "cooling"` and `cur.ts - W_eff <= r.ts < cur.ts`, **and no `heating` reading lies between that cooling and `cur`** (the scan back stops at same-side equipment: a flip is opposite, idle*, same-side; a restart after an earlier in-family heat run is a normal cycle, #1150);
2. `-0.5 <= dip < S_eff - 0.5` (the furnace started in the "shallow dip" zone: at most 0.5 F above the floor, less than 1.5 F below at defaults);
3. the thermostat was armed to *defend the floor*: `cur.setpoint_f <= F + 0.5` (a start toward a higher setpoint is a deliberate bank, pre-heat or occupant choice, not a floor defense; this one test replaces any need to read TOU or pre-heat labels);
4. the heat clause applies today: `cur.forecast_high_f >= threshold_mild + P1_NATIVE_MARGIN_F` (K2; no forecast -> the clause is silent);
5. not excused (below).

**Cool clause (M1).** Mirror: `equipment` goes to `cooling`, heating seen in the window, `dip = cur.indoor_f - ceiling_lo(cur.ts)`, setpoint `>= ceiling - 0.5`, applies only when `forecast_high_f < threshold_warm - P1_NATIVE_MARGIN_F`.

**Excused:** an `occupant_action` on the thermostat within the previous 30 min (of `cur.ts`; optionally, behind `P1_EXCUSE_ANCHOR_ON_ARMING_COMMAND`, default off pending owner sign-off per Invariant 2, also within 30 min before the first CA `set_hvac_mode` into the same family inside that window, #1150), an open thermostat `override` episode, thermostat `hvac_mode` unavailable in either reading, `cur.indoor_f is None`.

**Not the algorithm.** It never mentions the margin zone, hysteresis, dwell lock, tolerance by day type, the mode-change ring buffer, `comfort_holds`, the `ran` source, or the selector. It only says "a furnace that starts at the floor right after the AC ran is a bug, whoever armed it": the selector, a restore, a pause resume, TOU, the ODE guard, or a path nobody has written yet. That path-independence is the point, since the same promise would have caught each of the seven fixes. Numbers come from `PromiseNumbers` and are proven looser (3.1). Hit key: `settling_flip:heat@<cur.ts to minute>`.

**Levels.** Tests hard; home log. Evidence: `minutes_since_opposite`, `dip_f`, `indoor_f`, `floor_f`, `outdoor_f`, `setpoint_f`.

### 4.2 P2 `ac_whf_mutex`: the AC never runs while the whole-house fan runs

*Past bugs.* #739, #748, #1057. Existing check, kept. `check_ac_whf_mutex(*, hvac_action, whf_physically_on, fan_mode)` keeps its exact signature and tests; a new adapter `check_p2_snapshot(inp, cfg)` calls it on `inp.latest` (and `cfg.fan_mode`). The check stays a **snapshot** (no persistence requirement) so production behavior does not change. `PromiseConfig.p2_min_overlap_s` defaults to 0; a real-HA runner whose simulated compressor lags may set it for tests only. Hit key `ac_whf_mutex`, kind STATE. Tests hard, home **alert** (unchanged: CRITICAL log every evaluation, event, notification, Status line).

### 4.3 P3 `observe_only_silence`: with automation switched off, no thermostat or fan commands

*Past bugs.* #1108 (a fan-off sent once while the switch was off; the last of a family: #589, #1102, #1109 each closed one path).

**Data.** `Command.origin == "ca"` and `Command.automation_enabled` (the user's switch, `coordinator.automation_enabled`, *not* the engine's `dry_run`, so a desync between the two is itself caught).

**Boundary.** Any command with `target in {"thermostat","fan"}`, `origin == "ca"` and `automation_enabled is False`. No tolerance, no exclusions (hard). Notifications and logs are not commands and are not journaled.

**Production note (honest).** The hook sits below the observe-only gate, so at home this only fires if some path reaches the sender while the switch is off and the engine flag is wrong or bypassed. It is a backstop, which is why it starts log-only. **In tests it is the strongest form**: it reads the FakeHass `action_log` / real-HA service bus, which sees every call regardless of what CA thinks. Combined with the static choke-point guard (M3) there is no unobserved path. Hit key `observe_only_silence:<ts>:<service>`, kind EVENT. Tests hard; home log -> alert (promotion planned, the plan's "log -> alert").

### 4.4 P4 `override_respected`: a manual override is never undone before its grace ends, except by a listed event

*Past bugs.* #1027, #1045, #1055, #1006, and the open-window family fixed in #1119.

**Data.** `episodes` (CA's declared word) and `commands`/`readings` (the ground-truth effect).

**Boundary (a, reversal).** For each episode and each CA `Command` to the same `target` with `episode.start_ts < cmd.ts <= (episode.end_ts or now)` that *reverses* the occupant's value and is not excused, report a Hit. One Hit per (episode, `cmd.ts`): a `set_hvac_mode` and a `set_temperature` at the same instant are one reversal (#1151); the key is `override_respected:<episode>:<ts>` (no service). When the episode has no remaining grace the wording is "... while your manual change was still in force."; otherwise minutes are rounded half up.

*Reverses:* thermostat: `set_hvac_mode(m)` or a `mode` bundled in `set_temperature` with `m != episode.value`, or a `setpoint_f` that differs from the occupant's by at least `P4_REVERSAL_SETPOINT_F`. Fan `override` (occupant turned it on): CA `turn_off`. Fan `fan_off` episode (occupant turned it off; semantics inverted per #359): CA `turn_on`. A command equal to the occupant's value is a re-affirm, not a reversal.

**Boundary (b, silent early end).** An episode that ends with an unlisted reason while at least one minute of grace remained is a Hit (kind EVENT) even without a reversal command, because from then on nothing protects it. `grace_cancelled{reason:"superseded"}` is listed only when a new episode for the same target opens within 5 s.

**Listed exceptions (derived from `grace-periods-spec.md` callsites and code; K3):**

| Ending / excuse | Thermostat override | Fan override | Fan-off grace | Source |
|---|---|---|---|---|
| Natural grace expiry (`grace_expired`) | ends legitimately | ends legitimately | ends legitimately | callsites 1-3 |
| Dashboard cancel / End Grace (`user_cancel_override`, `user_cancel_fan_override`) | yes | yes | yes | callsite 6, #508, #1095 |
| Bedtime / morning wake-up | **no** (they skip; a command here is the #1119 shape) | yes (#1027, kept 2026-10-07) | no | callsites 4-5 |
| Occupancy away / vacation (`clear_manual_override(reason="occupancy_*")`) | yes | yes (same primitive) | no | callsite 7 |
| A newer occupant action on the same or the conflicting device (second override #201; occupant sets AC while the WHF runs = hard mutex, #714/#748: the newer setting wins) | yes | yes | yes | spec "Second override", invariant 10 |
| `override_adopted` / `adopted_matching_decision` | yes (nothing reversed) | n/a | n/a | callsite 8 |
| `override_self_resolved` (thermostat reverted before confirmation) | yes | n/a | n/a | PATH B |
| Indoor above the comfort ceiling during a `fan_off` grace (the #134 exception) | n/a | n/a | yes | spec "Fan-off grace" |
| HA restart / unload / reload (clean slate #282; #1006 and #1045 survivors are Step 2's real-HA lifecycle tests, because the journal is empty after restart) | blind | blind | blind | section 2.3 |
| `stuck_grace_recovered` | **not excused** (a watchdog killing a protected grace is the #530 defect shape) | not excused | not excused | |

**Not the algorithm.** It does not evaluate the gate (`DEFER_OVERRIDE`), the confirm window, the FSM, or any flag; it asks only "after the occupant spoke and CA acknowledged it, did CA send the opposite command before a listed ending?". Gate bugs of any shape (#1119: routines switching HVAC off through an open window) surface as a reversal command. Tests hard; home log.

### 4.5 P5 `no_blind_fan_start`: no fan start while the indoor temperature is unreadable

*Past bugs.* #1042, #1037, #1043.

**Boundary.** A CA command with `target == "fan"` and `fan_on is True`, or `climate.set_fan_mode` to `"on"`, where the indoor reading CA had at send time was unreadable: `cmd.indoor_f is None`. Test-side adapters that lack the hook stamp `indoor_f` from the newest `Reading` at or before `cmd.ts` within 15 min (a missing reading means "cannot tell", no Hit). No exclusions other than `origin == "ca"`.

**P5b (proposed, M2):** `fan_on is True` in every reading for more than `P5_BLIND_CONTINUE_MIN` while `indoor_f is None` throughout. Warning/log. Hit key `no_blind_fan_start:<ts>`. Tests hard (P5), warning (P5b); home log.

### 4.6 P6 `mode_flip_rate`: at most 2 heat/cool flips per hour (warning)

**Data.** `readings` only. The family of a mode is `heat` or `cool` (`off`, `heat_cool`, `auto`, `fan_only` ignored; a pause that returns to the same family is not a flip, `heat -> off -> cool` is). **Boundary:** collapse consecutive same-family readings, list the timestamps where the family changes, drop flips that follow an `occupant_action` by less than 5 min, and report a Hit at the third flip inside any 60-minute window. Key `mode_flip_rate:<third flip ts>`. Not the algorithm: it counts mode changes the thermostat actually reported, regardless of who wrote them (dwell lock, selector, restore are never mentioned). Tests warning; home log.

### 4.7 P7 `comfort_kept`: while in control, the house is not outside the comfort range for more than 45 minutes (warning)

*Past bug.* #1066 (the house sat past an edge while armed on the wrong side).

**Range.** Promise-owned, from raw config: `[floor_lo(t) - 1.0, ceiling_hi(t) + 1.0]`, each edge also widened by 2.0 F when `aggressive_savings` is on. Not the engine's resolved band and not the chart band (P8 compares those).

**In control** means none of these is true at the reading: occupancy away or vacation (marks); any `contacts_open`; `fan_on` true or a `nat_vent_*` session open (marks); an open `override`/`fan_off` episode or an `occupant_action` in the last 60 min; thermostat unavailable or indoor unreadable; `automation_enabled` false (a `Command` or mark says so); within 60 min after a `restart`/`startup_coalesced` mark; equipment working in the right direction (`cooling` while above, `heating` while below, i.e. undersized equipment is not CA's fault); **overshoot-explained** excursions (the opposite equipment ran in the last 120 min and the excursion is at most 5.0 F, which is exactly the legitimate settling hold, so a correct hold never counts).

**Boundary.** Consecutive in-control readings outside the range, with gaps no longer than 35 min, whose first-to-last duration exceeds 45 min. Hit when first exceeded; key `comfort_kept:<stretch start>`; evidence `minutes`, `worst_f`, `side`. Only meaningful where the house responds to commands (K5): evaluated in closed-loop generated runs and in the real-HA runner, **reported-only on open-loop goldens** (the indoor curve there is an exogenous script). Tests warning; home log.

### 4.8 P8 `surfaces_agree`: what CA shows matches the range the thermostat is holding (test-only)

Specified in section 8. Tests hard with a ratcheted allow-list; not evaluated at home (`HOME_LEVEL` = `None`).

---

## 5. Severity, promotion, dedup, wording

### 5.1 Tables (module constants in `invariant_watchdog.py`, not user settings)

| # | key | `TEST_SEVERITY` | `HOME_LEVEL` now | Kind | Promotion target |
|---|---|---|---|---|---|
| 1 | `settling_flip` | hard | log | EVENT | alert after two quiet weeks |
| 2 | `ac_whf_mutex` | hard | **alert** (existing) | STATE | already |
| 3 | `observe_only_silence` | hard | log | EVENT | alert (planned in the confirmed table) |
| 4 | `override_respected` | hard | log | EVENT | alert |
| 5 | `no_blind_fan_start` | hard | log | EVENT | alert |
| 6 | `mode_flip_rate` | warning | log | EVENT | stays log |
| 7 | `comfort_kept` | warning | log | EVENT | stays log |
| 8 | `surfaces_agree` | hard (ratcheted) | none | n/a | never at home |

### 5.2 Promotion log -> alert

1. `promise_stats[key]` (persisted, 8 keys) counts hits with `first_ts`/`last_ts`; it is exposed in `coordinator.data["promise_stats"]`, the diagnostic snapshot and the Debug tab. "Two quiet weeks" = `now - max(last_ts, first_evaluated_ts) >= 14 days` on the live install.
2. Every hit seen during the quiet-period run is either fixed (issue, regression test) or its promise number is tuned in `PromiseNumbers` with a note. A hit is never silently waived.
3. Promotion is a one-line change to `HOME_LEVEL`, shipped as a normal patch release (version bump, `fix_history` entry via `tools/add_fix_entry.py`, CHANGELOG), so it is reviewed and reversible.

### 5.3 Dedup

- **EVENT** promises: the `Hit.key` is a stable occurrence identity, and the journal's own `seen_keys` LRU (256) is the dedup, so a 3 h window that still contains the violating event next cycle is not re-reported. `_recent_duplicate` is **not** used for these: it keeps only the last signature per key, so hit A, hit B, then A again in the next window would re-report A.
- **STATE** promise (P2): keeps the existing 300 s window through `_recent_duplicate("invariant_violation:ac_whf_mutex", ("ac_whf_mutex",), window_seconds=300)`, now keyed per promise (K4) instead of the shared `"invariant_violation"` key.
- The CRITICAL log line for P2 stays per evaluation (unchanged behavior); log-level hits log once per key at WARNING and repeat at DEBUG.

### 5.4 Wording (occupant first; log lines lead with the outcome, then `key=value`)

| Promise | `Hit.detail` (plain English, user unit, no entity ids) | Log line (home) |
|---|---|---|
| P1 heat | "Your heat started {m} min after the AC cooled the house, while the house was only {d} under your comfort minimum. It should have waited." | `Household promise missed (log only): promise=settling_flip side=heat minutes_since_opposite=38 dip_f=0.8 indoor_f=67.2 floor_f=68.0 outdoor_f=79.0` |
| P1 cool | "Your AC started {m} min after the heat ran, while the house was only {d} over your comfort maximum. It should have waited." | same shape, `side=cool` |
| P2 | existing text | existing CRITICAL line unchanged |
| P3 | "Climate Advisor sent a command to your thermostat or fan while automation was switched off." | `Household promise broken: promise=observe_only_silence target=fan service=turn_off` (alert: ERROR) |
| P4 | "Climate Advisor changed your {thermostat/fan} setting {r} min before your manual change was due to end." (no remaining grace: "... setting while your manual change was still in force.", #1151) | `... promise=override_respected target=thermostat remaining_min=14 reversed_to=off` |
| P5 | "The fan was started while the indoor temperature could not be read." | `... promise=no_blind_fan_start` |
| P6 | "The thermostat flipped between heat and cool {n} times in the past hour." | `... promise=mode_flip_rate flips=3 window_min=60` |
| P7 | "The house stayed {d} outside your comfort range for {m} minutes while Climate Advisor was in control." | `... promise=comfort_kept minutes=52 worst_f=3.4 side=below` |

Log-level wording is WARNING `Household promise missed (log only): ...`; alert-level is ERROR `Household promise broken: ...` (CRITICAL stays reserved for P2). The notification text for an alert is `detail` plus "This should never happen. Please check the Climate Advisor activity log." The old "Climate Advisor is stepping in to prevent this" sentence is correct only for P2 and is kept only there.

---

## 6. Observability: definition of done

| Requirement (CLAUDE.md) | Design |
|---|---|
| **Logging** | INFO once at start: `Household promises armed: promises=7 alert=ac_whf_mutex log=settling_flip,...`. INFO once per config change for a promise made inapplicable (e.g. `Household promise not checked: promise=settling_flip reason=window_below_floor`). WARNING/ERROR per hit as in 5.4. Pass/skip outcomes at DEBUG. No credentials or entity ids. |
| **Status tab** | Alert-level hits only, via the existing `coordinator.data["invariant_violations"]` -> `api.py` -> the existing red "warning sign" line in `index.html` `loadStatus()`. No new card; no change to Next User Action / Next Automation / Automation Time (Status Card Ontology: this is "what is wrong right now", the Status card's question). EVENT-kind alerts stay visible for 60 minutes after detection (`_promise_status: {key: (until_ts, detail)}`, at most 8 entries); STATE-kind while persisting plus the current cycle. Log-level hits never appear on Status. |
| **Event log** | Reuse `invariant_violation` (backward compatible, no renderer breakage). Payload gains `promise` (1-8), `severity` (`log`/`alert`), `episode_ts` alongside `invariant` and `detail`. Log-level hits are emitted too, so the AI Investigator and Activity Report can see them. |
| **Activity catalog** | Update the existing `invariant_violation` row in `docs/activity-report-table.md` (new payload keys, "Deduped per promise key"). No new event type, therefore no new `_NO_DEDUP` or `EVENT_RENDERERS` entry. |
| **AI context** | `_render_invariant_violation` (`ai_skills_context.py`): label = `detail` (prefixed "Noted, not alerted: " when `severity == "log"`); settings column `promise {n}: {invariant} ({severity})`. |
| **Persisted state** | `promise_stats` added to the coordinator's saved state with type validation and the 8-key cap. |
| **Chart coverage** | **None required, stated explicitly.** No promise changes a setpoint; the watchdog never commands. `_compute_target_band_schedule()` and the ODE curve are untouched. P8 *reads* the chart band, it does not write it. |
| **Violation protocol** | If the I8 reviewer finds any proposed log/Status/chart gap, stop and ask (CLAUDE.md). |

---

## 7. Thread-safety, performance, security

- **Pure and cheap.** `PromiseJournal.record_*` is O(1) (deque append plus one comparison). `run_promise_checks` is O(window): at most 400 readings, 128 commands, 128 marks, no nested scan beyond a bounded lookback. Estimated microseconds to low milliseconds, well under the 10 ms CPU heuristic. No file, socket, subprocess or `tempfile` call is introduced, so no `async_add_executor_job` offload is needed and nothing is added to `tests/test_executor_offload.py`'s `_BLOCKING_METHODS`. `promise_stats` rides the existing `_async_save_state()` (already off-loop).
- **Event-loop safety.** Hooks are synchronous callbacks that never `await`. The command observer is wrapped `try/except Exception` and logs at DEBUG, so a journal fault can never block or abort a thermostat command (graceful degradation).
- **Never commands.** The module imports no service layer; it returns data. Home Assistant boundary rule untouched; nothing is written outside the integration.
- **Security.** No entity ids, tokens, names or notify-service names in any `Hit`, log, event or notification (the `thermostat`/`fan` generic labels follow `_zone_label()`). `promise_stats` is validated on load (`isinstance(dict)`, key allow-list, ints only), capped, and written through the existing atomic save with its `0o600` handling.
- **Multi-zone.** Each zone coordinator owns its own journal; tick callbacks are already wrapped by `_zone_scoped_sync`.

---

## 8. Promise 8: what Climate Advisor shows matches the band the thermostat is holding (test-only)

**Rule (DOCTRINE).** P8 calls the real production function for each surface and compares the result with the engine's armed band. It never re-derives a band. The pure comparison `check_surfaces(armed, surfaces, tol)` lives in `invariant_watchdog.py`; the readers live in `tools/promise_probe/surfaces.py` because they need live coordinator/engine objects (coordinator-mode runs, and the real-HA runner).

| Surface | Reader (real function) | Compared | Applies when |
|---|---|---|---|
| **Armed (reference)** | `engine._resolved_comfort_band(engine._current_classification)` -> `(floor, ceiling)`; plus the **live thermostat setpoint** (ground truth) must equal the active-family edge, within 0.6 F | the reference | a classification exists and the engine is not paused/overridden |
| Chart band now | `coordinator._target_band_lower_upper_now()` after `_resolve_target_band_schedule()`; also `_comfort_score_band_now()` | lower, upper | always (the schedule is hourly with ramps, so compare only at hour marks and outside a ramp, or compare the chart row value stored by the same cycle) |
| Status API | `ClimateAdvisorStatusView.get()` -> `ca_target_heat`, `ca_target_cool`, back-converted with `to_fahrenheit` | both edges | always |
| Briefing | real `generate_briefing(..., verbosity="tldr_only")`; the `HVAC Mode: Heat at X / Cool at X` row parsed with a regex and back-converted | the quoted edge for the day's family | awake window and the same occupancy mode the briefing assumes (it is a morning snapshot built from `select_comfort_band(..., in_sleep_window=False)`) |
| Next Action comparison band | `coordinator._compute_next_action(...)`, **boundary-probed**: it reads flat `comfort_heat/cool` from config and exposes no band, so vary `indoor_temp` and find where the guidance flips; compare the flip point with the armed edge | the derived edge | probe only when guidance depends on indoor vs ceiling/floor |
| ODE guard ceiling | `engine._ceiling_threshold(comfort_cool)` (the `ceiling_threshold` input the classification-FSM input builder passes to `decide_ode_ceiling_guard`) | ceiling | day is eligible for the guard (`hvac_mode == "off"`, home, awake) |
| Nat-vent cycling target | `coordinator._nat_vent_target_now()` vs `compute_nat_vent_target` inputs | target | nat-vent session open |

*UNVERIFIED, to confirm in I6:* the exact status-view entry point for a unit test (the existing `_simulate_status_get` pattern in `tests/test_api.py` applies) and whether the ODE guard exposes the ceiling through a function or only through the `OdeCeilingGuardInputs` it builds. Fallback for any surface with no readable function: boundary probing as for Next Action.

**Ratchet.** First runs will show real divergences (the plan expects P8 to show where the "compute the band once" refactor should start; `_compute_next_action` reading flat config during sleep or away is a likely first one). Known divergences live in `tools/simulations/surface_divergences.json`: `{surface, scenario|context, issue}`. A listed divergence is tolerated, an unlisted one fails the test, and a listed one that no longer diverges also fails ("remove the stale entry"). That keeps P8 hard without blocking on the production refactor.

---

## 9. Test-side wiring

### 9.1 Interfaces (the shape Architect A and I must agree; Coordinator reconciles)

```python
class Session(Protocol):                    # implemented by FakeHass runner AND real-HA runner
    def feed(self, event: dict) -> None: ...        # dispatch one scenario event, then settle
    def advance_to(self, ts: datetime) -> None: ...  # move the (virtual/frozen) clock, firing timers
    def now(self) -> datetime: ...
    def thermostat(self) -> ThermostatView: ...      # mode, setpoint, action, indoor (read-only; used by the closed-loop world)
    def finish(self) -> RunResult: ...               # event_log, action_log, engine_state, callback_errors, promise_report

class PromiseSource(Protocol):               # one adapter per world
    def poll(self, now: datetime) -> SourceBatch: ...   # new Readings, Commands, event Marks since the last poll, time-ordered
    def config(self) -> PromiseConfig: ...
```

```python
probe = PromiseProbe(source, scope=Scope.TEST, waivers=load_waivers(name))
probe.step(label)          # poll -> journal.record_* -> run_promise_checks -> collect Hits; never raises
report = probe.report()    # PromiseReport(hard=[...], warnings=[...], stats)
```

`PromiseProbe` is the *only* object a runner needs to know. It owns a `PromiseJournal`, so tests and production exercise the same journal and the same `run_promise_checks`.

### 9.2 Hook in the FakeHass runner (additive, default off)

`run_production_scenario` is refactored in behavior-preserving fashion into a `ProductionSession` (setup, `feed`, `advance_to`, `finish`) plus the existing function as a thin loop. Goldens must be byte-identical. With a probe attached, the loop is:

```python
for i, event in enumerate(events):
    session.advance_to(event_dt, tick=probe)       # advances in <= 5-min slices, calling probe.step("tick") per slice
    session.feed(event)                            # dispatch + settle (existing code)
    probe.step(f"event[{i}]:{etype}")
```

Splitting `advance_to` into slices is behavior-neutral (the heap fires callbacks in time order either way). The validation bar for the increment is "every golden (91 scenario files at v0.8.1) produces identical `event_log` and `action_log` with and without the probe". Without a probe the code path is the single existing `scheduler.advance_to(event_dt)` call.

Additive runner event types (each maps to a real production entry point or to FakeHass state, none changes engine semantics): `automation_toggle` (`coordinator.set_automation_enabled()` / `engine.dry_run`), `indoor_unreadable` (clears `current_temperature`), `thermostat_action` (sets only `hvac_action`).

### 9.3 Where violations land

- `ProductionRunResult.promise_report` (new optional field).
- `simulate.run_scenario_production()` result gains `promise_violations` (hard) and `promise_warnings`. **It does not change `passed`, `check_assertion` or `production_outcome_at`**, so `outcomes.py` semantics are untouched and rule #8 is not triggered. Each entry: `{promise, severity, ts, step, detail, evidence, replay: {scenario, event_index, virtual_ts}}`.
- `tests/test_promises_on_goldens.py` (new, parametrized like `test_production_harness.py`): fails on any un-waived hard violation, printing the replay pointer and the last 10 journal records. Warnings are compared with `tools/simulations/promise_baseline.json` per scenario and may only go down (ratchet, written by the Toolsmith's baseline run).
- **Waivers** are a sidecar `tools/simulations/promise_waivers.json` (`{scenario, promise, reason, issue}`), never an edit to a golden (goldens are LOCKED). A waiver whose violation no longer occurs fails the test (stale). `pending-fix` negative scenarios are waived by default for the promise their verdict names.
- `python -m tools.promise_probe replay <file> --until <ts>` prints the journal window leading to a Hit.

### 9.4 What Architect A must provide for the real-HA runner

A `RealHaSource` and `RealHaSession` honoring 9.1: a time-ordered batch of readings from `state_changed`, commands from `EVENT_CALL_SERVICE` with `origin` from a marked occupant `Context`, `coordinator._event_log` read incrementally for marks, `now()` from the frozen clock, and `probe.step()` after each scripted step and each `async_fire_time_changed`. The thermostat in that world is `ca_dev_thermostat_sim`, which already models the real thermal behavior, so the closed-loop generator needs no toy house there and P7 becomes fully meaningful.

---

## 10. Random-day generator

### 10.1 Shape

Hypothesis draws a **plan** (pure data, so shrinking works); a **closed-loop driver** executes it against a `Session`; the realised event list is written as an ordinary scenario file.

```
plan (st.builds)  ->  driver loop per 5-min tick:
                        world.step(thermostat(), outdoor(t))  -> indoor, hvac_action
                        session.feed(temp_update / thermostat_action ...)
                        probe.step("tick")
                      (plus the plan's sparse exogenous events at their times)
                  ->  RunRecord: realised events -> scenario JSON  (+ promise report)
```

`ToyHouse` is a physical plant, not CA logic: first-order RC (`d(indoor)/dt = k*(outdoor-indoor) + solar + hvac_gain`), thermostat swing of 0.5 F and compressor overshoot of about 0.5 F (the very physics behind #1066/#1073), `hvac_action` derived from the real FakeHass mode and setpoint. Per-example cost target: under 0.5 s for a one-day engine-only run (to be measured in I3; the budgets in 10.4 assume it).

### 10.2 What the plan varies (no knowledge of any promise or past bug)

| Axis | Values |
|---|---|
| Weather | forecast high/low and trend drawn per day; the day's classification is produced by the **real** `classify_day()` over a generated `ForecastSnapshot` and emitted as the `classification` event, so no classification field is hand-set (golden policy: never override a field to fit an assertion). Cloud and noise on the outdoor curve. |
| Config | `comfort_heat/cool`, `sleep_heat/cool`, `sleep_time/wake_time`, the five deadbands (80% default, 20% varied inside their clamps), recency window, `aggressive_savings`, `fan_mode` (all four), TOU `schedules` present/absent, `temp_unit` fahrenheit/celsius, `manual_grace_seconds` (including 0). |
| Occupancy | away/home/vacation/guest transitions at random times. |
| Doors and windows | 0-3 sensors, open durations from minutes to hours, partial closes. |
| Occupant | thermostat mode and setpoint pokes (`thermostat_state_changed`), fan on/off (`external_fan_state_change`), dashboard cancel (`cancel_override`). |
| Hazards | `simulate_restart`, thermostat `unavailable` blips, `indoor_unreadable` windows, `automation_toggle` off/on, coordinator refresh timing (`coordinator_refresh`). |

Hazard and occupant axes need the real listeners, so those examples run with `use_coordinator=True`; the rest run engine-only for speed. The mix is a plan field.

### 10.3 Output, shrinking, replay

- Format: the existing scenario schema (`name, description, config, events[]`, `use_coordinator`, `skip_startup_coalesce`) plus one extra top-level key `"generated": {generator_version, seed, profile, plan, found}` (unknown top-level keys are ignored by the runner). **No new engine event types**; the three runner-consumed additions are listed in 9.2.
- Because the realised `temp_update`/`thermostat_action` injections are recorded, replay through the *standard* runner is deterministic without Hypothesis. The file is written from the **final shrunk** example (the driver re-executes Hypothesis's minimal example once and saves its realised events), so the artifact is the small one, not the first failure.
- Locations: `tools/simulations/generated/failures/<UTC>_<promise>_<seed>.json` (gitignored, uploaded as a CI artifact); local example database `.hypothesis/` (gitignored). Reviewed findings are moved by hand to `tools/simulations/promise_corpus/` (committed), which every PR replays forever (a finding becomes a permanent regression test). A promoted scenario still goes through the existing `pending/` -> `golden/` review (Golden Simulation Test Policy and the three-exercise protocol: would it fail if the behavior were deleted; docs vs production; what the occupant experiences).
- Seed control: `--seed`, env `CA_PROMISE_SEED`, `@seed` in the pytest profile, Hypothesis `print_blob`.

### 10.4 Budgets

| Profile | Where | Examples | Determinism | Target wall time |
|---|---|---|---|---|
| `smoke` | unit tier, every PR | 25 plans + the whole committed corpus | `derandomize=True` | 60 s |
| `pr` | opt-in label and merges to main | 100 plans | `derandomize=True` | 3 min |
| `nightly` | scheduled workflow (new `.github/workflows/promises-nightly.yml`) | 3000 plans, time-boxed 40 min | fresh random seed, printed | 40 min; any new finding opens an issue draft with the replay file |
| `trial` | local, once (section 11) | 5000 plans x 5 seeds | fixed seeds | measured |

PR runs are derandomized so an unrelated PR is never blocked by a previously unknown bug; nightly explores. `hypothesis` is added to the `test` dependency group (I0).

---

## 11. Trial gate

**Question answered.** Does the generator, with promises 1-3 and no hint about the settling guard, find the #1073 regression on the pre-fix code, and stay quiet on current code?

**Commits.** Pre-fix: `d7209f4d096d76d8cb3acd388756b5b5632f2ab4` (merge of PR #1072, parent of the fix; the regression window is `d7209f4..a80d773`). Fixed reference: `a80d773` (and current `HEAD`, v0.8.1, as the false-positive control).

**Procedure (scripted in `tools/promise_probe/trial.py`, run once):**

1. **Freeze first.** Commit the promise code, the generator and the seeds; record the commit hash in the report. No edits to axes or numbers after seeing pre-fix results except a declared "tuning commit" listed in the report.
2. **Export, don't check out.** `git archive d7209f4 custom_components tools tests/conftest.py | tar -x -C <scratch>/prefix`. A scratch export has no `.git`, so nothing can be committed or confused with the working tree.
3. **Overlay only new files:** `custom_components/climate_advisor/promise_journal.py`, the new `invariant_watchdog.py`, and all of `tools/promise_probe/`. Do **not** overlay `run_production.py`/`fake_hass.py`: the probe attaches by wrapping `run_production._dispatch_event` and the clock slices from outside (`attach_probe(run_production_module)`), and the FakeHass adapter falls back to polling `states.get` and diffing `action_log` by index. The only old-tree surfaces it needs (`action_log`, `event_log`, `states.get`, the virtual clock) all exist at `d7209f4`.
4. **Run** `python -m tools.promise_probe generate --profile trial --promises 1,2,3 --seed 1..5 --out <scratch>/results` from the scratch root with `PYTHONPATH` set to it, in the `test` group environment (stubbed Home Assistant).
5. **Controls:** repeat on `a80d773` and on `HEAD` with the same seeds.

**Pass criteria (all must hold):**

| # | Criterion |
|---|---|
| a | On `d7209f4`, a P1 hard violation is found within budget on **at least 3 of 5 seeds**. |
| b | The shrunk example shows a furnace start in the dip zone shortly after a cooling run (the #1073 mechanism). The saved replay, run on `a80d773`, shows no P1 violation (revert test). |
| c | On `a80d773` and `HEAD`: **zero** P1 hard violations across all seeds. Any P2/P3 hard hit there is triaged: real bug -> issue; promise too strict -> tune the number with a note (this is the calibration the plan asks for). |
| d | The generator's coverage report shows non-trivial exploration (for example, days with cooling then heating inside two hours occur in a meaningful share of examples). Coverage is judged on generic axes only, never tuned toward #1073. |

**If it fails.** Do not build P4-P8 or the production wiring. Report which axis the generator lacked (the likely candidates are TOU banks, occupant setpoint pokes and overnight pre-cool), widen that axis generically, and re-run once. A second failure is a stop-and-ask for the user.

---

## 12. Increments (dependency order, with write boundaries)

`coordinator.py` and `automation.py` are touched only in I8, in the minimal places listed.

| # | Increment | Writes (files) | Depends on | Validation bar |
|---|---|---|---|---|
| **I0** | Dependency + ignore rules: add `hypothesis` to the `test` group, relock, ignore `.hypothesis/` and `tools/simulations/generated/` | `pyproject.toml`, `uv.lock`, `.gitignore` (**Craftsman owns env files now; coordinate**) | none | `scripts/test.py unit` green; lock reproducible |
| **I1** | Pure core, **trial scope**: records, journal (readings, commands, marks; episodes deferred to I5), `PromiseNumbers`, `PromiseConfig`, `Hit`/`PromiseSpec`/`PROMISES`, `run_promise_checks`, severity/home constants, and the checks for **P1, P2 (adapter) and P3** only. Legacy `check_ac_whf_mutex`/`run_invariant_checks` kept verbatim | NEW `custom_components/climate_advisor/promise_journal.py`; EDIT `invariant_watchdog.py`; NEW `tests/test_promise_journal.py`, `tests/test_promise_checks.py`, `tests/test_promise_numbers.py`; EDIT `tests/test_invariant_watchdog.py` only to add (not change) cases | none | ruff clean; new tests pass with `-W error::pytest.PytestUnraisableExceptionWarning -W error::RuntimeWarning`; **per-promise negative control**: a minimal history rebuilt from the past bug fails, the compliant twin passes; `test_promise_numbers` green over the full config grid; existing watchdog tests unchanged and green |
| **I2** | `Session` refactor + `PromiseProbe` + FakeHass adapter + hook + goldens test | NEW `tools/promise_probe/{__init__,probe,sources_fakehass,waivers}.py`; EDIT `tools/sim_harness/run_production.py` (Session split, tick slices), `tools/sim_harness/fake_hass.py` (additive `add_state_observer`), `tools/simulate.py` (result keys only); NEW `tests/test_promises_on_goldens.py`, `tools/simulations/promise_waivers.json`, `promise_baseline.json` | I1 | **every golden gives identical `event_log`/`action_log` with and without the probe**; `tools/simulate.py --check-integrity` clean; `test_production_harness.py` unchanged and green; every hard hit on a golden is either triaged to an issue or waived with a reason |
| **I3** | World, generator, replay, CLI, runner event additions | NEW `tools/promise_probe/{world,generator,replay,__main__}.py`; EDIT `tools/sim_harness/run_production.py` (+3 elif branches: `automation_toggle`, `indoor_unreadable`, `thermostat_action`); NEW `tests/test_promise_generator.py`, `tests/test_promise_random_days.py` (smoke profile) | I2 | same seed -> same scenario JSON; replay reproduces the result; a deliberately broken engine (test-only monkeypatch) is found and shrinks to a short example; measured seconds per example recorded |
| **I4** | **Trial gate** (no production code) | NEW `tools/promise_probe/trial.py`, report `plans/test-automation/trial-gate-report.md` | I3 | section 11 pass criteria; **go/no-go for everything below** |
| **I5** | Remaining pure checks, only after the trial passes: episodes derivation, **P4, P5 (+P5b), P6, P7**, `check_surfaces` (the pure half of P8), numbers calibrated from the trial | EDIT `promise_journal.py`, `invariant_watchdog.py`; EDIT `tests/test_promise_journal.py`, `tests/test_promise_checks.py`, `tests/test_promise_numbers.py` | I4 | as I1, with a negative control per new promise rebuilt from its past bug (P4: #1119 wake-up through an open window; P5: #1042 economizer with indoor `None`; P6/P7: #1066/#1073 flip-flop and wrong-edge arming) |
| **I6** | P8 surfaces + ratchet | NEW `tools/promise_probe/surfaces.py`, `tests/test_promise_surfaces.py`, `tools/simulations/surface_divergences.json` | I2 (needs coordinator-mode runs) | every reader returns a value on a golden; stale-entry rule proven by a test; first divergences filed as issues, listed with numbers |
| **I7** | Static choke-point guard | NEW `tests/test_service_call_choke_points.py` (AST walk: every `*.services.async_call` for `climate`/`fan`/`switch` or a non-literal domain sits in `_call_service_with_context` or `_async_command_fan_entity`) | none (can run any time) | passes today; adding a third call site fails it |
| **I8** | **Production wiring, log-only** (version bump, `fix_history`, CHANGELOG: Coordinator batches) | EDIT `coordinator.py` at **eight** points: (1) `__init__` creates `_promise_journal`, `_promise_stats`, `_promise_status`; (2) `_emit_event` one line `journal.on_event(entry)`; (3) `_async_thermostat_changed` first lines `journal.record_reading(self._promise_reading_now())`; (4) `_async_thermal_sample_tick` one line `self._tick_household_promises(now)`; (5) `_run_invariant_watchdog` body delegates to `_evaluate_household_promises` (same name and signature kept for existing tests); (6) `_async_save_state`/`async_restore_state` carry `promise_stats` (validated, capped); (7) `_async_command_fan_entity` one observer call; (8) `data` dict gets `promise_stats`. EDIT `automation.py` at **two** points: `__init__` `_command_observer = None`; `_call_service_with_context` one `_observe_command(...)` call above `hass.services.async_call` (+ the small helper). EDIT `ai_skills_context.py` (`_render_invariant_violation`), `diagnostic_snapshot.py` (`promise_stats`), `frontend/index.html` (Debug tab row only; Status unchanged), docs below. NEW `tests/test_promise_wiring.py`; EDIT `tests/test_activity_renderers.py` | I1, I5 (so I4 has passed), I7 | wiring tests use `object.__new__(ClimateAdvisorCoordinator)` + `types.MethodType` per the established pattern; P2 behavior byte-identical (existing tests green); log-level hit produces WARNING + event and **no** notification and **no** Status line; alert-level produces all four; dedup proven with two interleaved promises (K4); observer fault does not break a command; full unit suite green |
| **I9** | Adapter parity test | NEW `tests/test_promise_adapter_parity.py` | I2, I8 | the same coordinator-mode scenario produces equal journals (readings, commands, marks) through the production hooks and through the FakeHass adapter |
| **I10** | Documentation convergence | NEW `docs/household-promises-spec.md` (Tier 3); EDIT `docs/invariant-watchdog-brief.md` (Interfaces, Invariants 1-2 per K6, Disclosure Path), `docs/activity-report-table.md`, `docs/00-PROJECT-INSTRUCTIONS.md` anchors, `docs/sim-harness-spec.md` (Session, probe, three runner events), `docs/DEVELOPMENT.md`, CLAUDE.md Testing section | I8 | every touched doc's Anchors table filled; Scribe verifies spec against code; coverage score does not go down (issue #131) |
| **I11** | CI nightly | NEW `.github/workflows/promises-nightly.yml` | I3 | one dry-run dispatch; artifact upload works |

**Lane disjointness.** I0, I1, I7 can run in parallel (disjoint files). I2 then I3 share `run_production.py` with Architect A's lane: Coordinator decides which lane applies the Session split first; the other rebases. I6 is disjoint from I8. I8 is the only increment that touches shared production files, and its touch points are enumerated above so a reviewer can verify the diff is exactly that.

**Version bump note.** Any increment that edits `custom_components/` (I1 adds a module and edits one; I8 edits several) needs `const.py` `VERSION` + `manifest.json` + `fix_history.jsonl` via `tools/add_fix_entry.py` (omit `--user-summary`; internal tooling) + a CHANGELOG entry. Recommended: one patch release containing I1 + I7 + I8 (nothing user-visible changes until log-level hits exist), and a separate tiny release per promotion (5.2).

---

## 13. Decisions and open items

### 13.1 Needs the user (defaults applied so work can proceed; none blocks I0-I4)

1. **P1 number 3 F -> 2 F (effective flag zone 1.5 F), plus the forecast-high gate for each clause and the mirror (cool) clause** (K1, K2, M1). The confirmed 3 F would have fired on every correct mild/warm/cool-day furnace start at 2-3 F under the floor. The numbers are tunable per the plan; this is the starting value that is provably not stricter than default production. Also: the brief's Invariant 2 requires explicit human sign-off to add or loosen an invariant, so the I1 pull request shows the eight promises in the human-readable form of section 4 for approval.
2. **P4 exception list** (K3): the issue's "(bedtime, wake-up, end-grace button)" becomes the per-target table in 4.4. The one consequence worth knowing: bedtime or wake-up changing a *thermostat* override is a violation, not an exception (that is what #1119 fixed).
3. **P5b** (M2): add the "fan keeps running blind" clause? Default: included as warning/log, off if declined.

### 13.2 Decisions made here (reversible)

- Reuse the `invariant_violation` event type with extra payload keys instead of adding a new type.
- Journal in memory only; no seeding from the chart log in this step.
- P7 is reported-only on open-loop goldens; meaningful only where the house responds.
- 5-minute tick as the production evaluation heartbeat.

### 13.3 UNVERIFIED (confirm during the named increment)

- Payload keys of `occupancy_*` and `nat_vent_*` events (I1).
- The status-view entry point and the ODE-guard ceiling reader for P8 (I6).
- Seconds per generated example and therefore whether the 10.4 budgets hold (I3).
- Whether `use_coordinator=True` examples are fast enough to be more than a minority of the mix (I3).
- That an `automation_toggle` mapped to `coordinator.set_automation_enabled()` is faithful enough for P3 in coordinator mode (I3).

### 13.4 Not covered by these eight (noted, not filed)

Frozen-but-numeric indoor temperature (#1043), CA's own comfort accounting versus the promise's (the promise 7 range deliberately does not read `_comfort_score_band_now()`), and restart-survival of the `fan_off` and `fan_manual_override` graces (#1006, #1045), which belong to Step 2's real-HA lifecycle tests.

---

## 14. User decisions (2026-10-29), resolving §13.1

| Item | Decision |
|---|---|
| K1 / M1: Promise 1 boundary | **Follow the setting.** "Shallow" = the user's own configured settling tolerance minus 0.5 °F of slack (`S_eff = min(2.0, smallest configured tolerance) − 0.5` per §4). The promise is never stricter than production. Both directions (heat-after-cool, cool-after-heat), gated by the forecast high (K2). |
| K3: Promise 4 exceptions | **Split by device.** Fan overrides may be ended by bedtime, wake-up, end-grace button and the documented fan exceptions. Thermostat overrides may be ended only by away/vacation, a newer occupant action, the end-grace button, or the AC/WHF hard mutex (plus adoption, self-resolve, restart-as-blind per §4 table). |
| M2: Promise 5b | **Yes.** Add P5b, "a fan already running doesn't keep running blind > 35 min". Warning in tests, log-only at home. 9 promises total. |
| K6: Watchdog brief Invariant 1 | **Amend for P4 only.** P4 may read CA's own override/grace events. Every other promise reads only ground truth (thermostat, fan, actually-issued commands). Record the amendment in `docs/invariant-watchdog-brief.md` with this date. |
