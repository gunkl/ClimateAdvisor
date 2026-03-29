# Debugging Guide — Climate Advisor

This guide documents debugging strategies, sensor entities, and tooling for diagnosing Climate Advisor issues.

## Primary Debugging Data Sources

### 1. HA Sensor Entities (Recommended First)

Climate Advisor exposes several sensor entities in Home Assistant. These persist in the Recorder database (default 10 days) and survive container log rotation.

| Sensor | Entity ID | State | Key Attributes | Debugging Value |
|--------|-----------|-------|----------------|-----------------|
| Status | `sensor.climate_advisor_status` | active / paused / grace period / disabled | — | Current automation state |
| Day Type | `sensor.climate_advisor_day_type` | hot / warm / mild / cool / cold | — | Current classification |
| Last Action Reason | `sensor.climate_advisor_last_action_reason` | Truncated reason (250 chars) | `full_reason` | Why last HVAC action was taken |
| Last Action Time | `sensor.climate_advisor_last_action_time` | ISO timestamp | — | When last action occurred |
| Contact Sensors | `sensor.climate_advisor_contact_status` | "all closed" / sensor names | `sensors`, `paused_by_door`, `open_count` | Door/window state and pause status |
| Fan Status | `sensor.climate_advisor_fan_status` | active / inactive / override — on / override — off / disabled | `fan_runtime_minutes`, `fan_override_since`, `fan_running` | Fan automation state |
| Daily Briefing | `sensor.climate_advisor_daily_briefing` | TLDR summary | `full_briefing` | Today's plan |
| Occupancy Mode | `sensor.climate_advisor_occupancy_mode` | home / away / guest / vacation | — | Current occupancy |
| Comfort Score | `sensor.climate_advisor_comfort_score` | 0-100% | `pending_suggestions` | Compliance tracking |
| AI Status | `sensor.climate_advisor_ai_status` | active / inactive / error / disabled / circuit_open | `last_request_time`, `error_count`, `total_requests`, `model_in_use`, `circuit_breaker`, `monthly_cost_estimate`, `auto_requests_today`, `manual_requests_today` | AI integration health and usage |

**How to access:**
- HA UI: Developer Tools → States → filter "climate_advisor"
- HA History: Click any entity → History tab (shows state changes over time)
- CLI: `python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status --hours 24`

### 2. Container Logs (Real-Time)

```bash
# Recent climate_advisor logs
python3 tools/ha_logs.py --lines 100

# Filter for errors only
python3 tools/ha_logs.py --lines 100 --filter "ERROR"

# Save to file for later analysis
python3 tools/ha_logs.py --lines 200 --save
```

**Limitation:** Container logs (`ha core logs`) have limited buffer — typically only a few hours of history. For older data, use the HA REST API method (--history).

### 3. HA REST API History (Historical)

```bash
# Last 24 hours of logbook entries for climate_advisor
python3 tools/ha_logs.py --history --filter climate_advisor

# Status sensor history (state changes over 48 hours)
python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status --hours 48

# Contact sensor history (door/window events)
python3 tools/ha_logs.py --history --entity sensor.climate_advisor_contact_status --hours 24

# Multiple entities
python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status,sensor.climate_advisor_last_action_reason --hours 12
```

**Requires:** `HA_API_TOKEN` in `.deploy.env` (long-lived access token from HA Profile page).

## Common Debugging Scenarios

### "HVAC paused but I opened windows as planned"
1. Check `sensor.climate_advisor_status` — should show "windows open (as planned)" during recommended window period
2. Check `sensor.climate_advisor_day_type` — should be "warm" or "mild"
3. Check `sensor.climate_advisor_last_action_reason` — look for "planned window period" in the reason
4. If status shows "paused — door/window open" during a windows-recommended period, this is Bug #51

### "Got unexpected notifications"
1. Check `sensor.climate_advisor_last_action_reason` for the notification trigger
2. Check grace period status: `sensor.climate_advisor_status` showing "grace period (manual)" or "grace period (automation)"
3. Review container logs: `python3 tools/ha_logs.py --lines 100 --filter "notify\|grace"`

### "HVAC not behaving as expected"
1. Check classification: `sensor.climate_advisor_day_type`
2. Check last action: `sensor.climate_advisor_last_action_reason` (full_reason attribute)
3. Check contact sensors: `sensor.climate_advisor_contact_status` (paused_by_door attribute)
4. Check occupancy: `sensor.climate_advisor_occupancy_mode`
5. Review logs: `python3 tools/ha_logs.py --lines 200`

## Debugging AI Features

### AI Status Sensor

`sensor.climate_advisor_ai_status` is the first place to check when AI features are not responding:

- **`active`** — AI integration is healthy and making successful requests
- **`inactive`** — AI is enabled but no requests have been made yet
- **`error`** — last request failed; check the `error_count` attribute
- **`disabled`** — AI features are turned off in configuration
- **`circuit_open`** — circuit breaker has tripped after 5 consecutive failures; will auto-reset after 5 minutes

### Activity Report Service

The `ai_activity_report` service triggers an on-demand AI analysis of recent automation behavior. This is useful for diagnosing unexpected HVAC decisions — the report includes a timeline, key decisions, anomalies, and diagnostics drawn from current system state.

```bash
# Check report history file directly
python3 tools/ha_logs.py --history --entity sensor.climate_advisor_ai_status --hours 24
```

### AI Report Persistence

AI reports are stored at `climate_advisor_ai_reports.json` in the HA config root directory. The file is capped at 10 reports (`AI_REPORT_HISTORY_CAP`). Request history is capped at 50 entries (`AI_REQUEST_HISTORY_CAP`).

### Circuit Breaker

The circuit breaker trips after **5 consecutive failures** (`AI_CIRCUIT_BREAKER_THRESHOLD = 5`) and enters a cooldown period of **5 minutes** (`AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300`) before attempting requests again. While the circuit is open, all AI requests return immediately without calling the Claude API. The `circuit_breaker` attribute of the AI status sensor shows the current state (`closed` = normal, `open` = tripped).

---

## Diagnostic Logging

Key decision points in automation.py emit debug/info logs:
- `handle_door_window_open()` — logs classification context, planned window period check
- `_grace_expired()` — logs whether sensors are still open, planned window check
- `_re_pause_for_open_sensor()` — logs planned window period suppression
- `_async_door_window_changed()` (coordinator) — logs classification, windows_recommended, planned_window_active

To see these in real-time:
```bash
python3 tools/ha_logs.py --lines 200 --filter "automation\|coordinator"
```
