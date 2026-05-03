# Debugging Guide — Climate Advisor

This guide documents debugging strategies, sensor entities, and tooling for diagnosing Climate Advisor issues.

## Primary Debugging Data Sources

### 1. HA Sensor Entities (Recommended First)

Climate Advisor exposes several sensor entities in Home Assistant. These persist in the Recorder database (default 10 days).

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

### 2. Temperature Forecast Chart (Visual History)

The dashboard's **Temperature Forecast** chart provides a 1-year visual timeline of HVAC/fan activity alongside temperature data. Use it to diagnose behavior at a glance before diving into logs.

**Range presets**: 6h | 12h | 24h | 3d | 7d | 30d | 1y — select the window that covers the incident

**What each overlay shows:**
| Overlay | What to look for |
|---------|-----------------|
| Red bar (HVAC heating) | Heating fired — check if temp rose as expected |
| Blue bar (HVAC cooling) | Cooling fired — check if temp dropped as expected |
| Green bar (Fan/fan) | Fan-only circulation active |
| Orange solid line (Actual Indoor) | Real indoor temperature response |
| Blue solid line (Actual Outdoor) | Actual outdoor temps driving classification |
| Dashed lines | Predicted curves — divergence from actual reveals model error |
| Target Band shading | Green region = active target zone. The band is dynamic: it narrows to sleep setback overnight, widens to comfort during waking hours, and flattens to setback temperatures when occupancy is away or vacation. Renamed from "Comfort Band" in Issue #119. |
| Event markers | Vertical lines: grey=classification change, green=window recommendation, red=override |

**Drag-to-zoom** on any region for fine-grained analysis. Reset Zoom returns to the preset range.

**Persistent**: data is stored in `climate_advisor_chart_log.json` (1-year rolling) — available even if HA was restarted since the incident.

### 3. Container Logs (Real-Time)


```bash
# Recent climate_advisor logs (default: last 500 matching lines)
python3 tools/ha_logs.py

# Thermal learning diagnosis — filtered to thermal-relevant lines only
python3 tools/ha_logs.py --thermal

# Filter for errors only
python3 tools/ha_logs.py --filter "ERROR"

# Deeper history (Docker log files on HAOS persist to disk — typically days available)
python3 tools/ha_logs.py --lines 5000

# Save to file for later analysis
python3 tools/ha_logs.py --lines 2000 --save
```

**Note:** `ha core logs` reads Docker log files from disk on HAOS. Retention is typically
days (rotated by size, not time). Use `--lines 5000` or `--full` for deeper searches.
The default `--lines 500` covers ~40 minutes of thermal sampling activity.

### 4. HA REST API History (Historical)

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

## Chart Activity Bars

The temperature forecast chart shows four activity bars: HVAC, Fan, Windows Recommended, and Windows Open. These are built from a rolling JSON log at `/config/climate_advisor_chart_log.json` on HAOS.

### Inspecting the raw chart log

```bash
# On HAOS via SSH add-on — view last 20 entries
cat /config/climate_advisor_chart_log.json | python3 -c "
import json, sys
log = json.load(sys.stdin)
for e in log['entries'][-20:]:
    print(e['ts'][:19], 'hvac=', repr(e.get('hvac')), 'fan=', e.get('fan'), 'win_open=', e.get('windows_open'))
"
```

Key field to check: `hvac` should be `"heating"` or `"cooling"` (action strings), never `"heat"` or `"cool"` (mode strings).

### Tracing chart appends via HA logs

Enable debug logging for climate_advisor, then filter:

```bash
python3 tools/ha_logs.py --lines 200 --filter "chart_log append"
```

This shows every chart log write with its event type and resolved `hvac` value.

### Common failure modes

| Symptom | Likely cause | How to confirm |
|---|---|---|
| No red heating segments despite heater running | `hvac_mode` logged instead of `hvac_action` at an event-driven append site | Inspect JSON for `"hvac": "heat"` entries; check HA logs for `chart_log append: event=classification_change hvac='heat'` |
| Heating only visible at 30-min intervals | Event-driven append sites logging mode strings | Same as above |
| Fan bar always green (even when HVAC off) | `_fan_is_running()` detecting untracked fan state | Check for `"running (untracked)"` in `_compute_fan_status()` log lines |
| Windows bars drop on HVAC events | An append site missing `windows_open`/`windows_recommended` | Audit all 4 append call sites in coordinator.py for completeness |
| Heating shown but wrong color (green instead of red) | `hvac_action="fan"` with `fan_mode=auto` not being remapped | Check #109 remap logic in `_read_chart_hvac_action()` |

See `docs/08-COMPUTATION-REFERENCE.md` §19 for the full invariant table governing all four append sites.

---

## Debugging Thermal Model Learning

### "Thermal model confidence is 'none' after weeks of use"

**Step 1 — Check the structured rejection log (primary tool):**
```bash
python3 tools/learning_db.py --rejections
```
This reads `climate_advisor_learning.json` directly via SSH and shows every rejection event
with timestamps, reason codes, elapsed time, R², and delta-T. No HA_URL/HA_TOKEN needed.

**Step 2 — Check current active observations:**
```bash
python3 tools/thermal_health.py   # requires HA_URL + HA_TOKEN in .env or environment
```

**Step 3 — Check thermal log activity:**
```bash
python3 tools/ha_logs.py --thermal          # last 2000 thermal-relevant lines
python3 tools/ha_logs.py --thermal --lines 10000  # deeper history
```

Look for:
- `"keeping alive"` — multi-window accumulator running; observation extending past 30 min
- `"Thermal event commit"` — successful commit with k_passive and R²
- `"abandoned"` / `"max_window_exceeded"` — rejection with reason code

**Step 4 — Common root causes:**

| Symptom | Likely cause | What to check |
|---|---|---|
| All rejections `small_delta` | Integer-°F thermostat; ΔT < 0.2°F in 30 min | Normal — multi-window (Issue #126) accumulates up to 4h; check "keeping alive" logs |
| All rejections `too_few_samples` | Conditions change too fast | Check elapsed_minutes in rejection log; may need longer stable windows |
| R² rejection logged repeatedly | Short HVAC runs or sensor noise | Use 24h chart view; check run lengths |
| Rejections show `abandoned`, elapsed < 5 min | Condition-change abort (window closed, HVAC started) | Normal if window briefly closed; look for restart immediately after |
| No rejections AND count stays 0 | Observation never started | Check `hvac_action` / window sensor state; thermal trigger eval logs |

**Step 5 — Check the full learning DB:**
```bash
python3 tools/learning_db.py
```
Shows model summary, all committed observations, and rejection log in one report.

### "Predicted temperature curve looks wrong"

The physics path activates when `confidence != "none"` and `k_passive < 0`. Before that threshold is reached, the legacy ramp interpolation runs.

```bash
python3 tools/ha_logs.py --lines 100 --filter "using physics model\|k_passive"
```

A `DEBUG` log line is emitted inside `_build_predicted_indoor_future()` when the physics path is taken: `"_build_predicted_indoor_future: using physics model (conf=... k_passive=... k_active_heat=... k_active_cool=...)"`. If this line does not appear, the function fell back to ramp interpolation (model not ready or `k_passive` not yet negative).

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
