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
└── sensor.py            # 6 HA sensor entities for dashboards
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
                       └──► End of Day → Learning Engine (save DailyRecord)
                                              │
                                              └──► Suggestions (after 14+ days)
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

## Sensors Exposed

| Entity ID | Value | Extra Attributes |
|-----------|-------|-----------------|
| `sensor.climate_advisor_day_type` | hot/warm/mild/cool/cold | trend_direction, trend_magnitude |
| `sensor.climate_advisor_trend` | warming/cooling/stable | (dynamic icon) |
| `sensor.climate_advisor_next_action` | Human-readable next action | — |
| `sensor.climate_advisor_daily_briefing` | Truncated briefing (255 char) | full_briefing (complete text) |
| `sensor.climate_advisor_comfort_score` | 0–100% | pending_suggestions, suggestions list |
| `sensor.climate_advisor_status` | active/inactive | — |

## Services Registered

| Service | Data | Purpose |
|---------|------|---------|
| `climate_advisor.respond_to_suggestion` | action (accept/dismiss), suggestion_key | User responds to learning suggestion |

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
sensor_debounce_seconds: 300    # how long a door/window must stay open before HVAC pauses (default 5 min)
manual_grace_seconds: 1800      # hands-off window after user manually turns HVAC on (default 30 min)
manual_grace_notify: false      # send notification when manual grace period expires
automation_grace_seconds: 3600  # settling period after Climate Advisor auto-resumes HVAC (default 60 min)
automation_grace_notify: true   # send notification when automation grace period expires
wake_time: "06:30"
sleep_time: "22:30"
briefing_time: "06:00"
```

### Debounce and Grace Period System

**Debounce** (`sensor_debounce_seconds`): A door or window must remain open for this duration before HVAC is paused. Quick pass-throughs that close within the window have no effect. Default: 5 minutes.

**Manual grace period** (`manual_grace_seconds`): After the user manually turns HVAC back on during a door/window pause, Climate Advisor stays hands-off for this duration. Door/window sensors cannot trigger another pause during this window — the user just overrode the system and should not be immediately overridden back. Default: 30 minutes. Notification on expiry: off by default.

**Automation grace period** (`automation_grace_seconds`): After Climate Advisor itself resumes HVAC (all doors/windows closed), it waits this duration before door/window sensors can trigger another pause. This prevents rapid cycling when someone is moving in and out. Default: 60 minutes. Notification on expiry: on by default so the user knows normal sensing has resumed.

Setting either grace period to 0 disables it entirely.

**Briefing integration**: The daily briefing automatically mentions active grace periods so users understand why door/window sensing may behave differently than expected. The fresh air section also shows the actual configured debounce duration (e.g., "5 minutes" instead of a hardcoded value) so the briefing always reflects the user's settings.

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
- Automation grace period: 3600 seconds (60 min)
- Occupancy setback delay: 15 minutes
- Max continuous runtime alert: 3 hours

### Learning Parameters
- Min data points before suggesting: 14 days
- Suggestion cooldown: 7 days
- Low compliance threshold: 30%
- High compliance threshold: 80%
- Data retention: 90-day rolling window
- Storage: JSON file in HA config dir
