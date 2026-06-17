<!-- Nav: ← [Strategy and Design](01-STRATEGY-AND-DESIGN.md) → [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) -->

# Climate Advisor — Architecture Reference

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What are the 16 source files and what does each own? | Every module has a single responsibility: `coordinator.py` orchestrates, `classifier.py` classifies, `automation.py` executes HVAC calls, `learning.py` persists and analyses behavior, etc. | [§File Structure](02-ARCHITECTURE-REFERENCE.md#file-structure) |
| How does data flow from the weather entity to an HVAC service call? | Weather entity → coordinator (every 30 min) → `classify_day()` → `DayClassification` → `apply_classification()` in the automation engine → `climate.set_temperature` / `climate.set_hvac_mode`. | [§Data Flow](02-ARCHITECTURE-REFERENCE.md#data-flow) |
| What are the five coordinator-scheduled daily events and when do they fire? | Briefing (default 6:00 AM), morning wakeup (6:30 AM), bedtime setback (10:30 PM), end-of-day save (11:59 PM), and the 30-minute forecast refresh loop. | [§Coordinator Scheduled Events](02-ARCHITECTURE-REFERENCE.md#coordinator-scheduled-events) |
| What is the debounce / grace period system and how do the three timers interact? | Debounce delays a pause until a sensor stays open for the configured time. Manual grace (default 30 min) blocks new pauses after a user override. Automation grace (default 5 min) blocks re-pause after system resumes. Manual override always wins. | [§Debounce and Grace Period System](02-ARCHITECTURE-REFERENCE.md#debounce-and-grace-period-system) |
| What sensors does Climate Advisor expose and what do their attributes carry? | Eight sensor entities: day type, trend, next action, daily briefing, comfort score, status, occupancy mode, and AI status — each with diagnostic attributes. | [§Sensors Exposed](02-ARCHITECTURE-REFERENCE.md#sensors-exposed) |
| How does the thermal state machine (v3) work and which methods own each phase? | Ten coordinator methods drive six concurrent observation types in `_pending_observations` (Issue #121); HVAC observations survive HA restarts via `LearningState.pending_thermal_event`. | [§Coordinator Thermal State Machine Methods](02-ARCHITECTURE-REFERENCE.md#coordinator-thermal-state-machine-methods) |
| How does state persistence work — what is stored, where, and in what format? | Two JSON files: `climate_advisor_state.json` (runtime coordinator state, atomic write) and `climate_advisor_learning.json` (thermal model + observation history). State version mismatch discards and resets; no migration chain. | [§State Persistence Brief](state-persistence.md) |
| How does unit conversion work between °F and °C? | `from_fahrenheit()` for absolute temperatures (subtracts 32, ×5/9); `convert_delta()` for differences and rates (×5/9 only, no offset). Unit is user-selected in config flow, not auto-detected from HA. | [§Temperature Conversion Brief](temperature-conversion.md) |
| How does the REST API work — endpoints, auth, and data access pattern? | 19 views, all requiring a HA long-lived token. GETs read from `coordinator.data`; POSTs delegate to the coordinator or automation engine. Two config fields are redacted; out-of-range numerics are silently clamped. | [§REST API Brief](rest-api.md) |
| How does the Claude AI integration work — circuit breaker, rate limiting, skills, budget? | `ClaudeAPIClient` wraps all API calls with a three-state circuit breaker, per-month budget cap, and exponential-backoff retry. `AISkillRegistry` provides a pluggable skill framework; two skills are registered: `activity_report` and `investigator`. | [§AI Integration Brief](ai-integration.md) |
| Where is the full Tier 3 occupancy dispatch spec — priority resolution, state transitions, setback formulas? | The Territory spec covers all four occupancy modes, GUEST > VACATION > HOME/AWAY priority, toggle entity wiring, 7-row state transition table, setback formulas, and persistence across HA restarts. | [§Occupancy Dispatch Spec](occupancy-dispatch-spec.md) |
| Where is the Tier 3 spec for the chart state log — entry schema, ring buffer contracts, retention, persistence? | The Territory spec covers the raw/hourly/daily entry schemas, append ordering invariant, 8-path load error table, atomic write contract, downsampling tiers, and the 17,520-entry (≈1 year) cap. New `fan_running`/`nat_vent_active` fields and merged Vent bar documented in §Vent Bar Fields (Issue #331). | [Chart State Log — Territory Spec](chart-log-spec.md) |
| Where is the Tier 3 spec for the Activity Report per-event table — event vocabulary, setpoint convention, renderer runbook? | The Territory spec covers 34 event types in 6 groups, `_format_band_setpoint` single-setpoint convention, `EVENT_RENDERERS` registry, `_default_renderer` contract, dedup mechanics, and the four-step runbook for adding a new renderer. | [Activity Report Table Spec](activity-report-table.md) |
| What replaced the ClimateSimulator and how does the production harness work? | Issue #236 eliminated the standalone simulator engine. `tools/sim_harness/` drives the real `AutomationEngine` headless via FakeHass + FakeScheduler. `tools/simulate.py` is now a CLI/MANIFEST shell only. | [Sim Harness Brief](sim-harness-brief.md) |
| What are the full contracts for FakeHass, FakeScheduler, run_production, and outcomes? | FakeHass service-bus interception + state-feedback loop; FakeScheduler virtual clock patching contract; run_production event dispatch table including predicted_indoor ODE re-classification; outcomes custom assertion types. | [Sim Harness Spec](sim-harness-spec.md) |

## File Structure

```
custom_components/climate_advisor/
├── __init__.py          # Integration setup, service registration
├── manifest.json        # HA integration metadata (domain, dependencies, version)
├── const.py             # Constants: thresholds, defaults, attribute names
├── config_flow.py       # 3-step setup wizard (entities → sensors → schedule)
├── strings.json         # UI text for config flow steps
├── coordinator.py       # Central brain: scheduling, events, data flow, state
├── classifier.py        # Day type + trend classification from forecast
├── briefing.py          # Daily email/notification text generation
├── automation.py        # HVAC service calls, door/window pause, occupancy
├── learning.py          # Pattern tracking, suggestion generation, persistence
├── sensor.py            # HA sensor entities for dashboards
├── switch.py            # Automation enable/disable switch (observe-only mode)
├── claude_api.py        # Centralized Claude API client: auth, retry, circuit breaker, rate limiting, budget tracking. Provides async_request() for all AI features.
├── ai_skills.py         # AI skills framework: lightweight registry for pluggable AI analysis capabilities. Skills register a context builder, response parser, and optional fallback.
├── ai_skills_activity.py  # Activity Report skill (first AI skill): gathers system state, sends to Claude for analysis, returns structured report with timeline, decisions, anomalies, diagnostics.
├── chart_log.py         # Chart state log: persistent 1-year ring buffer of HVAC/fan/temp data points and event markers, used by the Temperature Forecast chart.
├── repairs.py           # HA Repairs integration: surfaces actionable fix prompts when CA detects config or data problems.
├── ai_skills_investigator.py  # AI Investigator skill: structured internal-state context builder for the Claude investigator.
└── frontend/            # Dashboard panel (iframe): index.html + locally bundled Chart.js v4 + zoom plugin + HammerJS
```

## Data Flow

```
Weather Entity ──► Coordinator (every 30 min)
                       │
                       ├──► Classifier → DayClassification
                       │         │
                       │         ├──► Automation Engine (apply HVAC changes)
                       │         └──► Briefing Generator (daily email)
                       │
                       ├──► Door/Window Events → Automation Engine (pause/resume)
                       ├──► Thermostat Events → Learning Engine (track overrides)
                       ├──► Time Events → Automation Engine (bedtime/morning)
                       │
                       ├──► End of Day → Learning Engine (save DailyRecord)
                       │                      │
                       │                      └──► Suggestions (after 14+ days)
                       │
                       └──► AI Service Calls ──► claude_api.py (circuit breaker, budget)
                                                       │
                                                  ai_skills.py (skill registry)
                                                       │
                                             ai_skills_activity.py (Activity Report)
                                                       │
                                             AI Status Sensor + Report History
```

## Key Data Structures

### ForecastSnapshot (classifier.py)
Contains today_high, today_low, tomorrow_high, tomorrow_low, current_outdoor_temp, current_indoor_temp, current_humidity, timestamp.

### DayClassification (classifier.py)
Produced by `classify_day()`. Contains day_type, trend_direction, trend_magnitude, and computed recommendations: hvac_mode, pre_condition, windows_recommended, window_open/close times, setback_modifier.

### DailyRecord (learning.py)
One day's tracked data: what was recommended, what actually happened, outcomes (runtime, overrides, comfort violations). Stored as JSON, rolling 90-day window. v0.3.48 adds five setback-visibility fields: `setback_heat_applied_f`, `setback_cool_applied_f`, `setback_depth_f`, `setback_was_adaptive`, `setback_skipped_reason`. See [§6a DailyRecord setback fields](08-COMPUTATION-REFERENCE.md#6a-occupancy-aware-automation-guards-issue-85).

## Coordinator Scheduled Events

| Time | Event | Handler |
|------|-------|---------|
| Briefing time (default 6:00 AM) | Send daily briefing | `_async_send_briefing` |
| Wake time (default 6:30 AM) | Restore comfort setpoint | `_async_morning_wakeup` |
| Sleep time (default 10:30 PM) | Apply bedtime setback | `_async_bedtime` |
| 11:59 PM | Save daily record, reset | `_async_end_of_day` |
| Every 30 minutes | Refresh forecast + classification | `_async_update_data` |

## Coordinator State Listeners

| Entity | Event | Handler |
|--------|-------|---------|
| Door/window sensors | State change (open/closed) | `_async_door_window_changed` |
| Climate entity | State change (temp, mode) | `_async_thermostat_changed` |
| Occupancy toggle entities (home/vacation/guest) | State change (on/off) | `_async_occupancy_changed` |

## Coordinator Thermal State Machine Methods

These methods on `ClimateAdvisorCoordinator` implement the Issue #121 v3 concurrent-observation pipeline. They are driven by thermostat state changes detected in `_async_thermostat_changed` and by the 30-minute coordinator tick.

| Method | Role |
|--------|------|
| `_start_hvac_observation(session_mode)` | Begins an HVAC (heat or cool) observation; pre-loads the pre-heat buffer |
| `_start_decay_observation(obs_type)` | Begins one of the four rolling-decay observation types |
| `_sample_all_observations()` | Per-tick: samples all active observations in `_pending_observations`; starts new decay types when trigger conditions first become true |
| `_end_hvac_active_phase(obs_type)` | Transitions HVAC obs from `active` to `post_heat` when HVAC stops |
| `_check_hvac_stabilization(obs_type)` | Tests for post-heat stabilization; triggers commit or continues polling |
| `_evaluate_rolling_window(obs_type)` | Commit/keep-alive decision for rolling-window decay types (30-min min, 240-min cap) |
| `_commit_rolling_window_obs(obs_type)` | Calls `_commit_observation()` for a rolling obs that has passed the signal check |
| `_commit_observation_if_sufficient(obs_type)` | Commits if `len(samples) >= min_samples`, else abandons — used on HVAC-start contamination flush |
| `_abandon_observation(obs_type, reason)` | Removes obs from `_pending_observations`; appends to rejection log; logs WARNING |
| `_commit_observation(obs_type)` | Calls `learning.record_thermal_observation()` and pops obs from `_pending_observations` |

All six concurrent types are tracked in `_pending_observations: dict[str, PendingObservation]`. The pending HVAC event is serialised in `LearningState.pending_thermal_event` so a mid-event HA restart can recover the post-heat phase. See [Thermal Model v3 Spec](thermal-model-v3-spec.md) for the full observation-type matrix and lifecycle.

> **v2 pipeline coexistence note:** The v2 HVAC-specific methods (`_start_thermal_event`, `_sample_thermal_event`, `_end_active_phase`, `_check_stabilization`, `_commit_thermal_event`, `_abandon_thermal_event`, `_update_pre_heat_buffer`) remain in the codebase as a **parallel, independent pipeline** — they are not internal helpers of the v3 methods. On every HVAC start, `_async_thermostat_changed` calls both `_start_thermal_event` and `_start_hvac_observation`, and the 30-minute tick calls both `_sample_thermal_event`/`_check_stabilization` (v2) and `_sample_all_observations`/`_check_hvac_stabilization` (v3). The v2 pipeline has not been retired. `_update_pre_heat_buffer` is shared infrastructure used by both paths.

## Coordinator Chart Helper Functions

Pure module-level functions called by `get_chart_data()` to build the dashboard temperature forecast payload.

| Function | Role |
|----------|------|
| `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now)` | Returns `[{ts, lower, upper}]` — the occupancy-aware dynamic target band for each forecast hour. Away/vacation applies flat setback for today only; home/guest uses wake/sleep ramp schedule. Future days always use the home schedule. (Issue #119) |
| `_build_predicted_indoor_future(hourly_forecast, config, now, ...)` | Returns `[{ts, temp}]` — predicted future indoor temperatures. Uses physics ODE when model has confidence ≥ `"low"` and `k_passive < 0`; falls back to ramp interpolation otherwise. Accepts `occupancy_mode` parameter and calls `_compute_target_band_schedule()` internally so prediction and target band share a single source of truth. (Issues #114, #119) |
| `_build_future_forecast_outdoor(hourly_forecast)` | Returns `[{ts, temp}]` — raw hourly outdoor forecast temperatures. |
| `_simulate_indoor_physics(t_current, t_outdoor, q, k_passive, dt_hours)` | Single ODE time step for the physics path. |
| `_compute_ramp_hours(temp_delta, rate)` | Computes ramp duration for the legacy fallback path. |

`get_chart_data(range_str: str = "24h", before_ts: float | None = None)` — the `before_ts` parameter (Unix milliseconds, Issue #160) enables historical navigation. When supplied, the data window is anchored at that timestamp instead of the current clock: `get_entries(range_str, before=datetime.fromtimestamp(before_ts/1000, UTC))`. The frontend passes `before_ts` to scroll the chart to any past window without losing the current live view.

**Data window vs viewport:** `before_ts` shifts the *data window* (what the API fetches from the chart log). The frontend *viewport* is a separate concern — range preset buttons and drag-to-zoom control the display without necessarily changing `before_ts`. When `before_ts` is absent, the window ends at now (live mode).

`get_chart_data()` also returns two setpoint arrays added in v0.3.48 (Issue #151):

| Response key | Content |
|---|---|
| `historical_setpoint` | `[{ts, temp}]` — actual thermostat `target_temperature` captured at each 30-min poll; `null` entries when HVAC mode is off |
| `predicted_setpoint` | `[{ts, temp}]` — future setpoint derived from target band (lower bound in heat mode, upper bound in cool mode, null in off mode) |

Both are rendered in the dashboard as a stepped purple/magenta line: solid past, dashed future, faint-dotted forward-fill during off-mode periods. Toggle via the "Thermostat Setpoint" overlay checkbox (hidden by default).

## Sensors Exposed

| Entity ID | Value | Extra Attributes |
|-----------|-------|-----------------|
| `sensor.climate_advisor_day_type` | hot/warm/mild/cool/cold | trend_direction, trend_magnitude |
| `sensor.climate_advisor_trend` | warming/cooling/stable | (dynamic icon) |
| `sensor.climate_advisor_next_action` | Human-readable next action | — |
| `sensor.climate_advisor_daily_briefing` | Truncated briefing (255 char) | full_briefing (complete text) |
| `sensor.climate_advisor_comfort_score` | 0–100% | `pending_suggestions`, `comfort_violations_minutes_today`, `comfort_range_low`, `comfort_range_high` |
| `sensor.climate_advisor_status` | active/inactive | — |
| `sensor.climate_advisor_occupancy_mode` | home/away/vacation/guest | occupancy_entity_states (raw toggle states) |
| `sensor.climate_advisor_ai_status` | active/inactive/error/disabled/circuit_open | last_request_time, error_count, total_requests, model_in_use, circuit_breaker, monthly_cost_estimate, auto_requests_today, manual_requests_today |

## Services Registered

| Service | Data | Purpose |
|---------|------|---------|
| `climate_advisor.respond_to_suggestion` | action (accept/dismiss), suggestion_key | User responds to learning suggestion |
| `climate_advisor.ai_activity_report` | (none) | Trigger an on-demand AI activity report analysis |
| `climate_advisor.get_ai_report` | (none) | Retrieve the most recent AI activity report |
| `climate_advisor.clear_ai_reports` | (none) | Clear persisted AI report history |

## Configuration Data (from config flow)

```
weather_entity: weather.home
climate_entity: climate.living_room
outdoor_temp_entity: sensor.outdoor_temp (optional)
indoor_temp_entity: sensor.indoor_temp (optional)
comfort_heat: 70 (°F)
comfort_cool: 75 (°F)
setback_heat: 60 (°F)
setback_cool: 80 (°F)
notify_service: notify.mobile_app_phone
door_window_sensors: [binary_sensor.back_door, binary_sensor.all_windows, ...]  # any binary_sensor, including groups
sensor_polarity_inverted: false  # true if sensors report on=closed instead of on=open
sensor_debounce_seconds: 300    # how long a door/window must stay open before HVAC pauses (default 5 min, UI: 0–60 min)
manual_grace_seconds: 1800      # hands-off window after user manually turns HVAC on (default 30 min, UI: 0–240 min)
manual_grace_notify: false      # send notification when manual grace period expires
automation_grace_seconds: 300   # settling period after Climate Advisor auto-resumes HVAC (default 5 min, UI: 0–240 min)
automation_grace_notify: true   # send notification when automation grace period expires
# Note: Config UI displays minutes; values are stored internally as seconds
wake_time: "06:30"
sleep_time: "22:30"
briefing_time: "06:00"
occupancy_home_entity: binary_sensor.someone_home (optional)
occupancy_home_inverted: false          # true if on=away instead of on=home
occupancy_vacation_entity: input_boolean.vacation_mode (optional)
occupancy_vacation_inverted: false
occupancy_guest_entity: input_boolean.guest_mode (optional)
occupancy_guest_inverted: false
```

### Debounce and Grace Period System

**Debounce** (`sensor_debounce_seconds`): A door or window must remain open for this duration before HVAC is paused. Quick pass-throughs that close within the window have no effect. Default: 5 minutes.

**Manual grace period** (`manual_grace_seconds`): After the user manually turns HVAC back on during a door/window pause, Climate Advisor stays hands-off for this duration. Door/window sensors cannot trigger another pause during this window — the user just overrode the system and should not be immediately overridden back. Default: 30 minutes. Notification on expiry: off by default.

**Automation grace period** (`automation_grace_seconds`): After Climate Advisor itself resumes HVAC (all doors/windows closed), it waits this duration before door/window sensors can trigger another pause. This prevents rapid cycling when someone is moving in and out. Default: 5 minutes. Notification on expiry: on by default so the user knows normal sensing has resumed.

Setting either grace period to 0 disables it entirely.

**Timer priority**: Manual override always takes highest priority. When a user manually turns HVAC on during a door/window pause:
1. The pause is immediately lifted (`paused_by_door` → False)
2. All pending debounce timers for still-open sensors are cancelled
3. A manual grace period starts, blocking any new pause events for its configured duration
4. After the grace period expires, normal door/window sensing resumes

This ensures the user's explicit action is never overridden by a stale or pending debounce timer.

```
Sequence: sensor opens → debounce timer → HVAC paused → user turns on
           → manual grace starts (all debounce timers cancelled)
           → grace expires → normal sensing resumes
```

**Briefing integration**: The daily briefing automatically mentions active grace periods so users understand why door/window sensing may behave differently than expected. The fresh air section also shows the actual configured debounce duration (e.g., "5 minutes" instead of a hardcoded value) so the briefing always reflects the user's settings.

## Integration Version

- **Canonical version**: `manifest.json` `"version"` field (shown in HA integrations UI)
- **Python constant**: `const.VERSION` (used in startup logs, API responses, diagnostics)
- **Format**: semantic versioning (`MAJOR.MINOR.PATCH`)
- **Sync rule**: both locations MUST match. A test in `tests/test_version_sync.py` enforces this automatically.
- **When releasing**: update both `const.py` and `manifest.json`.

Note: `config_flow.VERSION` (config entry schema) and `state.STATE_VERSION` (state file format) are separate internal versioning concerns and do not track the integration release version.

## Constants (const.py)

### Day Type Thresholds
- HOT: ≥ 85°F
- WARM: ≥ 75°F
- MILD: ≥ 60°F
- COOL: ≥ 45°F
- COLD: < 45°F

### Trend Thresholds
- Significant: 10°F+ change
- Moderate: 5°F+ change

### Timing Defaults
- Sensor debounce: 300 seconds (5 min)
- Manual grace period: 1800 seconds (30 min)
- Automation grace period: 300 seconds (5 min)
- Occupancy setback delay: 15 minutes
- Max continuous runtime alert: 3 hours

### Learning Parameters
- Min data points before suggesting: 14 days
- Suggestion cooldown: 7 days
- Low compliance threshold: 30%
- High compliance threshold: 80%
- Data retention: 90-day rolling window
- Storage: JSON file in HA config dir

### Thermal Model Parameters (Issue #114)
- Post-heat timeout: 45 min (`THERMAL_POST_HEAT_TIMEOUT_MINUTES`)
- Stabilization threshold: 0.3°F over 5 consecutive minutes (`THERMAL_STABILIZATION_THRESHOLD_F`, `THERMAL_STABILIZATION_WINDOW_MINUTES`)
- Sample interval: 60 seconds (`THERMAL_SAMPLE_INTERVAL_SECONDS`)
- Pre-heat buffer window: 15 min / max 15 entries (`THERMAL_PRE_HEAT_BUFFER_MINUTES`)
- Minimum R² for k_passive acceptance: 0.2 (`THERMAL_MIN_R_SQUARED`)
- Minimum post-heat samples for regression: 4 (`THERMAL_MIN_POST_HEAT_SAMPLES`)
- k_passive sanity bounds: −0.5 to −0.001 hr⁻¹
- k_active_heat sanity bounds: 0.5 to 15.0 °F/hr
- k_active_cool sanity bounds: −15.0 to −0.5 °F/hr

### Chart Log Parameters
- Entry cadence: every coordinator tick (~30 min)
- Retention cap: 365 days rolling (~17,500 entries ≈ 2MB)
- Downsampling: raw points ≤3 days; hourly averages 4–30 days; daily summaries >30 days
- Storage: `climate_advisor_chart_log.json` in HA config dir

## Supported Thermostat Configuration

CA issues separate `heat` and `cool` commands (dual `heat_cool` mode was dropped due to
thermostat compatibility bugs). The underlying HVAC system must support **both** heating
and cooling — heat-only or cool-only systems will not receive commands for their unsupported
mode and `_apply_comfort_band` will no-op silently for that day type. This is expected
behavior, not a defect.

## Observe-Only Mode (Disable Automation)

Climate Advisor exposes a `switch.climate_advisor_automation` entity that controls whether real actions are executed.

**When ON (default)**: Normal operation — all thermostat changes and notifications are executed.

**When OFF (observe-only)**: The full computation pipeline continues (classification, decision-making, state tracking, logging) but all HA service calls are skipped:

- `climate.set_hvac_mode` — skipped
- `climate.set_temperature` — skipped
- `notify.*` — skipped (including daily briefing delivery)

Skipped actions are logged at INFO level with a `[DRY RUN]` prefix:
```
INFO  [DRY RUN] Would set HVAC mode to cool — daily classification — hot day, trend warming 8°F
INFO  [DRY RUN] Would set temperature to 72°F — bedtime — heat setback (comfort 70 - 4 + modifier 2)
INFO  [DRY RUN] Would send notification: Climate Advisor — 🏠 Welcome home! ...
```

### Implementation

Guards are placed at the 3 thermostat primitives (`_set_hvac_mode`, `_set_temperature`, `_notify`) in `AutomationEngine` plus the briefing notification in the coordinator. Higher-level logic (classification application, door/window pause tracking, grace periods, occupancy handling) continues unaffected.

## Automation Engine — Occupancy Methods

| Method | Trigger | Behaviour |
|--------|---------|-----------|
| `handle_occupancy_vacation(active: bool)` | Vacation toggle changes | `active=True`: applies setback + `VACATION_SETBACK_EXTRA` offset. `active=False`: restores comfort setpoint. Logs mode change; respects dry-run guard. |
| `handle_occupancy_guest(active: bool)` | Guest toggle changes | `active=True`: sets comfort setpoint immediately, disables all setback paths. `active=False`: re-evaluates current occupancy state and applies appropriate setpoint. |
| `handle_occupancy_home(home: bool)` | Home/Away toggle changes | Delegates to existing away/return logic with the configured setback delay. |

Priority enforcement lives in `_async_occupancy_changed` (coordinator): it reads all three toggle states, resolves the winner using Guest > Vacation > Home/Away > default, and dispatches to the appropriate handler above.

The toggle state is persisted via `StatePersistence` and survives HA restarts. It is also exposed in the dashboard API (`/api/climate_advisor/status`) and debug state.

### repairs.py — HA Repair Issues

Integrates with Home Assistant's Repairs framework to surface actionable fix prompts
when Climate Advisor detects a configuration or data problem (e.g., a missing entity,
expired token, or learning DB schema mismatch). When a repair issue is raised, the
user sees a "Fix" button in the HA Settings → Repairs UI. Resolving the repair
dismisses it from the queue.

### ai_skills_investigator.py — AI Investigator Skill

Provides the Claude investigator with structured context about Climate Advisor's
internal state: sensor values, HVAC session history, thermal model parameters,
fan status, occupancy mode, and diagnostic guidance. This module knows the
thermostat's deadband behavior (swing) and explains it to the AI so it does not
misinterpret normal deadband oscillation as a CA fault. See
[Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) for swing detection details.
