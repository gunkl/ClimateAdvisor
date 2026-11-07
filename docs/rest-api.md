<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → api.py | ↔ [AI Integration](ai-integration.md) -->

# REST API — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does api.py own and what does it explicitly not own? | It owns the HTTP view layer — routing, auth enforcement, input validation, and response serialisation. It does NOT own business logic; every action delegates to the coordinator or automation engine. | [Scope](#scope) |
| How does authentication work and are there any unauthenticated endpoints? | All 22 views set `requires_auth = True`. A HA long-lived access token is required for every endpoint; no unauthenticated endpoints exist. | [Authentication](#authentication) |
| Which config values are ever redacted and why? | Two fields are redacted: `ai_api_key` (via `"sensitive": True` in CONFIG_METADATA) and `notify_service` (hard-coded name check). Both return `"configured"` or `"not set"` — never the raw value. (`github_token` is not in `CONFIG_METADATA`, so it never appears in this view at all; diagnostics redacts it separately, Issue #1062.) | [Sensitive Field Redaction](#sensitive-field-redaction) |
| What HTTP status codes does the API return and when? | 400 bad input, 403 feature disabled, 429 rate limit, 500 investigation failure, 503 coordinator/AI unavailable. Out-of-range numerics are silently clamped, not rejected. | [Error Handling](#error-handling) |
| How does api.py read coordinator state and why does it sometimes bypass the coordinator cache? | Primary reads use `coordinator.data.get(...)` with safe defaults. Live HVAC state is read via `hass.states.get(climate_entity_id)` to guarantee freshness past the 30-minute update cycle. | [Coordinator Access Pattern](#coordinator-access-pattern) |
| What security guardrails prevent sensitive data from leaking through the config or AI status endpoints? | `ClimateAdvisorConfigView` applies the two-case redaction rule. `ClimateAdvisorAIStatusView` calls `status.pop("api_key", None)` before serialising. Unknown suggestion-accept keys are applied in-memory only and never persisted to the HA config entry. | [Security Notes](#security-notes) |
| How does a caller know an `event_log`/`activity_record` response doesn't cover the full requested window? | Both endpoints return `is_truncated` (bool) and `oldest_available` (ISO timestamp or null) alongside their normal payload, computed by comparing the oldest stored event against the requested window's cutoff. | [Event Log Truncation Signal](#event-log-truncation-signal) |
| What is `hvac_control_issues` in the status response? | A list of `{issue, detail}` entries (same shape as `invariant_violations`) present only while the thermostat is not following Climate Advisor: `mode_not_obeyed` (requested mode vs live mode, minutes), `command_failing` (last attempted mode, minutes; since Issue #1121 shown only once the failure episode is at least `HVAC_CMD_FAIL_BANNER_MIN_AGE_MIN` = 5 min old, and hidden — like `mode_not_obeyed` — while the user owns the thermostat (manual override or its pending confirmation) or the thermostat already reports the failed mode) or, since Issue #1050, `output_stall` (house outside the comfort band for hours with no heating/cooling running; diagnostic text in the user's unit; hidden while a `mode_not_obeyed` entry is open). Empty list when fine; Status-tab only (Issues #1049, #1050). | [Endpoints](#endpoints) |
| What does `GET /api/climate_advisor/prediction_accuracy` return and where does its data come from? | Prediction engine v4's archived predictions scored against what the room then read, beside v3 and a "no change" baseline, for the "Prediction accuracy (preview)" page. Live v4 archive first, the published backtest file for older history; read-only, no decision reads it. | [Prediction accuracy endpoint](#get-apiclimate_advisorprediction_accuracy-issue-1230-r1) |
| Where does the v4 shadow model show up for debugging? | `GET /api/climate_advisor/engines` carries a `v4` key and `get_debug_state()` (`automation_state` endpoint) a `v4_model` key: confidence, fit time, span, training MAE, last fit/cycle, archive slot counts. | [Engines and debug keys](#engines-and-debug-keys-for-v4) |

## Scope

**Owns:**
- HTTP view registration for all 22 Climate Advisor REST endpoints
- Auth enforcement (`requires_auth = True` on every view)
- Input validation and HTTP error responses (400, 403, 429, 500, 503)
- Response serialisation: field selection, display transforms, sensitive-field redaction
- Routing of POST actions to the correct coordinator method or automation engine call

**Explicitly does NOT own:**
- Business logic — all actions delegate to `coordinator.py` or `automation.py`
- Thermal model, learning, classification — owned by `learning.py`, `classifier.py`
- AI execution — delegated to `coordinator.ai_skills.async_execute(...)` (`ai_skills.py`)
- Chart data computation — delegated to `coordinator.get_chart_data()` (`coordinator.py`)
- Debug state assembly — delegated to `coordinator.get_debug_state()` (`coordinator.py`)

## Authentication

All 22 view classes set `requires_auth = True`. HA handles token validation before the view handler is invoked. No endpoint is reachable without a valid HA long-lived access token. There is no per-endpoint permission model beyond HA authentication.

## Endpoints

### GET Endpoints

| URL | Purpose | Key response fields |
|---|---|---|
| `/api/climate_advisor/status` | Current system snapshot | `day_type`, `trend_direction`, `trend_magnitude`, `hvac_mode`, `hvac_action`, `indoor_temp`, `outdoor_temp`, `unit`, `automation_status`, `occupancy_mode`, `fan_status`, `contact_status`, `manual_override_active`, `next_action`, `compliance_score`, `invariant_violations`, `hvac_control_issues`, `remote_offset` (Issue #1194 d: the remote-sensor outer loop's state, comfort target vs commanded setpoint, signed offset and the Status sentence; `null` without a remote sensor; fields in [08 Computation Reference](08-COMPUTATION-REFERENCE.md#remote-sensor-outer-loop-on-the-thermostat-setpoint-issue-1194-increment-a)) |
| `/api/climate_advisor/briefing` | Today's daily briefing text | `briefing`, `briefing_sent_today`, `verbosity` |
| `/api/climate_advisor/chart_data` | Temperature forecast chart data | Delegated to `coordinator.get_chart_data()`. Response includes `target_band` time-series array, `historical_setpoint`, and `predicted_setpoint` arrays (Issue #151, #160). |
| `/api/climate_advisor/automation_state` | Full automation engine debug state | Delegated to `coordinator.get_debug_state()` |
| `/api/climate_advisor/learning` | Learning engine summary | `today_record`, `yesterday_record`, `tomorrow_plan`, `suggestions`, `compliance`, `comfort_range_low`, `comfort_range_high`, `unit` **Units (Issue #1049):** `tomorrow_plan.expected_high` / `expected_low` are converted at this boundary into the configured `unit` (via `temperature.display_temp()`, on a copy; `coordinator.tomorrow_plan` stays internal °F), matching `comfort_range_*`; the frontend appends the unit symbol and does no conversion. |
| `/api/climate_advisor/config` | Integration configuration | `settings` list of `{key, value, label, description, category[, unit]}` — sensitive values redacted; temperature settings carry `unit` and are converted to the user's unit (see [Config Display Transforms](#config-display-transforms)) |
| `/api/climate_advisor/ai_status` | AI client health and budget | `status` (api_key removed), `recent_requests` (last 10) |
| `/api/climate_advisor/investigation_reports` | AI investigator report history | Direct return from `coordinator.get_investigation_report_history()` |
| `/api/climate_advisor/event_log` | Internal event log | `events`, `total`, `hours` — `hours` param silently clamped if out of range. `is_truncated`, `oldest_available` (Issue #432) — see [Event Log Truncation Signal](#event-log-truncation-signal) |
| `/api/climate_advisor/engines` | Prediction engine status | `get_engine_status()` of the learning engine plus `unit` and (Issue #1230 R1) `v4` - see [Engines and debug keys](#engines-and-debug-keys-for-v4) |
| `/api/climate_advisor/prediction_accuracy` | v4 vs v3 vs "no change" prediction scoring (Issue #1230 R1, read-only) | `points`, `heat_runs`, `stats`, `model`, `gate`, `gate_summary` - see [below](#get-apiclimate_advisorprediction_accuracy-issue-1230-r1) |
| `/api/climate_advisor/activity_record` | Deterministic event timeline table (no AI required) | `table` (from `build_event_timeline_table()`), `hours`, `generated_at`, `is_truncated`, `oldest_available` (Issue #432) — see [Event Log Truncation Signal](#event-log-truncation-signal) |

**Chart data additions (Issue #1230 R2, additive; every older key keeps its shape and unit):** `indoor_sensors` (`[{id, name, role, primary}]` for each indoor sensor that read in the window, from chart_log `indoor_sensors`), `actual_indoor_sensors` (`{entity_id: [{time, temp}]}`, converted to the display unit like `actual_indoor`), and `v4`: `{ready, reason, gate, model, leads, sensors, future, future_at, baseline, past, corrected_outdoor}`. `ready` is the backtest file's `gate_summary.ready is True`; `reason` is `ok`, `not_ready`, `no_backtest`, `shadow_off` or `error`. While not ready, `past`, `future`, `baseline` and `corrected_outdoor` are empty: a v4 line the backtest has not cleared never reaches the chart. When ready: `past` = `{sensor: {lead: [{ts, temp}]}}` for leads `1h/4h/12h/24h` (what v4 said that long before each 30-minute slot: live archive first, the backtest file for older slots; hourly means beyond 3 days, daily beyond 30), `future` = newest 24 h line per sensor (empty for a historical `before_ts` view), `baseline` = the same predict with no setpoints, `corrected_outdoor` = `{past, future}` the weather service's outdoor values after v4's learned correction. `_ca` is never drawn (it follows the sensor CA reads and jumps when that swaps). Display only: nothing reads these for a decision.

**Chart Data Query Parameters:** `range` (string, default `24h`) and `before_ts` (Unix ms, optional). When `before_ts` is absent, the window ends at now (live mode); when present, the window is anchored at that point for historical navigation.

### GET /api/climate_advisor/prediction_accuracy (Issue #1230 R1)

Occupant view: a read-only page that shows how well the new prediction engine (v4, running in shadow) guessed the room temperature hours ahead, next to today's line (v3) and "the temperature just stays put". Reading it changes nothing in the house. Implementation: `ClimateAdvisorPredictionAccuracyView` (`api.py#L1375`) -> `prediction_accuracy.build_prediction_accuracy` (`prediction_accuracy.py#L181`); data and files: [v4 Shadow Spec](v4-shadow-spec.md).

**Query parameters** (every bad value is clamped to its default, never a 400, like `chart_data`):

| Param | Values | Default | Notes |
|---|---|---|---|
| `zone` | config entry id (`entry_id` accepted too) | the only zone; with several zones and no `zone` the view refuses with 400 (`resolve_zone(..., allow_guess=False)`, the `_ambiguous_zone_payload` body with `zones`) instead of guessing: a scorecard showing another zone's numbers would mislead the per-zone v4 decision. The preview page always sends `zone` once `/status` has listed the zones | |
| `sensor` | an indoor entity id, or `_ca` | `_ca` | `_ca` = the reading Climate Advisor itself uses. Unknown ids simply return no points |
| `lead` | `1h`, `4h`, `12h`, `24h` | `4h` | How far ahead the prediction was made. Invalid -> `4h` |
| `range` | `6h`, `12h`, `24h`, `3d`, `7d`, `30d`, `1y` | `24h` | Window length. Invalid -> `24h` |
| `before` | ISO timestamp | now | The window ends here (scrolling back). Unparseable -> ignored; a naive time is read in the HA local zone |

**Downsampling** (same thresholds as `ChartStateLog.get_entries`): window up to 3 days -> `"raw"` (one point per 30-minute slot); up to 30 days (`7d`, `30d`) -> `"hourly"` (mean of `actual`/`v4`/`v3` per local hour, `t` = hour start); longer (`1y`) -> `"daily"` (per local day). `stats` are always computed from the undownsampled slots. `heat_runs` is `[]` in `daily` mode.

**Units.** Internally everything is degrees F. At this boundary absolute temperatures (`actual`, `v4`, `v3`) go through `display_temp(..., 1 dp)` and errors (`*_mae`, `*_bias`, and the `v4`/`v3`/`flat`/`bias` of `gate` rows) through `display_delta(..., 2 dp)` (no 32 offset). `unit` is the symbol (`"°F"` / `"°C"`). The page appends it and does no conversion of its own.

**How a point is built.** The v4 value of a 30-minute slot is what v4 said `lead` before that slot (first write wins). `actual` is the chart-log reading of the chosen sensor nearest that slot (within 20 minutes; `indoor` for `_ca`, `indoor_sensors[entity]` otherwise). `v3` exists only for `sensor=_ca` and `lead=4h` (v3's archive is 4 h ahead, stored as chart-log `pred_indoor`). The "no change" baseline for the stats is the sensor reading `lead` before the slot.

**Merge rule (live archive + backtest file).** Live archive slots (`climate_advisor_v4_archive_<id>.json`) win. A point from the published backtest file (`climate_advisor_v4_backtest_<id>.json`, `points[sensor][lead]`) is used only for a 30-minute slot the live archive does not cover; its `actual`/`v3`/`flat` fill in only where the chart log no longer has a row. v4's live history exists only from the first release with the shadow (0.9.4); older ranges come from the backtest. `gate` and `gate_summary` come only from the backtest file.

**Response**

| Field | Meaning |
|---|---|
| `zone`, `sensor`, `lead`, `range`, `unit` | Echo of the resolved request |
| `sensors` | `[{id, name, role}]`: first `{id:"_ca", name:"Climate Advisor reading"}`, then the model's and chart-log sensors with a friendly name and a role (`thermostat's finer sensor`, `thermostat`, `bedroom sensor`, `room sensor`); entity ids found only in the archive/backtest are appended with an empty role |
| `start`, `end`, `downsample` | Window (ISO, local offset) and `raw`/`hourly`/`daily` |
| `points` | `[{t, actual?, v4, v3?}]` ascending; fields absent when unknown |
| `heat_runs` | `[[start, end], ...]` spans of consecutive chart rows with `hvac == "heating"` (page shades them) |
| `stats` | `{n, v4_mae, v3_mae, flat_mae, v4_bias, v3_bias}` over the window; bias = mean(predicted - actual), positive = ran warm; `null` where there is nothing to score |
| `model` | `{confidence: "none"|"low"|"ok", fitted_at, span_days}`; `confidence: "none"` with nulls when no model is stored |
| `gate` | Rows from the backtest file: `{zone, season: "heating"|"summer", lead, v4, v3, flat, bias, pass, scored, ...}`, passed through (errors converted). `[]` without a backtest |
| `gate_summary` | The backtest file's summary object (e.g. `ready`) or `null` |

Sample (trimmed; full fixture `tests/fixtures/prediction_accuracy_sample.json`):

```json
{"zone": "9a0c...", "unit": "°F",
 "sensors": [{"id": "_ca", "name": "Climate Advisor reading", "role": "the reading Climate Advisor uses"},
             {"id": "sensor.hallway_temp", "name": "Hallway", "role": "thermostat's finer sensor"}],
 "sensor": "_ca", "lead": "4h", "range": "3d",
 "start": "2026-10-31T04:00-07:00", "end": "2026-11-02T19:00-08:00", "downsample": "raw",
 "points": [{"t": "2026-10-31T04:00-07:00", "actual": 65.7, "v4": 64.6, "v3": 66.0}],
 "heat_runs": [["2026-10-31T06:30-07:00", "2026-10-31T06:45-07:00"]],
 "stats": {"n": 65, "v4_mae": 0.76, "v3_mae": 1.6, "flat_mae": 1.51, "v4_bias": 0.07, "v3_bias": 1.36},
 "model": {"confidence": "ok", "fitted_at": "2026-11-02T03:15:00-08:00", "span_days": 30},
 "gate": [{"zone": "Zone 1", "season": "heating", "lead": "4h", "v4": 0.76, "v3": 1.6, "flat": 1.51, "bias": 0.07, "pass": true, "scored": true}],
 "gate_summary": null}
```

Errors: 400 several zones loaded and no `zone`/`entry_id` given (body `{message, zone_selection_required: true, zones, zone_count}`, the zone-resolution Repairs issue is raised once); 503 Climate Advisor not loaded or an unknown `zone`. The payload is built in the executor (it reads the backtest file and walks the chart log); the view itself only calls `coordinator.v4_accuracy_inputs()` on the loop.

### Engines and debug keys for v4

- `GET /api/climate_advisor/engines` -> existing engine status plus `v4` = `coordinator.v4_model_summary()` (`api.py#L1370`).
- `GET /api/climate_advisor/automation_state` (debug state) -> `v4_model` = the same dict (`coordinator.py#L14883`).
- Keys (temperatures/errors in the user's unit; `unit` carries the symbol): `has_model`, `confidence`, `fitted_at`, `span_days`, `n_anchors`, `mae_by_lead` (`{"1h","4h","12h"}` training MAE), `primary`, `sensors`, `schema_version`, `unit`, `engine_available`, `mass` (hidden thermal-mass estimate), `fit_running`, `last_fit` (`{at, reason: nightly|first, seconds, ok}`), `last_cycle` (`{at, ms, predicted, archived, inputs}`), `archive_slots` (slots per sensor line). Failure yields `{has_model: false, confidence: "none", error: true}`. Debug visibility only; nothing consumes it for a decision.

### POST Endpoints

| URL | Purpose | Key response fields |
|---|---|---|
| `/api/climate_advisor/force_reclassify` | Force immediate day reclassification | `status`, `message` |
| `/api/climate_advisor/send_briefing` | Send today's briefing via notify service | `status`, `message` |
| `/api/climate_advisor/respond_suggestion` | Accept, dismiss, or feedback a learning suggestion | accept: `status`, `changes`; dismiss: `status`, `dismissed`; feedback-only: `status` |
| `/api/climate_advisor/cancel_override` | Cancel active manual temperature override | `status`, `message` |
| `/api/climate_advisor/resume_from_pause` | Resume automation from door/window pause | `status`, `message`, `restored_mode` |
| `/api/climate_advisor/cancel_fan_override` | Cancel active fan override | `status`, `message` |
| `/api/climate_advisor/toggle_automation` | Enable or disable automation engine | `status`, `automation_enabled`, `message` |
| `/api/climate_advisor/ai_investigate` | Run AI Investigator skill | Delegated — returns `coordinator.ai_skills.async_execute(...)` result dict |

## Sensitive Field Redaction

`ClimateAdvisorConfigView` applies a two-case redaction rule before serialising any config setting:

1. **`ai_api_key`** — carries `"sensitive": True` in `CONFIG_METADATA` (the only field that does). Redacted via the metadata check: `meta.get("sensitive")`.
2. **`notify_service`** — no metadata flag; redacted via a hard-coded name check: `if key == "notify_service"`.

Both cases substitute `"configured"` if a value is present, or `"not set"` if absent. The raw value is never included in the response. All other fields return their actual value.

## Config Display Transforms

Before returning config values, `ClimateAdvisorConfigView` applies these transforms:

| Transform | Fields affected | Rule |
|---|---|---|
| `seconds_to_minutes` | `sensor_debounce_seconds`, `manual_grace_seconds`, `automation_grace_seconds`, `override_confirm_seconds`, `welcome_home_debounce_seconds` | Integer value divided by 60 |
| List → count string | Any list-typed field | Replaced with `"N configured"` |
| Time → string | Any `time` object | `str(value)` |
| `temp_abs` (`display_transform` in `CONFIG_METADATA`, Issue #1048/#1050) | 10 absolute temperature settings: `comfort_heat`, `comfort_cool`, `setback_heat`, `setback_cool`, `sleep_heat`, `sleep_cool`, `threshold_hot`, `threshold_warm`, `threshold_mild`, `threshold_cool` | Stored °F; value converted with `temperature.display_temp()` into the user's `temp_unit` (1 decimal) and the entry gains `unit` (`"°C"` / `"°F"`). |
| `temp_delta` (same mechanism) | 6 "degrees of tolerance" settings: `max_setback_depth_f` and the five `comfort_deadband_{hot,warm,mild,cool,cold}_f` | Stored °F deltas; value converted with `temperature.display_delta()` (scale-only, 1 decimal -- the same conversion the options form uses, Issue #1063) into the user's `temp_unit`, and the entry gains `unit` (`"°C"` / `"°F"`). The "(°F)" suffix was dropped from these six labels (`const.py`) because the unit now travels with the value. |
| Missing key fallback | Any key absent from the config entry (added after entry was created) | Falls back to `meta.get("default")` |
| Temp-source label (`temp_source_label`) | `outdoor_temp_source`, `indoor_temp_source` | Friendly source label, plus the selected entity id (Issue #1032): `Dedicated sensor — sensor.x`; `… (<reason> — using weather service)` while the outdoor sensor is falling back; `Weather service — sensor.x selected but not in use` for a pre-#1032 mismatch. **Indoor (Issue #1033):** `indoor_temp_source` gets the same treatment from the zone's primary-sensor failure episode, with the consequence worded as pausing rather than falling back: `Dedicated sensor — sensor.x (<reason> — indoor decisions paused)`, `Dedicated sensor — none selected (indoor decisions paused)` when a dedicated source has no entity, and `Thermostat's built-in sensor (<reason> — indoor decisions paused)` for `climate_fallback`. **Companion sensor (Issue #1230 R1b):** while the thermostat's finer same-device temperature sensor is the indoor reading, `climate_fallback` shows `Thermostat (finer sensor — sensor.x)` (set by `thermostat_companion_sensor`, default `auto`; see [Bug D](08-COMPUTATION-REFERENCE.md#bug-d--thermostat-companion-sensor-issue-1230-r1b)). `<reason>` is the human label of `no_reading`/`unavailable`/`stale`/etc. (shared `SENSOR_REASON_LABELS`); the suffix is absent while the sensor is healthy. Entity id only (never a friendly name); the frontend HTML-escapes every settings value. |

**Where `unit` lives:** in each `settings` entry of the response, not in `CONFIG_METADATA` -- the unit depends on the installation's configured `temp_unit`, while `CONFIG_METADATA` is static (`tests/test_config_metadata.py` enforces that it carries only `display_transform`, never a unit). Only numeric (non-bool) values are transformed. The frontend appends `unit` via its single `unitSymbol()` helper.

**Units across the other endpoints (Issue #1050):** every temperature the status/learning endpoints and the sensors expose is built with `display_temp()` / `display_delta()` from `temperature.py` (absolute vs scale-only delta) instead of hand-rolled `round(from_fahrenheit(...))` copies: status `indoor_temp`, `outdoor_temp`, `ca_target_heat/cool` (resolved through `automation.resolve_active_band()` with the engine's TOU away/vacation expansion -- the band the thermostat is armed with; before the first classification, `fallback_edges_without_classification()` supplies a sleep/day-only placeholder, Issue #1080; since Issue #1345 assembled by `automation.active_thresholds_for()`, which also feeds the zone's **Heat Threshold** / **Cool Threshold** sensors, so the sensors and these fields always agree -- see [08 Computation Reference](08-COMPUTATION-REFERENCE.md) §5d), `nat_vent_*`, `trend_magnitude`; learning `comfort_range_low/high` and `tomorrow_plan`. The Compliance sensor's `comfort_range_low/high` attributes and the Day Type sensor's `trend_magnitude` attribute are now in the user's unit too (previously raw °F) -- see [temperature-conversion.md](temperature-conversion.md).

## Error Handling

| Code | When issued |
|---|---|
| 400 | Invalid parameter, missing or invalid JSON field, JSON parse failure |
| 403 | AI feature disabled in config, or investigator feature not enabled |
| 429 | Claude API rate limit exceeded |
| 500 | AI investigation skill returned a failure result |
| 503 | Coordinator not yet loaded, or AI client unavailable |

**Silent clamping**: Out-of-range numerics are never rejected with 400. The `hours` parameter in `event_log`, an unrecognised `range_str` in `chart_data`, and the `hours` field in AI activity requests are all silently clamped or reset to their defaults.

## Event Log Truncation Signal

**Decision (Issue #432):** Silent clamping of `hours=` (above) means a caller can request a window the stored event log doesn't actually reach back far enough to cover — before this fix, both `event_log` and `activity_record` would render whatever partial data existed with no signal that it was incomplete. Both endpoints now include two additive response fields, computed identically (each view derives `cutoff = now - hours` and compares it against the oldest stored entry's `time`):

| Field | Type | Meaning |
|---|---|---|
| `is_truncated` | bool | `True` if the event log's oldest stored entry is newer than the requested window's cutoff — i.e. the response cannot possibly cover the full requested window, regardless of cause (fresh install, recent restart, or the `EVENT_LOG_MAX_AGE_HOURS`/`EVENT_LOG_CAP` retention backstop having already evicted older entries). `False` otherwise. |
| `oldest_available` | string (ISO timestamp) or `null` | The `time` field of the oldest event currently in storage (`coordinator._event_log[0]`). `null` only when the event log is currently empty. |

`hours=` parameter handling itself is unchanged by this fix — this is a pure additive change to both responses; existing consumers that ignore the two new fields are unaffected. See [Event Log Provider](ai-skills-spec.md#event-log-provider) and the coordinator retention brief in [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) for how the underlying log's retention window is enforced.

## Coordinator Access Pattern

- **Prediction accuracy (Issue #1230 R1)**: the view calls the public `coordinator.v4_accuracy_inputs()` once on the event loop (shallow copies plus the archive object) and builds the payload in the executor, so no coordinator attribute is read off the loop.
- **Primary**: `coordinator.data.get(key, default)` — reads from the last 30-minute update cycle snapshot.
- **Freshness bypass**: `hass.states.get(climate_entity_id)` is called directly for live HVAC state to avoid serving stale data between update cycles.
- **Private attribute access**: `api.py` reads these coordinator attributes directly by name: `coordinator._last_briefing`, `coordinator._briefing_sent_today`, `coordinator._occupancy_mode`, `coordinator._event_log`, `coordinator._current_classification`, `coordinator._last_outdoor_temp`, `coordinator.automation_engine` (and the engine's private fields).

## Security Notes

- **AI status endpoint**: `status.pop("api_key", None)` is called explicitly before serialising the AI client status dict, regardless of whether the key is present.
- **Suggestion accept**: When a suggestion is accepted, only keys that appear in `CONFIG_METADATA.keys()` are persisted to the HA config entry. Unknown keys are applied to the in-memory config only and are not written to HA storage.
- **Config endpoint**: See [Sensitive Field Redaction](#sensitive-field-redaction) — no raw sensitive value is ever returned.
- **`current_setpoint`**: Returned as `null` when HVAC mode is off — withheld for UX clarity, not security.

## Invariants

1. Every view class sets `requires_auth = True` — no endpoint is reachable without a valid HA long-lived access token.
2. `ai_api_key` is never present in any API response — removed at the redaction layer before serialisation, not conditionally.
3. `notify_service` is never returned as a raw value — redacted by hard-coded name check independent of its metadata flags.
4. Unknown config keys submitted via suggestion-accept are never persisted to the HA config entry — the allowlist is `CONFIG_METADATA.keys()`.
5. All numeric range parameters are clamped, not rejected — no 400 is issued for an out-of-range numeric param.

## Disclosure Path

← Tier 2 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3 (not yet authored): per-endpoint request/response contracts (pre-conditions, post-conditions, field-level invariants)
↔ Siblings: [state-persistence.md](state-persistence.md), [temperature-conversion.md](temperature-conversion.md), [thermal-model-v3-spec.md](thermal-model-v3-spec.md), [grace-periods-spec.md](grace-periods-spec.md)
