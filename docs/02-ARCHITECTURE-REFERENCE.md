# Climate Advisor — Architecture Reference

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
One day's tracked data: what was recommended, what actually happened, outcomes (runtime, overrides, comfort violations). Stored as JSON, rolling 90-day window.

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

These methods on `ClimateAdvisorCoordinator` implement the Issue #114 two-parameter physics observation pipeline. They are driven by thermostat state changes detected in `_async_thermostat_changed`.

| Method | Role |
|--------|------|
| `_start_thermal_event(session_mode)` | Begins a new observation window; sets state to `active` |
| `_sample_thermal_event()` | Records a sample during the active (HVAC-on) phase |
| `_end_active_phase()` | Transitions from `active` to `post_heat` when HVAC stops |
| `_check_stabilization()` | Tests for post-heat temperature stabilization; triggers commit or continues polling |
| `_commit_thermal_event()` | Calls `learning.commit_thermal_event()` to extract k_passive/k_active and persist |
| `_abandon_thermal_event(reason)` | Discards the event (timeout or bad data); logs a WARNING |
| `_update_pre_heat_buffer()` | Maintains the 15-min rolling pre-HVAC sample buffer for richer regression |

The pending event is serialised in `LearningState.pending_thermal_event` (persisted to disk) so a mid-event HA restart can recover the post-heat phase.

## Coordinator Chart Helper Functions

Pure module-level functions called by `get_chart_data()` to build the dashboard temperature forecast payload.

| Function | Role |
|----------|------|
| `_compute_target_band_schedule(hourly_timestamps, config, occupancy_mode, now)` | Returns `[{ts, lower, upper}]` — the occupancy-aware dynamic target band for each forecast hour. Away/vacation applies flat setback for today only; home/guest uses wake/sleep ramp schedule. Future days always use the home schedule. (Issue #119) |
| `_build_predicted_indoor_future(hourly_forecast, config, now, ...)` | Returns `[{ts, temp}]` — predicted future indoor temperatures. Uses physics ODE when model has confidence ≥ `"low"` and `k_passive < 0`; falls back to ramp interpolation otherwise. Accepts `occupancy_mode` parameter and calls `_compute_target_band_schedule()` internally so prediction and target band share a single source of truth. (Issues #114, #119) |
| `_build_future_forecast_outdoor(hourly_forecast)` | Returns `[{ts, temp}]` — raw hourly outdoor forecast temperatures. |
| `_simulate_indoor_physics(t_current, t_outdoor, q, k_passive, dt_hours)` | Single ODE time step for the physics path. |
| `_compute_ramp_hours(temp_delta, rate)` | Computes ramp duration for the legacy fallback path. |

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
- Minimum post-heat samples for regression: 10 (`THERMAL_MIN_POST_HEAT_SAMPLES`)
- k_passive sanity bounds: −0.5 to −0.001 hr⁻¹
- k_active_heat sanity bounds: 0.5 to 15.0 °F/hr
- k_active_cool sanity bounds: −15.0 to −0.5 °F/hr

### Chart Log Parameters
- Entry cadence: every coordinator tick (~30 min)
- Retention cap: 365 days rolling (~17,500 entries ≈ 2MB)
- Downsampling: raw points ≤3 days; hourly averages 4–30 days; daily summaries >30 days
- Storage: `climate_advisor_chart_log.json` in HA config dir

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
