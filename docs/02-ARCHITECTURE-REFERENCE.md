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
door_window_sensors: [binary_sensor.back_door, ...]
wake_time: "06:30"
sleep_time: "22:30"
briefing_time: "06:00"
```

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
- Door/window pause delay: 180 seconds (3 min)
- Occupancy setback delay: 15 minutes
- Max continuous runtime alert: 3 hours

### Learning Parameters
- Min data points before suggesting: 14 days
- Suggestion cooldown: 7 days
- Low compliance threshold: 30%
- High compliance threshold: 80%
- Data retention: 90-day rolling window
- Storage: JSON file in HA config dir
