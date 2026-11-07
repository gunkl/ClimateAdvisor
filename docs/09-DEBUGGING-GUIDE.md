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
| How do you diagnose a user-reported "CA keeps overriding my thermostat changes"? | Check the Override Bypass Inventory: 9 known bypasses covering setpoint-only changes (#197), confirm state lost on restart (RC-1 #198), grace timer not restored (RC-2 #199), PATH B short overrides (#200), second override ignored (#201), 30s guard too wide (#202), bedtime/wakeup unconditional clear (BW #204 — fixed), away setback mode mismatch (OA #222 — fixed), and away setback detected as override (OB #221 — fixed). | [§Override Bypass Inventory](09-DEBUGGING-GUIDE.md#override-bypass-inventory) |
| Forecast Low/High or day trend looks absurd (e.g. outdoor -8 °C on a mild day) — what do I check? | Grep logs for `Weather unit mismatch`, `Temperature unit mismatch with Home Assistant`, `Temperature sensor unit mismatch`, `Temperature unit changed since last run`, and `Observed outdoor extreme overrides forecast`; check the diagnostics `chart_data_summary` outdoor_min/max and the `unit_changed` event. | [§"Forecast Low/High looks wrong"](09-DEBUGGING-GUIDE.md#forecast-lowhigh-looks-wrong-unit-mismatch--unit-change-issue-1015) |
| The house is cold/hot and the Activity Log says the band was applied -- how do I tell whether the thermostat is ignoring the mode (Sensi/ecobee)? | Download diagnostics and read `thermostat_snapshot.last_command` (`live_state` vs `mode`, `live_state_matches_commanded`, `read_back_skip_reason`, the `mode_mismatch_*` and `command_failure_*` fields), `thermostat_snapshot.climate_entity.capabilities`, and `recent_hvac_events` (`hvac_mode_not_applied`, `hvac_command_failed`). Issue #1049. | [Reading the thermostat diagnostics blocks](#reading-the-thermostat-diagnostics-blocks-and-confirming-an-ignored-mode-issue-1049) |
| How well is the comfort-band arming / HVAC cycling working in a home, and is model-based deferral worth building? | Read `control_quality` in Download Logs / Download Full Logs / HA Download diagnostics (`thermostat_snapshot.control_quality`): misarmed minutes should be near zero; `would_have_deferred` vs total run minutes sizes the deferral opportunity; `prediction_error` is the accuracy gate. Issue #1066. | [Control quality report](#control-quality-report-issue-1066) |
| The house has been cold/hot for hours and the equipment never ran -- where do I look? | The stall alert (Issue #1050): an `hvac_output_stall` Activity Log row, a push (no email), and an `output_stall` line in Status `hvac_control_issues`. | [Debugging a house-off-target stall alert](#debugging-a-house-off-target-stall-alert-issue-1050) |
| A whole-house-fan switch ignores CA's on-command and the AC stays suppressed -- how do I confirm it? | `fan_cancel` with `readback: true` in the Activity Log, then a single WARNING `Fan command ignored twice`. | [Debugging a fan switch that ignores the on-command](#debugging-a-fan-switch-that-ignores-the-on-command-issue-1050) |

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
| Daily Briefing | `sensor.climate_advisor_daily_briefing` | TLDR summary, truncated (250 chars) only if it exceeds HA's 255-char limit (Issue #555) | `full_briefing`, `tldr` (untruncated) | Today's plan |
| Occupancy Mode | `sensor.climate_advisor_occupancy_mode` | home / away / guest / vacation | — | Current occupancy |
| Comfort Score | `sensor.climate_advisor_comfort_score` | 0-100% | `pending_suggestions` | Compliance tracking |
| AI Status | `sensor.climate_advisor_ai_status` | active / inactive / error / disabled / circuit_open | `last_request_time`, `error_count`, `total_requests`, `model_in_use`, `circuit_breaker`, `monthly_cost_estimate`, `auto_requests_today`, `manual_requests_today` | AI integration health and usage |

**How to access:**
- HA UI: Developer Tools → States → filter "climate_advisor"
- HA History: Click any entity → History tab (shows state changes over time)
- CLI: `python3 tools/ha_logs.py --history --entity sensor.climate_advisor_status --hours 24` (`--hours` above 24 works since Issue #1233; before it, HA returned only the oldest 24 h because no `end_time` was sent)

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
1. Check `sensor.climate_advisor_day_type` — should be "warm" or "mild"
2. Check whether windows were actually recommended today: the briefing header says "(windows day)" and the Windows row shows "Close by …" only when outside air helps (Issue #1211: mild days are no longer always windows days; look for `windows_recommended` in the Debug tab or the `Regenerating briefing text — windows_recommended flipped` log line)
3. Since Issue #1211 a window opened past the debounce inside the planned period is NOT exempt: the handler starts nat-vent when the gate passes (fan configured, outdoor cooler than indoor, indoor above the floor, hvac_fan not above the ceiling) and **pauses** when it refuses. A pause there is expected when no fan is configured or the gate fails — check the log line "Nat vent not started … primary gates failed"
4. If a window stayed open past the end of the planned period with the HVAC still running, look for "Planned window period ended — reconciling door/window pause with live sensors" (the exit-edge reconcile, also run when the live outdoor gate clears the recommendation)

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

### "Forecast Low/High looks wrong" (unit mismatch / unit change, Issue #1015)

Occupant symptom: Forecast Low/High, the day trend and the bedtime-setback text are wrong (a Celsius home shows an outdoor low around -8 °C on a mild day), and the briefing contradicts itself.

```bash
python3 tools/ha_logs.py --lines 3000 --filter "unit\|Observed outdoor extreme"
```

| Log line / signal | Meaning | Action |
|---|---|---|
| `Weather unit mismatch: provider_unit=… configured_unit=…` (WARNING, once per run) | The weather entity's `temperature_unit` disagrees with Climate Advisor's configured unit. CA never reads the provider's unit, so readings are misinterpreted. Debug state shows `unit_mismatch: true` plus `provider_unit`/`configured_unit`. | Set the Climate Advisor temperature unit to match the provider (options flow). Wrong samples already in the same-day history are discarded by the reload that applies the change. |
| `Climate entity <id> reports temperature_unit=<raw> but Climate Advisor is configured for <configured_unit> — setpoints written by CA will be wrong until this is resolved` (ERROR, once per run, Issue #968/#1018) | The thermostat entity exposes a `temperature_unit` attribute that differs from the configured unit (unrecognised values also count). Silent when the attribute is absent (normal for HA core thermostats). Log-only, no auto-correction. See [temperature-conversion.md](temperature-conversion.md#climate-entity-unit-check-issue-968-issue-1018). | Check which unit the thermostat integration really reports its temperatures in. Home Assistant converts climate temperatures to its own unit system, so the attribute may only be the device's native unit; change the Climate Advisor unit only if CA's displayed/commanded temperatures are actually wrong. |
| `Temperature unit mismatch with Home Assistant: ha_unit=<unit> configured_unit=<unit> — thermostat readings and setpoints will be misread until corrected` (WARNING, once per run, Issue #1021) | Climate Advisor's configured unit differs from Home Assistant's system unit. HA converts climate temperatures to its system unit on state attributes and on `climate.set_temperature`, while CA uses its own unit, so every thermostat read and setpoint write is off by the C/F factor. Log-only. Debug state: `ha_system_unit`, `ha_unit_mismatch`. See [temperature-conversion.md](temperature-conversion.md#home-assistant-unit-system-and-sensor-unit-checks-issue-1020-issue-1021). | Check which unit HA really reports (Settings, System, General, Unit system) and set the Climate Advisor unit to match HA's system unit. New installs now default to HA's unit (0.7.86), so on a fresh install a mismatch usually points to a manual override of the pre-selected unit. |
| `Temperature sensor unit mismatch: role=<role> sensor_unit=<unit> configured_unit=<unit> — readings from this sensor will be misread until corrected` (WARNING, once per role per run, Issue #1020) | A configured temperature sensor (`outdoor_temp`, `indoor_temp` or `sleep_indoor_temp`) reports a `unit_of_measurement` that differs from the configured unit, so its readings are misinterpreted. Silent when the sensor has no or an unrecognised unit. Log-only; the role is logged, not the entity id. Debug state: `sensor_unit_mismatches` (role to sensor unit, `{}` when none). | Check which unit that sensor really reports (entity attributes) and set the Climate Advisor unit to match HA's system unit, or fix/convert the sensor so it matches. |
| `Outdoor sensor fallback: reason=<r> entity=<id> using=<weather_interpolation or weather_attribute> value_f=<x.x>` (INFO, once per episode, Issue #1032) | The dedicated outdoor sensor failed and the weather service value is being used. `reason` is one of `not_configured`, `entity_missing`, `unavailable`, `non_numeric`, `non_finite`, `implausible`, `stale`. Not emitted during the startup-coalesce window. Mirrored by an `outdoor_sensor_fallback` Activity Log row and a suffix on the Settings tab's "Outdoor Temp Source" value. | Check the entity in Developer Tools → States. `stale` means no report for 6 h (threshold `OUTDOOR_SENSOR_STALE_HOURS`, uncalibrated). |
| `Outdoor sensor recovered: entity=<id> previous_reason=<r> minutes_in_fallback=<n> value_f=<x.x>` (INFO, Issue #1032) | The sensor reads as a good value again; paired with an `outdoor_sensor_recovered` Activity Log row. | None. |
| `Indoor sensor unavailable: role=<primary or sleep> reason=<r> entity=<id> using=<…>` (WARNING for `primary`, INFO for `sleep`; once per episode per zone, Issue #1033) | The dedicated indoor sensor (or the sleep-window bedroom sensor) failed. For `primary` there is no fallback: indoor-based decisions pause (`using=none`) until it reports again; for `sleep` the primary indoor sensor is used instead. `reason` is one of `not_configured`, `entity_missing`, `unavailable`, `no_reading` (climate entity without `current_temperature`), `non_numeric`, `non_finite`, `implausible`, `stale`. Not emitted during the startup-coalesce window; the sleep role is only evaluated inside the sleep window. Mirrored by an `indoor_sensor_unavailable` Activity Log row and a suffix on the Settings tab's "Indoor Temp Source" value. | Check the entity in Developer Tools → States. `stale` means no report for 12 h (`INDOOR_SENSOR_STALE_HOURS`, uncalibrated; `sensor` source and the sleep sensor only, not `input_number`). |
| `Indoor sensor recovered: role=<…> entity=<id> previous_reason=<r> minutes_down=<n>` (INFO, Issue #1033) | The sensor reads as a good value again; paired with an `indoor_sensor_recovered` Activity Log row. | None. |
| `Indoor temp entity … unusable: reason=…` / `Indoor temp from climate entity … unusable: reason=…` / `Sleep indoor sensor … unusable: reason=…` / `Indoor temp … outside plausible range` (Issue #1033: DEBUG) | Per-read diagnostics from `indoor_temp.py`. Before #1033 the non-numeric and out-of-range lines were WARNINGs repeated on every read (~45 per cycle during an outage), a missing entity was silent, and the sleep sensor logged `Sleep sensor unavailable (n/2)` / `… available again after N misses` from a process-global streak counter; those streak lines no longer exist. The failure is announced once per episode by the `Indoor sensor unavailable` line above. | Set the `custom_components.climate_advisor.indoor_temp` logger to DEBUG to see per-read detail. |
| `Temperature entity configured but not in use: role=<outdoor or indoor> source=<s> entity=<id>` (WARNING, once per setup, Issue #1032) | A saved sensor entity sits next to a default source (weather service / climate entity), so it is ignored. Possible only for configs saved before #1032; the Options flow now reconciles new saves. | Set the source to Dedicated sensor / Input helper (the Options flow now switches it automatically when the entity is picked), or clear the entity. |
| `Temperature source reconciled: field=<key> rule=<rule> source=<s> entity_set=<bool>` / `Temperature source rejected: field=<key> rule=<rule>` (INFO, Issue #1032) | The Options flow adjusted or rejected a source/entity combination (`reconcile_temperature_source()` in `config_flow.py`). Rules: `entity_promotes_source`, `entity_cleared_reverts_source`, `source_default_drops_entity`, `entity_domain_sets_source`; errors `entity_required_for_source`, `entity_domain_mismatch`, `source_entity_conflict`. | None — explains why the saved source differs from the one selected. |
| `Temperature unit changed since last run: same-day readings discarded` (WARNING, at restore, any day since Issue #1360) | The saved state's `temp_unit` differs from the configured unit; the persisted pending thermal observations, kept forecast and `pred_archive` were dropped (plus temp history and `classification` on a same-day restore). Full text: `Temperature unit changed since last run: same-day readings discarded from=<saved_unit> to=<current_unit> dropped_outdoor=<n> dropped_indoor=<n> dropped_pred_archive=<n> dropped_classification=<True|False> dropped_pending_observations=<n>`. Expected after the user changes the unit. | None; forecast values are correct from the first cycle after the reload. Only an unexpected occurrence needs investigation (who changed the unit?). |
| `unit_changed` event (persisted event log / Activity Report / diagnostics) | The structured twin of the line above. Payload: `from`, `to`, `dropped_outdoor`, `dropped_indoor`, `dropped_pred_archive`, `dropped_classification`, `dropped_pending_observations`. | Correlate its timestamp with the first wrong value. |
| `Observed outdoor extreme overrides forecast: kind=<low\|high> observed_ts=<ts> observed_value=<x.x> provider_value=<x.x> delta_f=<x.x>` (WARNING) | Observed history overrode the provider's high/low by more than 20°F (`_OBSERVED_EXTREME_DELTA_F`, values internal °F). Emitted once per distinct (kind, observed_ts) per day (latch cleared at end of day); returned values are unchanged. A legitimate large day/night swing can trigger it once per new extreme sample. | A sample taken in the wrong unit (check for a unit change or mismatch near that ts) or a bad sensor reading. |

**Diagnostics:** the `chart_data_summary` block now includes `outdoor_min`, `outdoor_max`, `outdoor_oldest_ts` and `outdoor_newest_ts` next to the existing point counts, (internal °F, regardless of the configured display unit), so one diagnostics download shows a poisoned same-day sample directly (an `outdoor_min` far below the provider's low is the signature).

**What to expect after changing the unit:** the options flow only writes the new unit and raises a Repairs "reload needed" issue; the unit applies after the Repairs "Fix", a manual reload or an HA restart. Since 0.7.83 that restore discards the same-day readings (since Issue #1360 the unit check also runs when the restart comes back on a later day, dropping pending observations, kept forecast and `pred_archive`), so Forecast Low/High and the day trend recover straight away (before it, wrong values persisted until the 23:59 history clear). An already-written morning briefing may keep its old wording until the next one. Avoid switching units just to test. **Not repaired:** chart-log history and learning/thermal records written while the unit was wrong stay as recorded; chart entries fade as they age out. A unit change on the same restart as the first upgrade to 0.7.83 is not detected (the older state file has no `temp_unit`). See [temperature-conversion.md](temperature-conversion.md#provider-unit-must-equal-the-configured-unit-issue-1015) and [state-persistence.md](state-persistence.md#temp_unit-key-and-restore-rule-issue-1015).

## Debugging a Fan Running Without an Indoor Temperature (Issue #1037)

**Symptom:** the house overcooled overnight with a ventilation fan running, or you received "Climate Advisor: fan running without a temperature reading" / "ventilation fan turned off" / "indoor temperature is back" pushes.

1. Activity Log: `fan_blind_started` (session, will_stop, stop_at) -> `fan_blind_stopped` (minutes_blind) -> `fan_blind_recovered` (minutes_blind, stopped). Next to them, the #1033 `indoor_sensor_unavailable` / `indoor_sensor_recovered` rows give the *reason* (stale, unavailable, no_reading, ...) - the engine does not know it.
2. Logs (`python tools/ha_logs.py --lines 3000`): WARNING `Fan running blind: ...`, DEBUG `Fan still blind: ... remaining_s=`, WARNING `Fan stopped: indoor blind timeout ...`, WARNING `Fan stop deferred: ... retry=next_tick` (Issue #641 rate limiter), INFO `Fan blind episode ended: recovered ...`. The real stop is 30-35 min after the first blind line (5-minute tick).
3. Status tab: the Fan (WHF)/(HVAC) card shows `(indoor sensor offline — stops HH:MM)` or `(— no auto-stop)` while an alerted episode is live.
4. No alert at all? The backstop only runs for CA-activated fans. A manual/RF-timer fan with no CA session, and the min-runtime cycle, are not covered by design; a *frozen but numeric* indoor value is not a blind episode (tracked in #1043).

## Reading the thermostat diagnostics blocks and confirming an ignored mode (Issue #1049)

**Symptom:** the house is cold (or hot) while Climate Advisor reports a heat/cool setpoint, or the Status tab shows "thermostat is not following the requested mode". Cause class (Issue #1048): Sensi and HA-core ecobee ignore the `hvac_mode` kwarg of `climate.set_temperature`, and Sensi reports `temperature: null` while Off.

**Where the data is.** The HA diagnostics download (Settings -> Devices & Services -> Climate Advisor -> Download diagnostics, `diagnostics.py`) and the investigator log downloads carry the same redacted blocks (identifying attributes -- serial, MAC, IP, location, `friendly_name`, tokens -- are redacted inside `diagnostic_snapshot.redact_sensitive()` before any consumer sees them):

| Block | What it tells you |
|---|---|
| `thermostat_snapshot.climate_entity` | `state` (the thermostat's live mode), redacted `attributes`, and `capabilities`: `hvac_modes`, `supports_heat` / `supports_cool` / `supports_heat_cool`, `supported_feature_names`, `min_temp` / `max_temp` / `target_temp_step`, `fan_modes`, `preset_modes`, `temperature_unit`. If the mode CA is asking for is not in `hvac_modes`, you will see `hvac_mode_not_applied` with `kind: rejected` instead of a mismatch. |
| `thermostat_snapshot.last_command` | What CA last asked: `mode` / `mode_time`, `setpoint_f` (internal F) and `setpoint` (+ `setpoint_unit`) / `setpoint_time`, `result` (`executed`, `unconfirmed`, `failed`, `dry_run`, `whf_blocked`, `superseded`), plus `live_state` and `live_state_matches_commanded`. `read_back_skip_reason` says why the 10 s / 130 s mode read-back did not judge (`excluded_manual_override`, `excluded_whf_owned`, `excluded_paused`, `excluded_dry_run`, `excluded_command_in_progress`, `unreadable`). `mode_mismatch_since` / `mode_mismatch_episode_mode` are set while a mismatch episode is open; `command_failure_since` / `command_failure_mode` while a command-failure episode is open. |
| `recent_hvac_events` | The newest 40 Activity Log events whose type starts with `hvac_`, `setpoint_`, `manual_override`, `thermostat_`, plus `classification_applied` and `incident_detected` -- so the command, the read-back and the incident appear in order without a separate log pull. |

**Confirming a thermostat that ignores the mode (all of these together):**
1. `last_command.mode` is `heat`/`cool`, `live_state` is something else (typically `off`), `live_state_matches_commanded` is `false`, and `read_back_skip_reason` is `null` (the check ran and judged).
2. `recent_hvac_events` shows `hvac_mode_not_applied` (`kind: not_obeyed`, `requested_mode`, `actual_mode`) about 130 s after a `comfort_band_applied` / `setpoint_*` event whose `outcome` is `sent`. One row per episode; it closes with `hvac_mode_applied_recovered` (`minutes_down`).
3. Logs (`python tools/ha_logs.py --lines 3000`, grep): `Switching HVAC mode before setpoint write` (CA did send the explicit mode switch), then WARNING `HVAC mode not applied -- requested ... but thermostat reports ...`. If the switch line is absent, CA believed the mode already matched (check `live_state` freshness / the 120 s dedupe) -- if present and the thermostat still reports the old mode, the integration is swallowing `climate.set_hvac_mode` itself.
4. The Status tab / `GET /api/climate_advisor/status` -> `hvac_control_issues` has a `mode_not_obeyed` entry while the episode is open.

**Command failures instead of silent ignoring:** `incident_detected` with `incident_class: hvac_command_failed` appears once per episode (`hvac_mode`, `climate_entity`, `error`, `setpoint_f` internal F), repeated failures log ERROR only; `hvac_command_recovered` closes it; `hvac_control_issues` shows `command_failing`. A `TIMEOUT` (no answer from a slow cloud thermostat within 10 s) is logged WARNING `no answer from thermostat ... state unknown` and is deliberately NOT a failure -- rely on the read-back. HA validation rejections (value out of range, mode not offered) log WARNING `rejected by Home Assistant validation, skipped` and never open an episode.

**Not a bug (nothing to fix):** the thermostat returning from `unavailable` as `off` does not start a manual-override grace (Issue #1049, [grace-periods-spec.md](grace-periods-spec.md)); read-back is quiet during a manual override, WHF ownership, a door/window pause, dry-run, or while a newer write is in flight.

Reference: [Computation Reference, Command shapes since #301](08-COMPUTATION-REFERENCE.md#command-shapes-since-301-explicit-mode-switch-issue-1049); reproduction on the real engine: `tools/simulations/pending/issue_1048_sensi_like_off_thermostat_must_switch_to_heat.json` ([sim-harness-spec.md](sim-harness-spec.md), `thermostat_quirk: sensi_like`).

## Control quality report (Issue #1066)

Every diagnostic download carries a `control_quality` block (Download Logs / Download Full Logs under `diagnostics`, HA "Download diagnostics" under `thermostat_snapshot`, the investigator report footer as one summary line). It is numbers and counts only (no entity names), built from in-memory data, for two windows: `24h` and `7d`. Any field is `null` when there was not enough data -- `null` means "unknown", never "zero". All temperatures are internal Fahrenheit; `temp_unit` is the unit the home displays.

| Field | Plain-language meaning | How to read it |
|---|---|---|
| `samples` | How much data backs the numbers (mode changes, chart rows, hours of chart coverage) | Low coverage means every other number is a partial picture. |
| `misarmed` | Minutes (and episodes, longest episode, % of window) the thermostat was armed on the wrong side of the comfort band: cooling armed while the house is below the floor, or heating armed while above the ceiling. Time with the mode off (e.g. windows-open pause) is not counted. It is measured from the *commanded* mode, so it reads 0 when the thermostat ignores a command and stays in the wrong mode -- it flags bad arming decisions, not a thermostat that fails to follow them. | Should be near zero. Sustained minutes mean the nearest-edge arming is not keeping up or a regression was introduced. |
| `mode_changes` | Count and per-day rate of heat/cool re-arms, and `edge_to_armed_latency_s` (median/max seconds from the house first crossing the band edge to the matching mode being armed). | High per-day counts with tiny latencies = chatter; large latency = slow re-arm. `null` latency = every change happened before the edge was crossed (pre-arming). |
| `runtime` | Heating/cooling minutes, run count, median run length, and runs under 10 minutes, from chart-log `hvac_action`. A gap longer than 60 minutes between rows is capped at 60 minutes and ends a run. | Many short runs = short cycling. Rows older than 3 days are hourly averages, so the 7d figures undercount short runs. |
| `run_context` | Running minutes split by whether the house was gaining/losing heat (outdoor >= indoor while cooling, outdoor <= indoor while heating) or on the self-correcting side. | A large "gaining/losing" share means the run was unavoidable; the passive share is the only place a smarter rule could save run time. |
| `would_have_deferred` | Running minutes that a candidate rule (indoor strictly past the edge but by at most 1 F -- readings inside the band, e.g. during TOU pre-cool banking, do not count -- outdoor on the self-correcting side and moving that way, archived prediction back inside the band within about 30 minutes) would have deferred. Nothing is deferred in production. | Compare to total run minutes: a small share means model-based deferral is not worth building for this home. |
| `prediction_error` | Error of the archived indoor prediction: `horizons` (30m/60m/120m) compare the predicted change with the actual change (signed = predicted minus actual); `archive_lead` is predicted minus actual at the same instant (the ~4 hour lead error). | This is the accuracy gate for any future deferral: mean absolute errors of several degrees mean the prediction cannot be trusted to defer a run. |
| `settling_guard` | Schema 3 (Issue #1073; zone added in #1077). How often the settling guard held a heat/cool flip in the window: `hold_episodes`, `hold_minutes_total`, `median_hold_minutes` (completed holds), `outcomes` (`recovered` = the house came back inside the band without needing the opposite mode, `switched` = the guard gave way, `lapsed` = the recency window ran out, `open` = still holding), `recovered_share` (recovered / completed), `max_depth_f` (deepest overshoot held, over `breach`-zone holds only; margin holds are inside the band so have no overshoot, and holds stored before zone was recorded are `unknown` and excluded), `holds_by_zone` (`breach` = past the comfort edge, `margin` = inside the band near the edge, `unknown`), `holds_by_model_confidence` (`none`/`low`/`medium`/`high`/`unknown`: the thermal-model confidence when the hold started), and `mode_writes.by_reason` (count of heat/cool re-arms per reason; free-text reasons show as `other`). Aggregates only; `null` means no data (a live engine with no holds reports `hold_episodes` 0). | A high `recovered_share` means holds are saving needless opposite-mode runs; many `switched`/`lapsed` outcomes with large `max_depth_f` mean the tolerance is too tight or the house really needs the other side. Comparing `recovered_share` across `holds_by_model_confidence` is the data that decides whether a model-based shorter hold is worth building (nothing is model-driven today). |

### Reading holds and mode-set rows (Issue #1073)

Two activity rows explain every heat/cool decision. Full rules: [comfort-edge-selection-spec.md](comfort-edge-selection-spec.md).

| Row | Meaning | How to read it |
|---|---|---|
| `thermostat_mode_set` | Climate Advisor requested a mode change (re-affirms of the same mode are not logged). Fields: from/to mode, `reason` (fixed label per source), `source` (selector, pause, restore, tou, ode_guard, nat_vent, user, startup, other), indoor, floor/ceiling, `outcome` (always `requested`; the zone is the generic label `thermostat`). | To find who flipped the thermostat, match the time to a row and read `source` and `reason`. The row only says the change was requested; whether the thermostat obeyed is shown by `hvac_mode_not_applied`, `hvac_mode_applied_recovered`, `hvac_command_recovered` or `hvac_write_blocked_whf_active` (#1049). |
| `comfort_switch_held` | The settling guard deliberately did NOT flip: the house is slightly past an edge shortly after the opposite side ran (or the user acted). Fields: `zone` (`breach` = past the edge, `margin` = still inside the band near the edge), `depth_f` (degrees past the edge, 0.0 inside the band), `tolerance_f`, `opposite_ran_min_ago`, `window_min`. | A hold is expected when the house is no more than `tolerance_f` past the edge and `opposite_ran_min_ago` < `window_min`. It ends when the window lapses or the house goes deeper than the tolerance past the edge (the mode then switches at once and a `comfort_family_switch` row follows). If comfort is complained about during a hold, the knobs are `comfort_deadband_*_f` (tolerance) and `comfort_family_recency_window_min` (window). Repeated holds with an `opposite_ran_min_ago` that never grows can mean the "ran" source is stuck; check the INFO line `Comfort edge switch held:` in the HA log. |

The Debug tab shows a "holding {family}: ... (until ~HH:MM)" line while a hold is active (HH:MM is the absolute time the window lapses, in your temperature unit). `comfort_family_switch` now also appears when the previous thermostat mode was unknown (`from_family` = `unknown`).

Source of truth: `custom_components/climate_advisor/control_quality.py` (module docstring has the exact definitions); the mode-change ring buffer is `AutomationEngine.comfort_mode_changes`.

## Debugging a house-off-target stall alert (Issue #1050)

**What the occupant experienced:** the house sat more than 1 degree outside the comfort band for 3+ hours and the heating/cooling never started, and nothing told them. The alert is push-only (no email) and repeats every 12 h while it persists.

1. **Is it an alert or a bug?** Look for `hvac_output_stall` in the Activity Log (payload `direction`, `indoor_f`, `edge_f`, `hours`, `thermostat_mode`, `thermostat_action`) and WARNING `Comfort stall detected: direction=... thermostat_mode=... action=...`. Status shows a matching `output_stall` entry in `hvac_control_issues`. `thermostat_mode: off` with `action: idle` is a thermostat left Off or one that ignored the mode (check for an open `mode_not_obeyed` episode first -- the stall push and Status line are held while it is open, the clock keeps running, and the stall surfaces when the episode closes if the house is still off-target); `thermostat_mode: heat` with `action: idle` means the thermostat believes it is satisfied (setpoint, sensor offset, dead heat source).
2. **Why no alert when the house is clearly off?** The check is deliberately quiet (clock resets) during a manual override or pending confirm, a door/window pause or open sensor, startup coalescing, an unavailable thermostat or indoor reading, a thermostat that exposes no `hvac_action` (it cannot prove the equipment is idle), a mode the thermostat does not offer, and an in-tolerance nat-vent deviation. DEBUG `Comfort stall check skipped: excluded_by=<reason>` names which. Any heating/cooling `hvac_action` seen in the needed direction restarts the 3 h count, even a short cycle between two 30-min updates.
3. **Recovery:** `hvac_output_stall_recovered` (`reason: in_band` or `equipment_ran`, `minutes_down`), INFO `Comfort stall recovered`. State is not persisted, so an HA restart restarts the clock.
4. Constants: `COMFORT_STALL_MARGIN_F`, `COMFORT_STALL_SUSTAIN_SECONDS`, `COMFORT_STALL_REMINDER_SECONDS` in `const.py` (not user-configurable). Spec: [08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md#house-off-target-stall-alert-issue-1050-part-of-1048).

## Debugging a fan switch that ignores the on-command (Issue #1050)

**What the occupant experienced:** CA turned the whole-house fan on, the switch did nothing (fan-state feedback is off by default, so CA had no way to know), and the AC stayed suppressed while the house warmed up.

1. Activity Log: `fan_cancel` with `trigger: physical_drift_correction`, `readback: true`, `retry: true` ("Fan switch ignored the on-command -- retrying once") roughly 5-10 minutes after a `fan_activated`. A second `fan_cancel` with `retry: false` ("...again -- giving up on the fan", with `session_kept`) plus one WARNING `Fan command ignored twice` means the switch still did not react; CA then stops activating the fan until it reads on or you turn it on.
2. Logs: INFO `Fan command ignored: command entity never reacted to CA's on-command (attempt N) -- releasing and retrying once | giving up`; DEBUG `Fan drift tick: ... readback=True`.
3. What it will NOT do: an `assumed_state` entity (RF/IR/momentary transmitter), an `unavailable`/`unknown` entity, a manual or RF-remote override session and the HVAC fan are all left alone -- CA treats "cannot tell" as unknown, never as off. If the switch was turned off by a person after CA's command, that is a manual change handled by the fan listener, not a read-back failure.
4. Fix on the user's side: check the fan switch, or enable fan-state feedback and point it at a real state entity. Spec: [fan-lifecycle-spec.md](fan-lifecycle-spec.md#command-read-back-issue-1050).

## Debugging AI Features

### AI Status Sensor

`sensor.climate_advisor_ai_status` is the first place to check when AI features are not responding:

- **`active`** — AI integration is healthy and making successful requests
- **`inactive`** — AI is enabled but no requests have been made yet
- **`error`** — last request failed; check the `error_count` attribute
- **`disabled`** — AI features are turned off in configuration
- **`circuit_open`** — circuit breaker has tripped after 5 consecutive failures; will auto-reset after 5 minutes

### AI Investigative Analysis

The dashboard's "AI Investigative Analysis" report (`/api/climate_advisor/ai_investigate`, or the deterministic-analysis-only equivalent) triggers an on-demand AI analysis of recent automation behavior. This is useful for diagnosing unexpected HVAC decisions — the report includes a timeline, key decisions, anomalies, and diagnostics drawn from current system state. (The separate AI Activity Report skill/service was retired in Issue #578 — superseded by this investigator report and the deterministic, non-AI Activity Record.)

**`hours` parameter:** Controls how far back the event log and context window reach; the dashboard defaults to 24h. For requests spanning more than 36 hours, the report automatically appends **HISTORICAL DAILY SUMMARIES** — a per-day rollup from the DailyRecord history covering the requested window.

```bash
# Check report history file directly
python3 tools/ha_logs.py --history --entity sensor.climate_advisor_ai_status --hours 24
```

**ChatGPT provider logs:** INFO `Investigation requested: provider=... model=... entry_id=...` names the zone's provider and resolved model; INFO `ChatGPT sign-in completed` and `ChatGPT account disconnected|removed (revocation: confirmed|unconfirmed|not_needed)` track the account; at DEBUG, `ChatGPT stream ended: terminal=... status=... request_id=...`, `ChatGPT request|stream failed: category=... code=...` and `ChatGPT token refresh succeeded` show the last exchange. `category=rate_limit` is a 429 that failed that one request; only WARNING `ChatGPT usage limit reached ...` (`category=quota`) marks the one-hour pause of every zone. No tokens, email, user id, prompts or response text are logged.

### AI Report Persistence

Investigation reports are stored at `climate_advisor_investigation_reports.json` in the HA config root directory, capped at `INVESTIGATION_REPORT_HISTORY_CAP` entries. Request history is capped at 50 entries (`AI_REQUEST_HISTORY_CAP`). (The separate `climate_advisor_ai_reports.json` file from the retired AI Activity Report feature — Issue #578 — is no longer read or written; it may still exist on disk from before the upgrade and can be safely deleted.)

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
| **RESOLVED** | Matches a fix_history.jsonl entry at or before the current version | Off-by-one in HVAC observation count → Issue #156, fixed in v0.3.44 |

**Common noise patterns:**

- **Count discrepancies ≤ 1** between thermal model cache and rejection log: NOISE (transient EWMA flush lag, not a bug)
- **High abandonment rates** where top reason is `hvac_started` or `sensor_opened`: NOISE if committed count > 0 (operational interruptions are expected)
- **Pending / in-flight observation speculation** (e.g., "this observation has 0 samples at 4.4 minutes — may fail"): NOISE (unknown outcome; pending obs belong in the activity report, not the investigator)
- **chart_log endpoint count 1–5** when block-OLS count > 0: NOISE (not at the hard "both = 0" threshold)
- **96%+ abandonment on hot days with active HVAC**: NOISE — `passive_decay` is constantly interrupted by HVAC cycles on days when the system is actively heating or cooling; the committed count is what matters

**Classifying a finding step-by-step:**

1. Is it covered by a fix_history entry at or before the current CA version? → **RESOLVED**
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

### WHF (Whole-House Fan) Troubleshooting (Issue #361)

**Symptom: WHF appears unresponsive to CA commands**

1. Check `whf_mode` in the debug pane of the Climate Advisor dashboard:
   - `"command-only"`: CA is issuing commands but cannot confirm the fan received them.
     The `fan_entity` is treated as command-only (relay/switch that echoes last command).
   - `"state-feedback"`: CA reads actual motor state. If fan is still unresponsive,
     check whether `fan_state_entity` is correctly reporting the motor state.
   - `"disabled"`: Fan control is disabled in config.

2. If `whf_mode = "command-only"`:
   - Inspect `fan_entity` in HA Developer Tools → States. If its state only changes when
     CA issues a command (not when you flip the physical wall switch), it's a command-echo
     entity. This is normal — CA will still command it, it just can't confirm reception.
   - To enable physical override detection and state confirmation: configure
     `fan_state_entity` with a binary_sensor or switch that reads actual motor state,
     then set `fan_state_feedback = True` in options.

3. `whf_last_commanded` in the debug pane shows what CA last commanded (`"on"`, `"off"`, or
   `null` after restart). If it shows `"on"` but the fan isn't running, the relay may be faulty.

**Symptom: CA keeps re-commanding the fan unexpectedly**

In command-only mode, CA re-issues a command only when `_fan_active` (desired) diverges from
`_last_commanded_fan_state` (last issued command). This can happen after HA restart
(`_last_commanded_fan_state` resets to `None`) or after a post-grace reconciliation.
Check HA logs for: `"Fan command-only assert: desired=..."` log lines.

**Known limitation**: In command-only mode (`fan_state_feedback=False`), physical wall-switch
user overrides are undetectable. CA will re-assert its desired state on the next cycle.
Install a `fan_state_entity` sensor to enable override detection.

---

## Debugging the Learned Swing (Issue #1233)

**The question a homeowner asks:** the dashboard says "Heat swing 1.4 F below setpoint" - does that match my
thermostat? (The swing is how far past the setpoint the thermostat lets the room drift before starting the
equipment.)

1. **What did Climate Advisor judge, and why?** `python3 tools/learning_db.py --swing [--last 30] [--entry-id ID]`
   - one row per judged cycle (accepted or `rejected:<reason>`) with the setpoint, setpoint age, on/off readings,
   source entity, resolution and trigger; the reject histogram; the median recomputed from the retained samples
   (a `MISMATCH` flag means the stored value disagrees with them); and the coast section.
2. **Is it true of my equipment?** Save a few days of recorder history and compare:
   ```bash
   python3 tools/ha_logs.py --history --entity climate.my_thermostat --hours 72 > climate.txt
   python3 tools/ha_logs.py --history --entity sensor.my_fine_temperature --hours 72 > sensor.txt
   python3 tools/swing_audit.py --climate-file climate.txt --sensor-file sensor.txt --from-db
   ```
   `swing_audit.py` judges each cycle with the same `swing.evaluate_swing_cycle` the integration uses and prints
   `measured X F` next to `learned Y F` (`matches` / `DIFFERS`). Without a fine sensor it uses the thermostat's own
   whole-degree `current_temperature` and says so. Its coast line replays the history through the integration's own
   coast driver, so it shows what the integration records (a fine sensor's ticks count as its readings).
3. **In the logs:** `python3 tools/ha_logs.py --lines 5000 --filter "Swing sample"` - INFO for accepted samples
   (with `median before->after`), DEBUG for rejected ones (needs debug logging for `custom_components.climate_advisor`).
   Every line carries `zone=<entry id>`; the HVAC observation start/commit lines do too.

| Reject reason | Meaning for the occupant |
|---|---|
| `setpoint_change` | The equipment started within 120 s of a setpoint or mode change - CA's own or yours moved the target; not the thermostat's hysteresis |
| `short_restart` | `heating -> idle -> heating` within 60 s - the thermostat flapped, not a new drift |
| `wrong_direction` | A heat run whose temperature fell (or cool run that rose) - something else dominated |
| `non_positive` | The equipment started at/inside the setpoint - not a hysteresis start |
| `out_of_bounds` | Outside 0.1-5.0 F |
| `missing_input` | No setpoint or no usable indoor reading at the start or stop |

The history tool needs `--hours` above 24 to span several days; before Issue #1233 it silently returned only the
oldest 24 h (no `end_time` was sent).

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
- `"Thermal HVAC observation not started [zone=... type=... reason=indoor_unavailable ...]"` (INFO) — an HVAC session began while the indoor temperature sensor was unreadable, so that session was deliberately not learned (Issue #1035). It is also stored as a structured `indoor_unavailable` rejection: `python3 tools/learning_db.py --rejections --entry-id <id>`. Check the Activity Log for the `indoor_sensor_unavailable` / `indoor_sensor_recovered` rows (Issue #1033) to see how long the outage lasted. The next `"Thermal HVAC observation started"` line shows learning resumed.
- DEBUG only (enable debug logging): `"Pre-heat sample skipped: reason=indoor_unavailable"` (every tick) and `"Event-driven HVAC sample skipped: type=... reason=indoor_unavailable"` — samples dropped during an outage instead of being stored as fake `0.0°F` readings.

**Step 4 — Common root causes:**

| Symptom | Likely cause | What to check |
|---|---|---|
| All rejections `small_delta` | Integer-°F thermostat; ΔT < 0.2°F in 30 min | Normal — multi-window (Issue #126) accumulates up to 4h; check "keeping alive" logs |
| All rejections `too_few_samples` | Conditions change too fast | Check elapsed_minutes in rejection log; may need longer stable windows |
| R² rejection logged repeatedly | Short HVAC runs or sensor noise | Use 24h chart view; check run lengths |
| Rejections show `indoor_unavailable` (elapsed 0) | Indoor sensor was unreadable when an HVAC session started; session not learned (Issue #1035) | Activity Log `indoor_sensor_unavailable` rows; fix the sensor — no action on the thermal model needed |
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
- `handle_door_window_open()` — logs classification context; inside a planned window period it logs "running the nat-vent gate" (Issue #1211: no exemption, it then starts nat-vent or pauses)
- `_grace_expired()` — logs whether sensors are still open (no planned-window branch since Issue #1211)
- `_re_pause_for_open_sensor()` — logs the nat-vent gate result; "Planned window period ended — reconciling door/window pause with live sensors" comes from the exit-edge reconcile (Issue #1211, O4)
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
| 1 | Setpoint-only change not treated as override | `coordinator.py` — setpoint change block now calls `handle_manual_override(source="setpoint")` and enters the manual grace period | Fixed — v0.3.55 | [#197](https://github.com/gunkl/ClimateAdvisor/issues/197) |
| RC-1 | Override confirm state lost on restart | `automation.py restore_state()` never restores `override_confirm_pending` / `override_confirm_time` — 10-min gate is lost | Confirmed bug | [#198](https://github.com/gunkl/ClimateAdvisor/issues/198) |
| RC-2 | Grace timer not restored on restart | `async_restore_state()` re-schedules grace timer with remaining duration; if already expired, override cleared immediately on startup | Fixed — v0.3.56 | [#199](https://github.com/gunkl/ClimateAdvisor/issues/199) / [#227](https://github.com/gunkl/ClimateAdvisor/issues/227) |
| PATH-B | Self-resolve discards short deliberate overrides | `automation.py:630–644` PATH B treats quick mode revert as transient — no grace granted | Design gap | [#200](https://github.com/gunkl/ClimateAdvisor/issues/200) |
| 2nd | Second override during active override silently ignored | `coordinator.py:2172` gates override detection on `not _manual_override_active` | Design gap | [#201](https://github.com/gunkl/ClimateAdvisor/issues/201) |
| 30s | 30-second guard too wide for setpoint changes | `coordinator.py:2387` uses 30s vs 3s for mode changes — user changes within 30s of CA command are dropped | Design gap | [#202](https://github.com/gunkl/ClimateAdvisor/issues/202) |
| BW | Bedtime/wakeup handler unconditionally clears active override | `automation.py:1926` (`handle_bedtime_setback`) and `automation.py:2025` (`handle_morning_wakeup`) called `clear_manual_override()` unconditionally — override silently wiped at scheduled sleep/wake time even with active grace | Fixed — Issue #204 | [#204](https://github.com/gunkl/ClimateAdvisor/issues/204) |
| BW2 | Bedtime/wake-up clears an active FAN / RF-timer override and cancels its grace | `automation.py` `handle_bedtime` / `handle_morning_wakeup` (#874 block) | Kept deliberately (Issue #1027, 2026-10-07). Visible since Issue #1029 as `fan_override_cleared` + `grace_cancelled` in the Activity Log (reason `bedtime` / `morning_wakeup`, with remaining minutes and RF timer hours) | [#1027](https://github.com/gunkl/ClimateAdvisor/issues/1027) / [#1029](https://github.com/gunkl/ClimateAdvisor/issues/1029) |
| OA | Away/vacation setback uses classification hvac_mode instead of actual thermostat mode | `automation.py` `handle_occupancy_away()`, `handle_occupancy_vacation()` | On days that start in cool mode but nightly reclassification switches to heat, away setback applies heat setpoint (e.g. 61°F) to a cooling-mode thermostat — AC runs constantly trying to reach 61°F. Observed June 6: 349 min runtime vs 209 min avg. | Fixed this session (issue #222) |
| OB | Away setback setpoint change not guarded by time-based temp command check | `coordinator.py` ~line 2422, `automation.py` `_set_temperature()` | `_temp_command_pending` clears before `_async_thermostat_changed` fires. Automation's own setback (e.g. 72→79°F) detected as user manual override — spurious 90-min grace period blocks subsequent automation. | Fixed this session (issue #221) |

### Bypass #1 — Setpoint-only change (Issue #197) — FIXED

**Symptom (pre-fix)**: User raises setpoint (e.g. 72→76°F) with no mode change; CA re-applies 72°F at the next 30-min cycle.

**Root cause**: `_async_thermostat_changed` in coordinator.py only incremented the daily override counter for setpoint changes — it never called `handle_manual_override()`. The mode-change path called `handle_manual_override()`, but the setpoint-change path did not.

**Fix (v0.3.55)**: The setpoint-change block now calls `handle_manual_override(source="setpoint")` and enters the standard manual grace period. PATH B `_confirm_override_expired` treats `source="setpoint"` as always confirmed (mode equality does not imply setpoint agreement).

**Diagnostic log pattern (post-fix)**:
```
# A setpoint-only change now produces:
handle_manual_override called source=setpoint
```

### RC-1 — Override confirm state lost on restart (Issue #198)

**Symptom**: CA restarts while in the 10-minute override confirmation window; override is cleared immediately after restart instead of holding for the remaining window.

**Root cause**: `restore_state()` in automation.py (lines 2280–2315) does not restore `_override_confirm_pending`, `_override_confirm_time`, or `_override_confirm_mode`. These fields are serialized by `get_serializable_state()` but silently dropped on restore.

**Diagnostic**: Correlate a CA reload event in logs with a sudden `_manual_override_active → False` transition within minutes of an override being set.

### RC-2 — Grace timer not restored on restart (Issue #199) — FIXED in v0.3.56

**Symptom (pre-fix)**: After a CA restart, the manual override flag remained active indefinitely — CA never re-applied its schedule. Dashboard showed "0 min remaining" on the override status but the override never cleared.

**Root cause**: `automation.py:2303–2304` contained an explicit comment: "Grace timers cannot be restored — clear on restart." `_manual_override_active=True` was restored (line 2286) but the `_grace_expired` timer was not rescheduled. The flag stayed set until the `clear_manual_override` service was called.

**Fix (v0.3.56 / Issues #199 + #227)**: `async_restore_state()` now computes the remaining grace duration from the persisted `_grace_end_time` ISO timestamp. Three cases:
- Remaining > 0: `async_call_later(remaining)` re-schedules the expiry callback
- Already expired: `clear_manual_override(reason="grace_expired_during_restart")` called immediately
- Exception: `clear_manual_override(reason="grace_restore_error")` called as safety fallback

**Diagnostic log pattern (post-fix)**:
```
# Grace restored with remaining duration:
[climate_advisor] Restoring grace period timer — remaining=847s
# Grace already expired during restart:
[climate_advisor] clear_manual_override called — reason=grace_expired_during_restart
```

### BW — Bedtime/wakeup handler unconditionally clears override (Issue #204) — FIXED

**Symptom**: User sets a manual thermostat temperature shortly before the configured `sleep_time` or `wake_time`. Override grace starts (90 min by default). At the scheduled time, CA silently resets the thermostat to the setback temperature with no notification. `sensor.climate_advisor_last_action_reason` shows bedtime or wakeup setback — no indication that a manual override was active.

**Root cause**: `handle_bedtime_setback()` (automation.py:1926) and `handle_morning_wakeup()` (automation.py:2025) called `clear_manual_override()` unconditionally before applying the sleep/wake setpoint. Neither checked `_manual_override_active` before proceeding.

A secondary root cause: `clear_manual_override()` had no `reason` parameter, so all four callsites were indistinguishable in logs — making diagnosis require a full code trace.

**Fix (Issue #204)**: Both handlers now check `_manual_override_active` before proceeding. If `True`, the handler skips entirely and emits `bedtime_setback_skipped` / `morning_wakeup_skipped`. `clear_manual_override()` now takes a `reason=` parameter logged at INFO.

**Diagnostic log pattern (post-fix)**:
```
# Override active at sleep_time — expect this log, NO thermostat change:
[climate_advisor] handle_bedtime_setback: skipping — manual override active (_manual_override_active=True)
[climate_advisor] Event: bedtime_setback_skipped

# Override not active at sleep_time — normal setback path:
[climate_advisor] clear_manual_override called — reason=bedtime_setback
```

**Cross-reference**: [Grace Periods Spec — What Clears a Manual Override](grace-periods-spec.md#what-clears-a-manual-override)

### OA — Away/vacation setback mode mismatch (Issue #222) — FIXED

**Symptom**: On a day that began as a cooling day, occupancy transitions to away mid-day. CA applies the heat setback setpoint (e.g. 61°F) to a thermostat that is in cool mode. The thermostat interprets this as a cooling target and runs AC continuously, producing anomalously high HVAC runtime (observed June 6: 349 min vs 209 min rolling average).

**Root cause**: `handle_occupancy_away()` and `handle_occupancy_vacation()` selected the setback branch by reading `classification.hvac_mode` (the day's original classification) rather than the actual live thermostat mode. A warm-day classification could have `hvac_mode = "heat"` from the previous night's reclassification while the physical thermostat was still running in cool mode. The automation then called `_set_temperature_for_mode("heat", ...)` — applying the heat setback setpoint to a cooling thermostat.

**Fix (Issue #222)**: Both handlers now read the live thermostat state from `hass.states.get(self.climate_entity).state` to determine which setback branch to apply. If the live mode is `"cool"`, the cool setback setpoint is used; if `"heat"`, the heat setback setpoint is used; if `"off"`, no setpoint change is made.

**Diagnostic log pattern (post-fix)**:
```
# Occupancy → away with thermostat in cool mode:
handle_occupancy_away: live_mode=cool — applying cool setback (setpoint=79°F)

# Pre-fix (wrong branch):
handle_occupancy_away: classification.hvac_mode=heat — applying heat setback (setpoint=61°F)
```

### OB — Away setback detected as manual override (Issue #221) — FIXED

**Symptom**: When CA transitions to away mode and applies a setback setpoint (e.g. 72→79°F), the coordinator's `_async_thermostat_changed()` fires on the resulting thermostat state change and detects CA's own setpoint change as a user manual override. A spurious 90-minute grace period starts, blocking the automation engine from responding for the remainder of the away period.

**Root cause**: `_temp_command_pending` (the flag used to suppress override detection immediately after a CA command) was cleared by the time `_async_thermostat_changed()` processed the thermostat echo — the flag's guard window was too narrow (or absent) for setback commands from occupancy handlers. The setpoint change 72→79°F appeared identical to a user manual override from the coordinator's perspective.

**Fix (Issue #221)**: A `_temp_command_time` timestamp field was added to the automation engine. `_set_temperature()` records the current time into `_temp_command_time` on every CA setpoint command. A new `_is_recent_temp_command(window_seconds=30)` guard is checked in the coordinator's setpoint-change detection block: if the setpoint change arrived within 30 s of a CA-issued setpoint command, it is suppressed and no override is registered.

**Diagnostic log pattern (post-fix)**:
```
# Setpoint change arrives within 30 s of CA command — suppressed:
_async_thermostat_changed: setpoint change suppressed — recent CA command (0.8 s ago)

# Pre-fix (wrong detection):
handle_manual_override called source=setpoint  ← triggered by CA's own setback
```

**Cross-reference**: [Grace Periods Spec — What Clears a Manual Override](grace-periods-spec.md#what-clears-a-manual-override)
