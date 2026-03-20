# Climate Advisor for Home Assistant

An intelligent HVAC management integration that uses weather forecasts, occupancy, and door/window sensors to minimize energy waste while keeping your home comfortable — and learns from your household's behavior over time.

**Current version: 0.2.1**

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                      Climate Advisor                         │
│                                                              │
│  ┌─────────────┐   ┌──────────────┐   ┌─────────────┐        │
│  │  Classifier │─▶│  Coordinator │◀──│  Learning   │        │
│  │             │   │   (brain)    │   │  Engine     │        │
│  │ • Day type  │   │              │   │             │        │
│  │ • Trend     │   │ • Scheduling │   │ • Tracking  │        │
│  │ • Forecast  │   │ • Briefings  │   │ • Patterns  │        │
│  │   analysis  │   │ • Events     │   │ • Suggest   │        │
│  └─────────────┘   └──────┬───────┘   └─────────────┘        │
│                           │                                  │
│        ┌──────────┬───────┼───────┬──────────┐               │
│        ▼          ▼       ▼       ▼          ▼               │
│  ┌──────────┐ ┌────────┐ ┌─────┐ ┌────────┐ ┌─────────┐      │
│  │Automation│ │Briefing│ │ API │ │Sensors │ │ State   │      │
│  │ Engine   │ │  Gen   │ │     │ │ (13x)  │ │Persist  │      │
│  │          │ │        │ │12   │ │+ 1     │ │         │      │
│  │• HVAC    │ │• Daily │ │REST │ │switch  │ │• Save / │      │
│  │• Door/win│ │  email │ │end- │ │        │ │  restore│      │
│  │• Occupy  │ │• TLDR  │ │point│ │• Status│ │  across │      │
│  │• Fan ctrl│ │• Tips  │ │s    │ │• Learn │ │  restart│      │
│  │• Econom. │ │        │ │     │ │• Fan   │ │         │      │
│  └──────────┘ └────────┘ └─────┘ └────────┘ └─────────┘      │
└──────────────────────────────────────────────────────────────┘
         │            │         │         │
         ▼            ▼         ▼         ▼
   HA Climate    HA Notify   Dashboard  HA Dashboard
   Entity        Service     Panel      Lovelace Cards
```

## How It Works

### Daily Cycle

1. **6:00 AM** — Coordinator pulls forecast, classifies the day, and sends the daily briefing email/notification
2. **6:30 AM** — Morning warm-up restores comfort setpoint
3. **Throughout the day** — Automation engine responds to doors, windows, occupancy, and temperature changes
4. **10:30 PM** — Bedtime setback kicks in
5. **11:59 PM** — Day's data is saved to the learning engine

### Day Types

| Type | Today's High | HVAC Strategy | Human Actions |
|------|-------------|---------------|---------------|
| Hot  | 85°F+       | AC pre-cool, maintain all day | Keep sealed, close blinds |
| Warm | 75–84°F     | Off, AC standby | Open windows morning, close evening |
| Mild | 60–74°F     | Off (heat in AM) | Open windows mid-morning |
| Cool | 45–59°F     | Heat with midday break | Keep closed |
| Cold | Below 45°F  | Heat all day, pre-heat | Keep sealed, help insulate |

### Trend Modifiers

The forecast trend (tomorrow vs. today) adjusts behavior:

- **Warming 10°F+**: More aggressive overnight setback (tomorrow's warmth will help)
- **Warming 5-10°F**: Moderate setback increase
- **Cooling 5-10°F**: Pre-heat in evening, gentler setback
- **Cooling 10°F+**: Significant pre-heat, conservative setback, bank thermal energy

### Occupancy Awareness

Climate Advisor tracks occupancy state via configurable toggle entities:

| Mode | Behavior |
|------|----------|
| Home | Normal operation — full comfort management |
| Away | Setback temperatures applied, notifications reduced |
| Vacation | Extended setback, minimal HVAC activity |
| Guest | Comfort mode — more conservative setbacks |

### Fan Control

Supports whole-house fan and/or HVAC fan mode integration:

- **Whole-house fan**: Controls a dedicated fan entity (switch or fan domain) during economizer maintain phase
- **HVAC fan mode**: Activates your thermostat's fan-only mode for ventilation
- **Both**: Coordinates both fan types together
- Integrated with the economizer two-phase cooling strategy (cool-down with AC, maintain with ventilation)

### Learning Engine

After 14+ days of data, the learning engine starts analyzing patterns:

- **Window compliance**: If you rarely open windows when recommended, it offers to switch to HVAC-only strategies
- **Manual overrides**: Frequent thermostat adjustments suggest setpoints don't match preferences
- **Runtime anomalies**: High HVAC runtime on mild days may indicate sensor gaps
- **Short departures**: Adapts setback timing if you frequently leave for 30-45 minutes
- **Comfort violations**: Suggests less aggressive setbacks if the house is uncomfortable too often
- **Door pauses**: Identifies problem doors and offers to adjust monitoring

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots → Custom repositories
3. Add `https://github.com/yourgithubuser/ha-climate-advisor` as an Integration
4. Search for "Climate Advisor" and install
5. Restart Home Assistant
6. Go to Settings → Integrations → Add Integration → Climate Advisor

### Manual

1. Copy the `custom_components/climate_advisor` folder to your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Go to Settings → Integrations → Add Integration → Climate Advisor

## Configuration

The setup wizard walks you through these steps:

### Step 1: Core Entities
- **Weather Entity**: Your forecast provider (e.g., `weather.home`)
- **Climate Entity**: Your thermostat (e.g., `climate.living_room`)
- **Setpoints**: Separate comfort and setback temperatures for heating and cooling
- **Notification Service**: Where to send briefings (e.g., `notify.mobile_app_phone`)
- **Email Notifications**: Toggle on/off

### Step 2: Temperature Sources
Choose where indoor and outdoor temperature readings come from:
- Weather service (recommended for outdoor)
- Dedicated sensor entity
- Input number helper
- Climate entity fallback (indoor only)

### Step 3: Door/Window Sensors
- Select any binary sensors to monitor (HVAC pauses when open)
- Configure sensor polarity (for inverted sensors)
- Set debounce time (default 5 minutes) and grace periods
- **Fan control**: Choose fan mode (disabled, whole-house fan, HVAC fan, or both) and select fan entity

### Step 4: Occupancy
- Home/away toggle entity (optional)
- Vacation toggle entity (optional)
- Guest toggle entity (optional)
- Polarity inversion for each toggle

### Step 5: Schedule
Set your wake time, bedtime, and when you want the daily briefing.

### Options Flow (Edit After Setup)
All settings are editable after setup, plus advanced options:
- **Learning enabled**: Toggle the learning engine on/off
- **Aggressive savings**: More aggressive energy-saving strategies

## Entities Created

### Sensors (13)

| Sensor | Description |
|--------|-------------|
| `sensor.climate_advisor_day_type` | Today's classification (hot/warm/mild/cool/cold) |
| `sensor.climate_advisor_trend` | Temperature trend direction and magnitude |
| `sensor.climate_advisor_next_action` | Next recommended human action |
| `sensor.climate_advisor_daily_briefing` | Today's briefing TLDR (full text in attributes) |
| `sensor.climate_advisor_comfort_score` | Comfort compliance percentage |
| `sensor.climate_advisor_status` | Integration status |
| `sensor.climate_advisor_next_automation` | Next scheduled automation action |
| `sensor.climate_advisor_next_automation_time` | When the next automation runs |
| `sensor.climate_advisor_occupancy` | Current occupancy mode (home/away/vacation/guest) |
| `sensor.climate_advisor_last_action_time` | Timestamp of last HVAC action |
| `sensor.climate_advisor_last_action_reason` | Why the last HVAC action was taken |
| `sensor.climate_advisor_fan_status` | Fan status (active/inactive/override/disabled) |
| `sensor.climate_advisor_contact_status` | Door/window sensor summary with per-sensor details |

### Switches (1)

| Switch | Description |
|--------|-------------|
| `switch.climate_advisor_automation` | Enable/disable automation (observe-only mode when off) |

## Services

### `climate_advisor.respond_to_suggestion`

Accept or dismiss a learning suggestion.

```yaml
service: climate_advisor.respond_to_suggestion
data:
  action: accept  # or "dismiss"
  suggestion_key: low_window_compliance
```

### `climate_advisor.force_reclassify`

Force re-fetch of forecast data and reclassify the day. Useful for debugging.

```yaml
service: climate_advisor.force_reclassify
```

### `climate_advisor.resend_briefing`

Re-generate and resend the daily briefing notification.

```yaml
service: climate_advisor.resend_briefing
```

### `climate_advisor.dump_diagnostics`

Log a comprehensive diagnostic snapshot to HA logs at INFO level for troubleshooting.

```yaml
service: climate_advisor.dump_diagnostics
```

## Dashboard

Climate Advisor includes a built-in dashboard panel accessible from the HA sidebar. The panel provides:

- **Current Status** — Day type, HVAC mode, setpoint, indoor temp, automation status, contact sensor states, fan status
- **Daily Briefing** — Full briefing with TLDR summary table, verbosity control (tldr_only/normal/verbose)
- **Classification Details** — Forecast data, window schedules, trend analysis
- **Learning** — Today's record, suggestions, compliance tracking
- **Settings** — Read-only view of all configuration grouped by category
- **Debug** — Automation state, force reclassify, resend briefing, diagnostics dump

### REST API Endpoints

The dashboard is powered by 12 REST API endpoints under `/api/climate_advisor/`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Current state overview |
| `/briefing` | GET | Briefing text (supports `?verbosity=` param) |
| `/chart_data` | GET | Temperature chart data |
| `/automation_state` | GET | Automation engine debug state |
| `/learning` | GET | Learning records and suggestions |
| `/config` | GET | All settings with metadata |
| `/force_reclassify` | POST | Trigger reclassification |
| `/send_briefing` | POST | Resend daily briefing |
| `/respond_suggestion` | POST | Accept/dismiss a suggestion |
| `/cancel_override` | POST | Cancel manual override |
| `/resume_from_pause` | POST | Resume from contact sensor pause |
| `/toggle_automation` | POST | Toggle automation on/off |

### Lovelace Card Example

```yaml
type: entities
title: Climate Advisor
entities:
  - entity: sensor.climate_advisor_day_type
    name: Today's Plan
  - entity: sensor.climate_advisor_trend
    name: Trend
  - entity: sensor.climate_advisor_next_action
    name: Your Next Action
  - entity: sensor.climate_advisor_next_automation
    name: Next Automation
  - entity: sensor.climate_advisor_comfort_score
    name: Comfort Score
  - entity: sensor.climate_advisor_contact_status
    name: Doors/Windows
  - entity: sensor.climate_advisor_fan_status
    name: Fan
  - entity: sensor.climate_advisor_occupancy
    name: Occupancy
  - entity: switch.climate_advisor_automation
    name: Automation Enabled
  - entity: sensor.climate_advisor_status
    name: System Status
```

## Development Roadmap

See [Issue #11](https://github.com/gunkl/SmartHVAC/issues/11) for full tracking.

### Phase 1: Core (v0.1.0) — Complete
- [x] 5-level day type classification with trend analysis
- [x] Daily briefing as primary UI (email/notification)
- [x] Door/window pause automation with grace periods
- [x] Occupancy-based setback with configurable delay
- [x] Bedtime/morning scheduling with forecast-aware adjustments
- [x] Runaway protection (runtime alerts, daily budgets)
- [x] Learning engine foundation (90-day rolling window, 6 pattern detectors)
- [x] Config flow wizard, HA sensor entities, dashboard API
- [x] Flexible temperature source configuration
- [x] Separate comfort/setback temps for heat and cool modes

### Phase 2: Enhanced Learning & Adaptation (v0.2.x) — Complete
- [x] Persist operational state across restarts (#10)
- [x] Populate DailyRecord fields (runtime, avg temp, comfort violations, window compliance)
- [x] Per-sensor pause tracking and granular daily records (#12)
- [x] Override direction/timing/magnitude analysis (#12)
- [x] Built-in dashboard panel with status, briefing, classification, learning, settings, and debug tabs
- [x] 12 REST API endpoints powering the dashboard
- [x] 13 sensor entities + 1 automation switch
- [x] Observe-only mode (disable automation without uninstalling) (#19)
- [x] Economizer two-phase cooling strategy (AC cool-down, ventilation maintain) (#27)
- [x] Whole-house fan and HVAC fan mode support (#25)
- [x] Occupancy awareness with home/away/vacation/guest modes
- [x] Briefing TLDR summary table with verbosity control (#24)
- [x] Contact sensor status surfaced in dashboard and as HA entity (#46)
- [x] Resume from pause control with grace expiry re-check (#47)
- [x] Cancel manual override from dashboard
- [x] Reason logging on all thermostat adjustments (#16)
- [x] Repairs flow for missing weather entity
- [x] 4 HA services (respond to suggestion, force reclassify, resend briefing, dump diagnostics)
- [x] 250-char notification limit for short notifications (#21)
- [x] Startup race condition handling for weather entity (#36)

### Phase 2.5: Thermal Learning (v0.2.x) — Remaining
- [ ] Thermal model learning (heat/cool rates, recovery time estimates)
- [ ] Optimized pre-heat/pre-cool timing based on thermal performance
- [ ] Setback depth optimization based on house characteristics
- [ ] Override-driven scheduling suggestions ("You raise temp 2°F at 3pm daily")

### Phase 3: Seasonal & Cost Intelligence (v0.3+) — Future
- [ ] Seasonal performance baselines (after 1 year of data)
- [ ] Anomaly detection (e.g., "heating 30% higher than last November")
- [ ] Energy cost integration (utility rates → estimated cost)
- [ ] Savings tracking vs. "no automation" baseline

### Phase 4: Multi-Zone & Advanced (v0.4+) — Future
- [ ] Multi-zone HVAC support (multiple thermostats)
- [ ] Room-level occupancy detection
- [ ] Humidity-based decisions
- [ ] Energy source cost optimization
- [ ] Advanced thermal model with per-zone coefficients

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test with a Home Assistant dev environment
5. Submit a pull request

## File Structure

```
custom_components/climate_advisor/
├── __init__.py          # Integration setup, service registration
├── manifest.json        # HA integration metadata
├── const.py             # Constants, thresholds, defaults
├── config_flow.py       # Setup wizard UI (7-step flow + options)
├── strings.json         # UI text for config flow
├── translations/
│   └── en.json          # English translations
├── coordinator.py       # Central brain — scheduling, events, data flow
├── classifier.py        # Day type and trend classification
├── briefing.py          # Daily briefing text generation
├── automation.py        # HVAC control logic (incl. economizer, fan)
├── learning.py          # Pattern tracking and suggestion engine
├── sensor.py            # 13 HA sensor entities for dashboards
├── switch.py            # Automation enable/disable switch
├── api.py               # 12 REST API endpoints for dashboard panel
├── state.py             # State persistence across restarts
├── repairs.py           # HA repairs flow for config issues
├── services.yaml        # Service definitions
├── frontend/
│   └── index.html       # Built-in dashboard panel
├── brand/               # Integration branding assets
├── icon.png             # Integration icon
└── icon@2x.png          # Retina integration icon
```

## License

MIT
