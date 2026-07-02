<!-- Nav: ← [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → api.py | ↔ [AI Integration](ai-integration.md) -->

# REST API — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does api.py own and what does it explicitly not own? | It owns the HTTP view layer — routing, auth enforcement, input validation, and response serialisation. It does NOT own business logic; every action delegates to the coordinator or automation engine. | [Scope](#scope) |
| How does authentication work and are there any unauthenticated endpoints? | All 19 views set `requires_auth = True`. A HA long-lived access token is required for every endpoint; no unauthenticated endpoints exist. | [Authentication](#authentication) |
| Which config values are ever redacted and why? | Two fields are redacted: `ai_api_key` (via `"sensitive": True` in CONFIG_METADATA) and `notify_service` (hard-coded name check). Both return `"configured"` or `"not set"` — never the raw value. | [Sensitive Field Redaction](#sensitive-field-redaction) |
| What HTTP status codes does the API return and when? | 400 bad input, 403 feature disabled, 429 rate limit, 500 investigation failure, 503 coordinator/AI unavailable. Out-of-range numerics are silently clamped, not rejected. | [Error Handling](#error-handling) |
| How does api.py read coordinator state and why does it sometimes bypass the coordinator cache? | Primary reads use `coordinator.data.get(...)` with safe defaults. Live HVAC state is read via `hass.states.get(climate_entity_id)` to guarantee freshness past the 30-minute update cycle. | [Coordinator Access Pattern](#coordinator-access-pattern) |
| What security guardrails prevent sensitive data from leaking through the config or AI status endpoints? | `ClimateAdvisorConfigView` applies the two-case redaction rule. `ClimateAdvisorAIStatusView` calls `status.pop("api_key", None)` before serialising. Unknown suggestion-accept keys are applied in-memory only and never persisted to the HA config entry. | [Security Notes](#security-notes) |

## Scope

**Owns:**
- HTTP view registration for all 19 Climate Advisor REST endpoints
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

All 19 view classes set `requires_auth = True`. HA handles token validation before the view handler is invoked. No endpoint is reachable without a valid HA long-lived access token. There is no per-endpoint permission model beyond HA authentication.

## Endpoints

### GET Endpoints

| URL | Purpose | Key response fields |
|---|---|---|
| `/api/climate_advisor/status` | Current system snapshot | `day_type`, `trend_direction`, `trend_magnitude`, `hvac_mode`, `hvac_action`, `indoor_temp`, `outdoor_temp`, `unit`, `automation_status`, `occupancy_mode`, `fan_status`, `contact_status`, `manual_override_active`, `next_action`, `compliance_score` |
| `/api/climate_advisor/briefing` | Today's daily briefing text | `briefing`, `briefing_sent_today`, `verbosity` |
| `/api/climate_advisor/chart_data` | Temperature forecast chart data | Delegated to `coordinator.get_chart_data(range_str=..., before_ts=...)`. Query params: `range` (string, default `24h`) and `before_ts` (Unix ms, optional). When `before_ts` is absent the window ends at now (live mode); when present the window is anchored at that point for historical navigation. Response includes `target_band` time-series array, `historical_setpoint`, and `predicted_setpoint` arrays (Issue #151, #160). |
| `/api/climate_advisor/automation_state` | Full automation engine debug state | Delegated to `coordinator.get_debug_state()` |
| `/api/climate_advisor/learning` | Learning engine summary | `today_record`, `yesterday_record`, `tomorrow_plan`, `suggestions`, `compliance`, `comfort_range_low`, `comfort_range_high`, `unit` |
| `/api/climate_advisor/config` | Integration configuration | `settings` list of `{key, value, label, description, category}` — sensitive values redacted |
| `/api/climate_advisor/ai_status` | AI client health and budget | `status` (api_key removed), `recent_requests` (last 10) |
| `/api/climate_advisor/ai_reports` | AI activity report history | `reports` list from `coordinator.get_ai_report_history()` |
| `/api/climate_advisor/investigation_reports` | AI investigator report history | Direct return from `coordinator.get_investigation_report_history()` |
| `/api/climate_advisor/event_log` | Internal event log | `events`, `total`, `hours` — `hours` param silently clamped if out of range |

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
| `/api/climate_advisor/ai_activity` | Run AI Activity Report skill | Delegated — returns `coordinator.ai_skills.async_execute(...)` result dict |
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
| Missing key fallback | Any key absent from the config entry (added after entry was created) | Falls back to `meta.get("default")` |

## Error Handling

| Code | When issued |
|---|---|
| 400 | Invalid parameter, missing or invalid JSON field, JSON parse failure |
| 403 | AI feature disabled in config, or investigator feature not enabled |
| 429 | Claude API rate limit exceeded |
| 500 | AI investigation skill returned a failure result |
| 503 | Coordinator not yet loaded, or AI client unavailable |

**Silent clamping**: Out-of-range numerics are never rejected with 400. The `hours` parameter in `event_log`, an unrecognised `range_str` in `chart_data`, and the `hours` field in AI activity requests are all silently clamped or reset to their defaults.

## Coordinator Access Pattern

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
