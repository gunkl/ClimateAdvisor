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
# Recent climate_advisor logs
python3 tools/ha_logs.py --lines 100

# Filter for errors only
python3 tools/ha_logs.py --lines 100 --filter "ERROR"

# Save to file for later analysis
python3 tools/ha_logs.py --lines 200 --save
```

**Limitation:** Container logs (`ha core logs`) have limited buffer — typically only a few hours of history. For older data, use the HA REST API method (--history).

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

## Debugging Thermal Model Learning

### "Thermal model confidence is 'none' after weeks of use"

The v2 architecture (Issue #114) requires the full post-heat decay curve to extract `k_passive`. Observations are silently abandoned if they do not meet minimum quality bars.

**Step 1 — Check for WARNING logs from thermal methods:**
```bash
python3 tools/ha_logs.py --lines 500 --filter "thermal\|k_passive\|k_active"
```

Look for:
- `"Thermal event commit failed: k_passive rejected"` — R² too low; likely sensor noise or very short runs
- `"Thermal observation abandoned"` — timeout (post-heat took > 45 min to stabilize) or insufficient post-heat samples (< 10)
- `"Thermal observation abandoned: active phase restart"` — HVAC mode changed mid-session; the old active phase was discarded

**Step 2 — Check observation history in the learning DB:**
```bash
python3 tools/ha_logs.py --history --entity sensor.climate_advisor_comfort_score --hours 72
```
The `thermal_observation_count` attribute on `sensor.climate_advisor_comfort_score` shows how many observations have been accepted. If it is 0 after multiple heating/cooling days, all observations are being rejected.

**Step 3 — Common root causes:**

| Symptom | Likely cause | What to check |
|---|---|---|
| R² rejection logged repeatedly | Very short HVAC runs (< 10 post-heat samples) | Use the 24h chart view — do runs end quickly? Setpoints may be too close to current temp |
| Stabilization timeout logged | Indoor temp still drifting 45+ min after HVAC stops | House may have poor insulation or large thermal mass; observations still accumulate if R² ≥ 0.2 |
| No WARNING logs at all but count stays 0 | `_start_thermal_event` never called | Check `hvac_action` attribute on the climate entity — thermostat must report `"heating"` or `"cooling"`, not just `hvac_mode` |
| k_passive out of bounds | Value rejected by sanity bounds | Very unusual; log will show the rejected value |

**Step 4 — Verify the pending event is being persisted:**

Check `climate_advisor_learning.json` in the HA config directory for a `pending_thermal_event` key. If it is always `null` even during an active heating run, the state machine is not being entered — confirm `_async_thermostat_changed` is being called by checking thermostat entity history.

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
