<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Debugging Guide — Climate Advisor

This guide documents debugging strategies, sensor entities, and tooling for diagnosing Climate Advisor issues.

## Anchors
| Question | Short answer | → Full answer |
|---|---|---|
| What is the recommended first debugging step for any unexpected HVAC behavior? | Check the four key sensor entities in order: `sensor.climate_advisor_day_type` (classification), `sensor.climate_advisor_last_action_reason` (why), `sensor.climate_advisor_contact_status` (door/window pause), `sensor.climate_advisor_occupancy_mode`. | [§Common Debugging Scenarios](09-DEBUGGING-GUIDE.md#common-debugging-scenarios) |
| How do you pull Climate Advisor logs and filter for thermal activity? | `python3 tools/ha_logs.py --thermal` for last 2000 thermal-relevant lines; `--lines 5000` for deeper history. Docker log files on HAOS persist for days — do not assume rotation without checking. | [§3. Container Logs (Real-Time)](09-DEBUGGING-GUIDE.md#3-container-logs-real-time) |
| What is the step-by-step diagnostic sequence for "thermal model confidence is none"? | 1. `python3 tools/learning_db.py --rejections` (structured rejection log, no token needed). 2. `python3 tools/learning_db.py --pending` (in-flight observations). 3. `python3 tools/thermal_health.py` (active observations, needs HA_TOKEN). 4. `python3 tools/ha_logs.py --thermal`. | [§Debugging Thermal Model Learning](09-DEBUGGING-GUIDE.md#debugging-thermal-model-learning) |
| How do you diagnose `k_active_cool=None` on a home where AC has been running all season? | Run `--rejections --type hvac_cool` to check n values and reason codes. `n=0` with elapsed > 0 on coordinator < v0.3.50 indicates the key-shadow bug (fixed in v0.3.50). `new_session_started` repeatedly means short-cycling. Run `--pending` during a live AC cycle to confirm samples are accumulating. | [§"k_active_cool is None despite AC running all summer"](09-DEBUGGING-GUIDE.md#k_active_cool-is-none-despite-ac-running-all-summer-hvac-observation-debugging) |
| What does the Temperature Forecast chart show and how do you use it for diagnosis? | Four activity bars (HVAC, Fan, Windows Recommended, Windows Open) plus indoor/outdoor temperature lines, predicted curves, and target band shading. Drag-to-zoom any region; 1-year data in chart_log survives HA restarts. Use Prev/Next buttons to scroll the data window to any past point in time (Issue #160). | [§2. Temperature Forecast Chart (Visual History)](09-DEBUGGING-GUIDE.md#2-temperature-forecast-chart-visual-history) |
| Why does the Predicted Indoor line track Actual Indoor exactly (delta ≈ 0)? | Check log source tag: `(archive)` = working correctly; `(ode-warmup)` = HA restart < 4h ago (auto-resolves); `(none)` = ODE cache empty (check thermal model confidence). Five root causes documented. | [§"Predicted Indoor tracks Actual Indoor"](09-DEBUGGING-GUIDE.md#predicted-indoor-tracks-actual-indoor-delta--0) |
| How do you diagnose AI feature failures? | Check `sensor.climate_advisor_ai_status` first: active/inactive/error/disabled/circuit_open. Circuit breaker trips after 5 consecutive failures, auto-resets after 5 minutes. `monthly_cost_estimate` attribute tracks spending. | [§Debugging AI Features](09-DEBUGGING-GUIDE.md#debugging-ai-features) |
| How do you decide if a finding in an AI investigator report is a real bug or noise? | Apply the 5-category taxonomy: ACTIONABLE / TIME-DEPENDENT / CONTEXTUAL / NOISE / RESOLVED. Count discrepancies ≤ 1, high abandonment from operational interruptions, and pending-observation speculation are all NOISE. | [§Interpreting AI Investigator Reports — Noise Taxonomy](09-DEBUGGING-GUIDE.md#interpreting-ai-investigator-reports--noise-taxonomy) |
| How do you diagnose a user-reported "CA keeps overriding my thermostat changes"? | Check the Override Bypass Inventory: 6 known bypasses covering setpoint-only changes (#197), confirm state lost on restart (RC-1 #198), grace timer not restored (RC-2 #199), PATH B short overrides (#200), second override ignored (#201), and 30s guard too wide (#202). | [§Override Bypass Inventory](09-DEBUGGING-GUIDE.md#override-bypass-inventory) |

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

**Historical navigation** (Issue #160): use the Prev/Next buttons to scroll the data window backward and forward in time. The frontend passes `before_ts` (Unix ms) to the `/api/climate_advisor/chart_data` endpoint; the API shifts the entire fetch window to that anchor point. The current range preset (e.g. `24h`) stays in effect — only the window endpoint changes. "Live" mode resumes when you navigate back to the present.

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

## Interpreting AI Investigator Reports — Noise Taxonomy

Not all findings in a CA AI investigator report represent real bugs or user-actionable issues.
Use this taxonomy to classify findings before acting on them:

| Category | Definition | Example |
|---|---|---|
| **ACTIONABLE** | Real issue requiring investigation or fix | `hvac_runtime_today = 0.0` mid-day (counter reset by restart) |
| **TIME-DEPENDENT** | Cannot classify without more data over time | `solar_gain: 0 commits` on active-HVAC hot days — check after quiet days |
| **CONTEXTUAL** | Technically real but fully explained by operational conditions | `ventilated_decay: 32 abandoned` (normal contact-open events) |
| **NOISE** | Implementation detail user should never see | `observation_count_cool` off by 1 (flush lag) |
| **RESOLVED** | Covered by a KNOWN_FIXES entry in current version | Off-by-one in HVAC observation count → Issue #156 [COVERED] |

**Common noise patterns:**

- **Count discrepancies ≤ 1** between thermal model cache and rejection log: NOISE (transient EWMA flush lag, not a bug)
- **High abandonment rates** where top reason is `hvac_started` or `sensor_opened`: NOISE if committed count > 0 (operational interruptions are expected)
- **Pending / in-flight observation speculation** (e.g., "this observation has 0 samples at 4.4 minutes — may fail"): NOISE (unknown outcome; pending obs belong in the activity report, not the investigator)
- **chart_log endpoint count 1–5** when block-OLS count > 0: NOISE (not at the hard "both = 0" threshold)
- **96%+ abandonment on hot days with active HVAC**: NOISE — `passive_decay` is constantly interrupted by HVAC cycles on days when the system is actively heating or cooling; the committed count is what matters

**Classifying a finding step-by-step:**

1. Is it covered by a `KNOWN_FIXES` entry at the current CA version? → **RESOLVED**
2. Is it a count discrepancy ≤ 1 or a flush-lag artifact? → **NOISE**
3. Is it speculation about a pending (in-flight) observation? → **NOISE**
4. Is the abandonment rate driven entirely by operational interruptions (`hvac_started`, `sensor_opened`, `fan_activated`) with committed count > 0? → **NOISE**
5. Is the anomaly only visible on one class of day (hot, active-HVAC) but cannot be confirmed without quiet-day data? → **TIME-DEPENDENT**
6. Is the anomaly technically present but fully explained by the current operating context? → **CONTEXTUAL**
7. Everything else → **ACTIONABLE** — investigate with logs and DB tools before forming a hypothesis

Use `/investigate-ca-report <issue-url>` to run systematic triage via the dedicated skill.

**Reference:** `docs/09-DEBUGGING-GUIDE.md` — this section. Noise taxonomy is also documented in the `investigate-ca-report` skill at `.claude/skills/investigate-ca-report.md`.

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
python3 tools/learning_db.py --rejections --type hvac_cool   # filter by obs type
```
This reads `climate_advisor_learning.json` directly via SSH and shows every rejection event
with timestamps, reason codes, elapsed time, R², and delta-T. A summary of top rejection
reasons is shown at the bottom. No HA_URL/HA_TOKEN needed.

**Step 2 — Check in-flight observations:**
```bash
python3 tools/learning_db.py --pending
```
Shows any observations currently active in the pipeline: type, phase, elapsed time, sample
count, and peak indoor temperature. Run during a live HVAC cycle to confirm samples are
accumulating.

**Step 3 — Check current active observations (live system):**
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

### "k_active_cool is None despite AC running all summer" (HVAC observation debugging)

Use this workflow when `--model` shows `k_active_cool=None` or `k_active_heat=None` and
the HVAC has definitely been running.

```bash
# 1. Confirm zero committed observations for the HVAC type
python3 tools/learning_db.py --committed          # look for hvac_cool/hvac_heat rows

# 2. Check rejection log filtered to the HVAC type
python3 tools/learning_db.py --rejections --type hvac_cool

# 3. Check for in-flight observations during a live cycle
python3 tools/learning_db.py --pending

# 4. Cross-check model state
python3 tools/learning_db.py --model
```

**Interpreting rejection log results:**

| `--rejections --type hvac_cool` shows | Likely cause | Action |
|---|---|---|
| `n=0` entries with `elapsed > 0 min` on coordinator < v0.3.50 | Key-shadow bug: `"samples": []` shadowed `active_samples` | Upgrade to v0.3.50 — all HVAC obs discarded before fix |
| `new_session_started` repeated many times | Short-cycling thermostat: post-heat window interrupted by next HVAC start | Event-driven sampling (v0.3.50+) improves this; single-point fallback may commit |
| `plateau_guard: insufficient post-heat decay` repeated | Indoor temp didn't drop ≥ 0.3°F in post-heat phase | Efficient/short-cycle system; normal if occasional |
| `n=0, delta_t=0.00°F` on coordinator ≥ v0.3.50 | Sensor quantization: 1°F thermostat can't see 0.3–0.8°F HVAC effect | Single-point fallback kicks in if `T_start` vs `T_peak` delta ≥ `THERMAL_HVAC_MIN_SIGNAL_F (0.5°F)` |
| `--pending` shows obs with samples but `--committed` = 0 | Observation accumulated but commit path failed (OLS returning None) | Check `--model` for `k_passive` — if absent, bridge proxy not available; commit may be deferred |

**Step 5 — Check in-flight observations (v0.3.50+):**
```bash
python3 tools/learning_db.py --pending
```
Shows any observations currently in `pending_observations` — type, phase (`active`/`post_heat`), elapsed time, sample counts, and peak indoor temperature. Use this to confirm an observation is accumulating samples during a live HVAC cycle.

**Step 6 — Check the full learning DB:**
```bash
python3 tools/learning_db.py
```
Shows model summary, all committed observations, and rejection log in one report.

**Step 7 — Check nightly setback history (v0.3.48+):**
```bash
python3 tools/learning_db.py --daily        # last 30 nights
python3 tools/learning_db.py --daily 60     # last 60 nights
```
Prints one row per night: date, day type, HVAC mode, applied setpoint (°F), setback depth (°F), adaptive flag, and skip reason. Use this to diagnose whether `handle_bedtime()` has been firing, applying static or adaptive depth, or silently skipping on warm/mild nights (`hvac_off`) or away nights (`occupancy`). Skip nights show `setback_skipped_reason` in the last column; fire nights show `depth_f` and `adaptive=True/False`.

### "Predicted temperature curve looks wrong"

The physics path activates when `confidence != "none"` and `k_passive < 0`. Before that threshold is reached, the legacy ramp interpolation runs.

```bash
python3 tools/ha_logs.py --lines 100 --filter "using physics model\|k_passive"
```

A `DEBUG` log line is emitted inside `_build_predicted_indoor_future()` when the physics path is taken: `"_build_predicted_indoor_future: using physics model (conf=... k_passive=... k_active_heat=... k_active_cool=...)"`. If this line does not appear, the function fell back to ramp interpolation (model not ready or `k_passive` not yet negative).

### "Predicted Indoor tracks Actual Indoor (delta ≈ 0)"

**Symptom**: The "Predicted Indoor" line on the Temperature Forecast chart follows the "Actual Indoor" line exactly, or nearly so. `chart_log` entries show `pred_indoor ≈ indoor`.

**Step 1 — Check the archive source tag (highest priority):**
```bash
python3 tools/ha_logs.py --filter "chart_log pred_indoor"
```
Look for `(archive)`, `(ode-warmup)`, or `(none)` in the log output. This tells you immediately which code path populated `pred_indoor`.

| Tag | Meaning | Action |
|---|---|---|
| `(archive)` | First-write-wins archive is working | If delta is still ≈ 0, see Root cause 3 below |
| `(ode-warmup)` | HA restarted < 4h ago; archive warming up | Expected; auto-resolves within 4h |
| `(none)` | Archive and ODE cache both empty | Check thermal model confidence (Step 2) |

**Step 2 — Check thermal model confidence (when tag is `(none)`):**
```bash
python3 tools/ha_logs.py --filter "thermal model refreshed"
```

**Decision tree — five root causes:**

1. **`(ode-warmup)` in log** — HA restarted < 4h ago; archive is in warmup period. The `_last_predicted_indoor[0]` fallback is used until archive entries are populated 4h ahead. Auto-resolves within 4h. Not a bug.

2. **`(none)` in log — ODE cache empty** — `_last_predicted_indoor` was empty at tick time and archive had no entry for this slot. Root cause: thermal model empty, physics gate falsy (`k_passive` absent), or indoor temp unavailable. Since Issue #137, the thermal model is refreshed every 30-min cycle; this state auto-resolves within 30 min after restart for homes with existing observations. Verify with `--filter "thermal model refreshed"` → should show `confidence=solid` or `confidence=moderate` once resolved.

3. **`(archive)` but delta ≈ 0 during temperature transitions** — outdoor forecast accuracy issue. The advance prediction (made ~4h earlier) matched the actual trajectory because outdoor conditions tracked the forecast closely. Rare during pronounced heating/cooling cycles. Not a bug.

4. **Fresh install (no observations)** — `confidence=none` in refresh log. No thermal data has been collected yet. Fix: wait 1–2 days for `k_passive` observations to accumulate.

5. **Archive populated but `pred_indoor = indoor` exactly** — potential bug. Check that `_pred_archive_key()` is rounding to the correct 30-min boundary; check that archive epoch keys match the chart_log entry timestamps.

**Updated log filter commands:**
```bash
python3 tools/ha_logs.py --filter "chart_log pred_indoor"    # check (archive) vs (ode-warmup) vs (none)
python3 tools/ha_logs.py --filter "thermal model refreshed"  # check confidence after restart
```

See [Chart Log Spec — Regression Decision Tree](chart-log-spec.md#regression-decision-tree) for the full diagnostic tree and [First-Write-Wins Prediction Archive](chart-log-spec.md#first-write-wins-prediction-archive) for the archive architecture.

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

---

## Override Bypass Inventory

This section documents known ways a user-initiated thermostat change can be silently ignored or reversed by CA. Use it when a user reports that manual changes to the thermostat are not being respected.

### Quick triage checklist

1. Check `sensor.climate_advisor_last_action_reason` — was CA the last actor?
2. Check HA logs for `handle_manual_override` — was the override detected at all?
3. Check if the user changed mode, setpoint, or both — the detection paths differ.
4. Check if CA restarted (look for `Climate Advisor reloaded` in logs) within the override window.

### Bypass inventory

| # | Name | Scope | Status | Issue |
|---|------|-------|--------|-------|
| 1 | Setpoint-only change not treated as override | `coordinator.py:2382–2406` — setpoint change block never calls `handle_manual_override()` | Confirmed bug | [#197](https://github.com/gunkl/ClimateAdvisor/issues/197) |
| RC-1 | Override confirm state lost on restart | `automation.py restore_state()` never restores `override_confirm_pending` / `override_confirm_time` — 10-min gate is lost | Confirmed bug | [#198](https://github.com/gunkl/ClimateAdvisor/issues/198) |
| RC-2 | Grace timer not restored on restart | `automation.py:2303–2304` explicitly discards grace timer — `_manual_override_active` stays True indefinitely | Confirmed bug | [#199](https://github.com/gunkl/ClimateAdvisor/issues/199) |
| PATH-B | Self-resolve discards short deliberate overrides | `automation.py:630–644` PATH B treats quick mode revert as transient — no grace granted | Design gap | [#200](https://github.com/gunkl/ClimateAdvisor/issues/200) |
| 2nd | Second override during active override silently ignored | `coordinator.py:2172` gates override detection on `not _manual_override_active` | Design gap | [#201](https://github.com/gunkl/ClimateAdvisor/issues/201) |
| 30s | 30-second guard too wide for setpoint changes | `coordinator.py:2387` uses 30s vs 3s for mode changes — user changes within 30s of CA command are dropped | Design gap | [#202](https://github.com/gunkl/ClimateAdvisor/issues/202) |

### Bypass #1 — Setpoint-only change (Issue #197)

**Symptom**: User raises setpoint (e.g. 72→76°F) with no mode change; CA re-applies 72°F at the next 30-min cycle.

**Root cause**: `_async_thermostat_changed` in coordinator.py only increments the daily override counter for setpoint changes — it never calls `handle_manual_override()`. The mode-change path calls `handle_manual_override()`, but the setpoint-change path does not.

**Diagnostic log pattern** (absence = bypass is active):
```
# You should NOT see this for a setpoint-only change until the fix lands:
handle_manual_override called source=setpoint
```

**Fix scope**: Call `handle_manual_override(source="setpoint")` from the setpoint-change block; adjust `_confirm_override_expired` PATH B to treat `source="setpoint"` as always confirmed (mode equality does not imply setpoint agreement).

### RC-1 — Override confirm state lost on restart (Issue #198)

**Symptom**: CA restarts while in the 10-minute override confirmation window; override is cleared immediately after restart instead of holding for the remaining window.

**Root cause**: `restore_state()` in automation.py (lines 2280–2315) does not restore `_override_confirm_pending`, `_override_confirm_time`, or `_override_confirm_mode`. These fields are serialized by `get_serializable_state()` but silently dropped on restore.

**Diagnostic**: Correlate a CA reload event in logs with a sudden `_manual_override_active → False` transition within minutes of an override being set.

### RC-2 — Grace timer not restored on restart (Issue #199)

**Symptom**: After a CA restart, the manual override flag remains active indefinitely — CA never re-applies its schedule.

**Root cause**: `automation.py:2303–2304` contains an explicit comment: "Grace timers cannot be restored — clear on restart." `_manual_override_active=True` is restored (line 2286) but the `_grace_expired` timer is not rescheduled. The flag stays set until the `clear_manual_override` service is called.

**Diagnostic**: `_manual_override_active=True` persists in `sensor.climate_advisor_status` attributes for more than 4 hours after a CA restart with no user interaction.
