<!-- Nav: ← Context: [AI Integration Brief](ai-integration.md) | → Detail: [ai_skills.py](../custom_components/climate_advisor/ai_skills.py) · [ai_skills_activity.py](../custom_components/climate_advisor/ai_skills_activity.py) · [ai_skills_investigator.py](../custom_components/climate_advisor/ai_skills_investigator.py) (source) | ↔ Related: [claude_api.py spec](claude-api-spec.md) (pending) -->

# AI Skill Framework — Territory Spec (Tier 3)

## Anchors

| Question | Short answer (≤2 sentences) | → Full answer |
|---|---|---|
| What fields must an `AISkillDefinition` provide, and which are optional? | `name`, `description`, `system_prompt`, `context_builder`, and `response_parser` are required. `fallback`, `triggered_by`, `config_key_model`, `config_key_max_tokens`, and `config_key_reasoning` are optional (defaulting to `None`, `"manual"`, and three `None`s). | [AISkillDefinition contract](#aiskill-definition-contract) |
| What happens when `register()` is called with a name that already exists? | A WARNING is logged and the existing entry is silently overwritten. No exception is raised. | [Registration — duplicate handling](#registration) |
| What does `registry.get()` return for an unknown skill name? | It returns `None`. The caller is responsible for handling the `None` case; `async_execute()` handles it by returning a standard error dict. | [Lookup contract](#lookup-contract) |
| What six keys does `async_execute()` always return, and what does each hold on the error path? | `success=False`, `source="error"`, `data={}`, `error="<message>"`, `input_context=""` (or the assembled context if available), `raw_response=""`. All six keys are always present regardless of which code path is taken. | [Return contract](#return-contract) |
| When is the fallback invoked instead of returning an error result? | The fallback is invoked when: (a) the context builder raises and a fallback is defined, or (b) `ClaudeResponse.success=False` or the response parser raises, and a fallback is defined. If no fallback is defined, `_error_result()` is returned instead. | [Fallback trigger conditions](#fallback-trigger-conditions) |
| What learning suggestion data does `activity_report` omit from its context? | Only the count of pending suggestions and the list of `suggestion_type` values are included. Raw suggestion text, evidence dicts, and confidence values are never sent to Claude by this skill. | [activity\_report — context omissions](#context-omissions) |
| How does the `hours` parameter affect the `activity_report` context, and when are historical daily summaries included? | `hours` (default 24) controls the EVENT LOG cutoff window. When `hours > 36`, a HISTORICAL DAILY SUMMARIES section is appended covering daily records for the full window. | [§ Event Log](#event-log) · [§ Historical daily summaries](#12h-window-vs-full-day-coverage) |
| When is the `activity_report` contradiction warning suppressed? | The `hvac_mode=off` + active `hvac_action` warning is suppressed when `fan_status` is any of `"active"`, `"running (manual override)"`, or `"running (untracked)"` — all cases where CA knowingly has the fan running. | [Cross-validation suppression](#cross-validation) |
| What sensitive config key does the `investigator` strip before including config in context? | `ai_api_key` is removed via `.pop()` on a copy of `coordinator.config` before the config block is serialised into the context string. No other config keys are redacted. | [investigator — config redaction](#config-redaction) |
| What does the `THERMAL OBSERVATION PIPELINE` section in the investigator context show? | Per-type committed/rejected counts with top reason codes, NEVER LEARNED flags when `k_active_cool` or `k_active_heat` is None, pending in-flight observations, and engine status. Added in v0.3.50. | [§Thermal Observation Pipeline Context](#thermal-observation-pipeline-context) |
| What rules govern the AI investigator's thermal pipeline health diagnosis? | Three THERMAL PIPELINE HEALTH rules in the system prompt: flag 0 committed HVAC obs as pipeline failure; flag `k_active_cool=NEVER LEARNED` with recent AC history as failure; flag ≥ 3 `new_session_started` abandonments as short-cycling. | [§THERMAL PIPELINE HEALTH System Prompt Rules](#thermal-pipeline-health-system-prompt-rules) |
| Does the registry cache skill responses? | No. The registry has no response cache. Every `async_execute()` call builds a fresh context, calls Claude, and parses a new response. | [Caching](#caching) |
| What invariant holds for `async_execute()` with respect to exceptions? | `async_execute()` never raises to the caller. Every exception inside context building, Claude call, parsing, and fallback invocation is caught and surfaced as a structured error dict. | [Invariants](#invariants) |
| How does a caller know a report was cut off before Claude finished writing it? | `ClaudeAPIClient` inspects the Anthropic API's `stop_reason` on every request; `truncated=True` (present only on the AI success path) iff `stop_reason == "max_tokens"`. A stream ending on `max_tokens` is otherwise indistinguishable from a normal completion — no exception is raised (Issue #420). | [Return contract](#return-contract) |

---

## Scope

This spec covers the AI skill framework — the registry, execution pipeline, and both built-in skill implementations.

- **File 1:** `custom_components/climate_advisor/ai_skills.py` — `AISkillRegistry`, `AISkillDefinition`, `async_execute()`, `_run_fallback()`, `_error_result()` (L1–L184)
- **File 2:** `custom_components/climate_advisor/ai_skills_activity.py` — `"activity_report"` skill: context builder, response parser, fallback, registration (L1–L347)
- **File 3:** `custom_components/climate_advisor/ai_skills_investigator.py` — `"investigator"` skill: seven-source context builder, response parser, fallback, registration (L1–L773)

**Does NOT cover:**
- Anthropic API transport, circuit breaker, rate limiting, cost estimation — covered by `claude_api.py` (spec pending at `docs/claude-api-spec.md`)
- HA service registration for `ai_activity_report` and `investigator` calls — owned by `coordinator.py`
- Report history storage (`store_ai_report()`, `get_ai_report_history()`) — owned by `coordinator.py`

---

## AISkillDefinition Contract

`AISkillDefinition` is a `@dataclass` with these fields:

| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `name` | `str` | Yes | — | Registry key; must be unique (duplicate triggers overwrite-with-warning) |
| `description` | `str` | Yes | — | Human-readable summary; exposed via `list_skills()` |
| `system_prompt` | `str` | Yes | — | Passed verbatim to `claude_client.async_request()` as the system prompt |
| `context_builder` | `Callable` | Yes | — | `async (hass, coordinator, **kwargs) → str`; raises are caught by `async_execute()` |
| `response_parser` | `Callable` | Yes | — | `(raw_response: str) → dict[str, Any]`; raises are caught and cause fallback invocation |
| `fallback` | `Callable \| None` | No | `None` | `(coordinator, **kwargs) → dict[str, Any]`; called when Claude fails or parse fails |
| `triggered_by` | `str` | No | `"manual"` | `"auto"` or `"manual"`; determines which daily rate-limit counter is charged |
| `config_key_model` | `str \| None` | No | `None` | Config key read from `coordinator.config` for per-skill model override |
| `config_key_max_tokens` | `str \| None` | No | `None` | Config key for per-skill max_tokens override (cast to `int` before use) |
| `config_key_reasoning` | `str \| None` | No | `None` | Config key for per-skill reasoning_effort override |

No type enforcement is performed on callable signatures at registration time.

---

## AISkillRegistry

### Registration

`registry.register(skill: AISkillDefinition) → None`

**Pre-conditions:** None — the registry accepts any `AISkillDefinition` at any time.

**Behavior:**
- If `skill.name` is not in `_skills`: stores the definition and logs DEBUG.
- If `skill.name` already exists: logs WARNING (`"AI skill '{}' already registered, replacing"`), then overwrites the entry. The old definition is discarded. No exception is raised.

**Post-condition:** `registry._skills[skill.name]` is the just-registered definition.

### Lookup Contract

`registry.get(name: str) → AISkillDefinition | None`

- Returns the `AISkillDefinition` stored under `name`, or `None` if no skill with that name is registered.
- Pure lookup — no side effects.

`registry.list_skills() → list[dict[str, str]]`

- Returns a list of `{"name": str, "description": str}` dicts, one per registered skill, in insertion order.

---

## Execution Pipeline

`registry.async_execute(name, hass, coordinator, claude_client, **kwargs) → dict`

### Pre-conditions

1. `name` is a string (may be any value; unknown names are handled gracefully).
2. `hass` is a live `HomeAssistant` instance — the context builder may call `hass.states.get()`.
3. `coordinator` has a `.config` attribute (dict or `None`) and a `.data` attribute (dict or `None`).
4. `claude_client` is a `ClaudeAPIClient` instance with a callable `async_request()` coroutine method.

### Execution Steps (in order)

1. **Skill lookup:** `self._skills.get(name)`. If `None`, return `_error_result(f"Unknown skill: {name}")` immediately.
2. **Context build:** `await skill.context_builder(hass, coordinator, **kwargs)`. If an exception is raised:
   - If `skill.fallback` is defined → call `_run_fallback(skill, coordinator, **kwargs)` and return.
   - Else → return `_error_result(f"Context builder failed for {name}")`.
3. **Per-skill config override resolution:** Read `coordinator.config` for `config_key_model`, `config_key_max_tokens`, `config_key_reasoning` if those keys are set on the skill definition. `max_tokens` is cast to `int` if the config value is not `None`. Missing config keys produce `None` overrides (which `async_request()` treats as "use global defaults").
4. **Claude call:** `await claude_client.async_request(system_prompt=skill.system_prompt, user_message=context, triggered_by=skill.triggered_by, model=..., max_tokens=..., reasoning_effort=...)`.
5. **Success path:** If `response.success=True`:
   - Call `skill.response_parser(response.content)`. If the parser raises, fall through to step 6.
   - Return `{success: True, source: "ai", data: parsed, error: None, input_context: context, raw_response: response.content, truncated: response.truncated}`.
   - `response.truncated` (and `response.stop_reason`) come from the Anthropic API's `stop_reason` field, which `ClaudeAPIClient` now inspects on every request (both `async_request` and `async_request_streaming`). `truncated=True` iff `stop_reason == "max_tokens"` — the model was cut off before finishing, not because it chose to stop. This is always logged (DEBUG for any stop_reason, WARNING when truncated) so a cut-off report is diagnosable instead of silently indistinguishable from a complete one (Issue #420).
6. **Fallback path:** `response.success=False` or parser raised. Log WARNING. If `skill.fallback` is defined → `_run_fallback(skill, coordinator, context=context, **kwargs)`. Else → `_error_result(response.error or "AI request failed...", input_context=context)`.

### Fallback Trigger Conditions

The fallback is invoked when any of the following occur and `skill.fallback is not None`:

| Trigger | Where in pipeline |
|---|---|
| `context_builder` raises any exception | Step 2 |
| `ClaudeResponse.success=False` (includes rate limit, circuit open, budget exceeded) | Step 6 |
| `response_parser` raises any exception | Step 5 → fall through to step 6 |

If `skill.fallback is None`, all three conditions instead return `_error_result()` with `source="error"`.

### Return Contract

Every `async_execute()` call returns exactly this shape:

```python
{
    "success": bool,          # True only on the AI success path
    "source": str,            # "ai" | "fallback" | "error"
    "data": dict,             # parsed skill output, fallback output, or {} on error
    "error": str | None,      # error message string; None on success
    "input_context": str,     # assembled context string; "" if context build failed early
    "raw_response": str,      # Claude's raw text; "" on failure/fallback
    "truncated": bool,        # True iff Claude's stop_reason was "max_tokens" (AI success path only)
}
```

The first six keys are always present. `truncated` is only present on the AI success path
(step 5) — fallback and error results omit it, and callers should treat a missing key as
`False`. `async_execute()` never raises.

### `_run_fallback()` contract

`_run_fallback(skill, coordinator, context="", **kwargs) → dict`

- Calls `skill.fallback(coordinator, **kwargs)`. Note: `context` is NOT passed to the fallback callable; it is stored in the return dict's `input_context` field only.
- On success: `{success: True, source: "fallback", data: <fallback_result>, error: None, input_context: context, raw_response: ""}`.
- If the fallback itself raises: logs EXCEPTION, returns `_error_result(f"Both AI and fallback failed for {skill.name}", input_context=context)`.

---

## Caching

The registry has no response cache. No memoisation, no TTL, no invalidation path. Every `async_execute()` call:
- Awaits a fresh `context_builder` call
- Makes a live Claude API request (subject to the `ClaudeAPIClient` circuit breaker and rate limits)
- Parses a new response

Response persistence (history storage, timestamps) is the responsibility of `coordinator.py`, not the registry.

---

## `activity_report` Skill

**Registered name:** `"activity_report"` · **triggered_by:** `"manual"` · **No per-skill config overrides**

### Context Builder

`async_build_activity_context(hass, coordinator, **kwargs) → str`

Assembles eleven to twelve labeled sections in fixed order (twelve when `hours > 36`). Data sources per section:

| Section label | Data source | Notes |
|---|---|---|
| `STATE CROSS-VALIDATION` | Computed inline (see [Cross-Validation](#cross-validation)) | Always present; emits `[OK]`, `[FLAG]`, or `[WARNING]` tags |
| `CLASSIFICATION` | `coordinator.data` + fresh runtime from `coordinator._today_record` + `coordinator._hvac_on_since` | `hvac_mode` and `current_temperature` read live from `hass.states.get(climate_entity_id)` |
| `AUTOMATION STATE` | `coordinator.data` | `ATTR_AUTOMATION_STATUS`, `ATTR_LAST_ACTION_*`, `ATTR_NEXT_AUTOMATION_*` |
| `OCCUPANCY` | `coordinator.data` | `ATTR_OCCUPANCY_MODE` |
| `FAN` | `coordinator.data` + `coordinator.config` | `ATTR_FAN_STATUS`, `fan_mode` from config |
| `CONTACT SENSORS` | `coordinator.data` | `ATTR_CONTACT_STATUS` |
| `LEARNING` | `coordinator.data` | See [Context omissions](#context-omissions) |
| `CONFIGURATION` | `coordinator.config` | Comfort temps, setback temps, wake/sleep/briefing times |
| `ACTIVE FEATURES` | `coordinator.config` | Boolean feature flags |
| `ACTIVE PREDICTION ENGINES` | `coordinator.learning.get_engine_status()` formatted by `_format_engine_status_for_ai()` | See [Active Prediction Engines](#active-prediction-engines) |
| `EVENT LOG` | `coordinator._event_log` filtered to last N hours (`kwargs.get("hours", 24)`) | See [Event Log](#event-log) |
| `MANUAL OVERRIDES TODAY` | `coordinator._today_record.override_details` + live `coordinator.automation_engine` state | See [Manual Overrides Today](#manual-overrides-today) |
| `HISTORICAL DAILY SUMMARIES` | `coordinator.learning` daily records for the requested window | **Conditional:** only present when `kwargs.get("hours", 24) > 36`; omitted for standard ≤36h reports |

**Fresh HVAC runtime** is computed as `_today_record.hvac_runtime_minutes + session_elapsed_minutes` where `session_elapsed` is computed from `coordinator._hvac_on_since` at call time. This makes the runtime accurate regardless of coordinator.data staleness (up to 30 min between coordinator update cycles).

### Context Omissions

The `LEARNING` section includes only:
- Count of pending suggestions (`len(raw_suggestions)`)
- List of `suggestion_type` values from each suggestion dict

**Never included:**
- Raw suggestion `text` field
- Suggestion `evidence` dict
- Confidence values or thresholds

This enforces invariant 7 from `ai-integration.md`: suggestion text is not sent to Claude by this skill.

### Event Log

`coordinator._event_log` is a ring-buffer capped at `EVENT_LOG_CAP` entries. The activity report filters to events in the last N hours where N = `kwargs.get("hours", 24)` (v0.3.55+; previously hardcoded 12h). The cutoff is `datetime.now(UTC) - timedelta(hours=hours)`. If more than 60 events match, the oldest are dropped and a `(... N older events omitted)` note is prepended. Each entry renders as:

```
  HH:MM — <event_type>: key=value key=value ...
```

Times are converted to HA local timezone for display. The list is chronological (oldest first within the window). Representative event types:
- `apply_classification` — automation applied a day classification setpoint
- `override_confirm_pending` — debounce window started for detected setpoint change
- `override_confirmed` — override accepted after 10-min confirmation; includes `confirmed_after_seconds`
- `manual_override_cleared` — override cleared; includes `was_mode` and `duration_seconds`
- `grace_expired` — grace period ended, automation may resume
- `sensor_opened` / `door_pause` — contact sensor events
- `incident_detected` — emitted by the coordinator's incident detector (not the automation engine); fields: `incident_class`, `incident_id`, `indoor_f`, `outdoor_f`, `hvac_mode`, `comfort_cool`, `comfort_heat`. At most one per incident class per 30-min cycle. The proactive variant (`setpoint_mode_inconsistency`) may appear at command time rather than post-cycle. See [Incident Classes](incident-classes.md) for the full class list.

If the event log is empty or no events fall in the window, the section shows `(no events in last 12h)`. Any exception during log parsing is caught and logged at WARNING; the section shows `(unavailable)` rather than failing the context build.

### Manual Overrides Today

Provides a structured per-override record using two sources:

**`coordinator._today_record.override_details`** — list of dicts appended each time a manual setpoint change is confirmed (distinct from automation commands). Each entry: `{time: "HH:MM", old_temp: float, new_temp: float, direction: "up"|"down", magnitude: float}`. This covers setpoint changes; for mode changes use the event log.

**Current override state from `coordinator.automation_engine`:**
- `_manual_override_active: bool` — is an override active right now?
- `_manual_override_time: str | None` — ISO timestamp (HA local TZ) when the current override was confirmed

Formatted output:
```
## MANUAL OVERRIDES TODAY
  Count:             3
  #1  17:02  72.0°F → 74.0°F  (+2.0°F, up)
  #2  18:44  72.0°F → 75.0°F  (+3.0°F, up)
  #3  21:10  72.0°F → 74.0°F  (+2.0°F, up)
  Current override:  active since 21:10, duration 47 min (ongoing)
```

If `override_details` is empty: `(no setpoint overrides recorded today)`. If no override is currently active: `Current override: none active`. Duration is computed as `(dt_util.now() - override_dt).total_seconds() / 60` rounded to nearest minute. Any exception is caught and logged at WARNING.

**`hours` window vs. full-day coverage:** The EVENT LOG window may exclude early-morning events for late-afternoon reports when `hours=24`. `override_details` is not window-filtered — it accumulates all day, so all setpoint override timestamps are always present in MANUAL OVERRIDES TODAY regardless of report time.

**Historical daily summaries (>36h reports, v0.3.55+):** When `hours > 36`, the context builder appends a `HISTORICAL DAILY SUMMARIES` section drawn from `DailyRecord` history. Each daily row includes: date, day type, HVAC runtime minutes, comfort violations minutes, manual override count, and bedtime setback depth (if applied). This provides multi-day context without requiring the investigator skill. The section is appended after MANUAL OVERRIDES TODAY and is omitted entirely when `hours ≤ 36`.

### Cross-Validation

Two flags are computed and inserted into `STATE CROSS-VALIDATION` before the Claude call:

**1. Contradiction warning:** fires when `hvac_mode == "off"` AND `hvac_action` is one of `{"heating", "cooling", "fan"}`.

Suppression condition: if `hvac_action == "fan"` AND `fan_status` is any of `"active"`, `"running (manual override)"`, or `"running (untracked)"` → no flag is emitted (CA knowingly activated the fan). For heating and cooling actions, no suppression applies — the warning always fires.

**2. Comfort band check (deadband-aware):** acquires thermostat swing values from `learning.get_thermal_model()` before the check — `swing_heat_f_display` and `swing_cool_f_display`. Falls back to `THERMAL_SWING_DEFAULT_F` (1.5°F) if the learning engine is unavailable or the model is not yet populated. Celsius conversion is applied when `temp_unit == "celsius"`.

Then attempts `float(current_temp)`, `float(comfort_heat)`, `float(comfort_cool)`. If all three parse successfully:
- `(comfort_heat - current_temp) > swing_heat` → `[FLAG] Indoor X°F < comfort_heat Y°F — below by N.M°F (deadband: D.D°F)`
- `(current_temp - comfort_cool) > swing_cool` → `[FLAG] Indoor X°F > comfort_cool Y°F — above by N.M°F (deadband: D.D°F)`
- Otherwise → `[OK] Indoor X°F is within comfort band [Y–Z°F]`

The strict `>` (not `>=`) means a shortfall exactly equal to the swing does NOT flag — this matches thermostat deadband semantics where the measured offset is within expected thermal noise. A 1°F shortfall against a 1.5°F learned swing should never produce a false alarm.

If any value fails `float()` conversion (e.g., `"unknown"`), the comfort band check is silently skipped. If `_swing_heat_f` or `_swing_cool_f` is not a valid float, the same try/except silently skips the check.

### Active Prediction Engines

`_format_engine_status_for_ai(engine_status: dict) → str`

Formats the `get_engine_status()` return value from `LearningEngine` as a multi-line string for the `ACTIVE PREDICTION ENGINES` context section.

**k_active_hvac shape exception:** All engines except `k_active_hvac` store their learned value directly in `engine_status["k_active_hvac"]["value"]` as a flat scalar. `k_active_hvac` is the exception — its `"value"` key is a `dict` with `"heat"` and `"cool"` sub-keys:

```python
# All other engines (flat scalar):
engine_status["k_passive"]["value"]           # float | None

# k_active_hvac only (nested dict):
engine_status["k_active_hvac"]["value"]["heat"]   # float | None
engine_status["k_active_hvac"]["value"]["cool"]   # float | None
```

**Active gate:** An engine is displayed as active when `value is not None` (or for k_active_hvac: either `heat is not None or cool is not None`). A `"since"` date is included when available; if the engine was active before date tracking was introduced (pre-v0.3.46), it displays as `"pre-v0.3.46"`.

### System Prompt Deduplication Rule

`_SYSTEM_PROMPT` includes a `DEDUPLICATION RULE` requiring each section to be exclusive:
- `SUMMARY`: current state only — no analysis, no decisions
- `TIMELINE`: chronological events built from the EVENT LOG — automation setpoint actions, manual overrides (with setpoint values from MANUAL OVERRIDES TODAY and durations), contact sensor events. Each override must state when it started, what setpoint was set, and how long it lasted ("ongoing" if still active). No "why" analysis.
- `DECISIONS`: explain WHY each action was taken — do NOT re-describe Timeline content
- `ANOMALIES`: deviations from expected behavior only — do NOT re-explain Decisions
- `DIAGNOSTICS`: subsystem health only — do NOT repeat anomalies or decisions

A one-line cross-reference (`"see Decisions"`) is acceptable; restating the same analysis verbatim is not.

### Response Parser

`parse_activity_response(raw_response: str) → dict[str, Any]`

Splits on `## HEADER` lines. Expected headers and their output keys:

| Claude header | Output key |
|---|---|
| `## SUMMARY` | `"summary"` |
| `## TIMELINE` | `"timeline"` |
| `## DECISIONS` | `"decisions"` |
| `## ANOMALIES` | `"anomalies"` |
| `## DIAGNOSTICS` | `"diagnostics"` |

**Malformed response handling:**
- Empty or `None` `raw_response` → all five keys default to `""`, no exception.
- Unrecognised `## HEADER` → logged at DEBUG, content until next known header is discarded.
- Partial response (some sections missing) → missing sections default to `""`, present sections are populated normally.
- The parser never raises.

**Output schema:**

```python
{
    "summary": str,
    "timeline": str,
    "decisions": str,
    "anomalies": str,
    "diagnostics": str,
}
```

### Fallback

`activity_fallback(coordinator, **kwargs) → dict[str, Any]`

Reads from `coordinator.data` only. Produces the same five-key output schema. Called when Claude is unavailable or the parser raises. Does not call Claude. Does not modify coordinator state.

---

## `investigator` Skill

**Registered name:** `"investigator"` · **triggered_by:** `"manual"` · **Per-skill config overrides:** `CONF_AI_INVESTIGATOR_MODEL`, `CONF_AI_INVESTIGATOR_MAX_TOKENS`, `CONF_AI_INVESTIGATOR_REASONING`

This is the only skill in the registry that uses per-skill config overrides.

### Context Builder

`async_build_investigator_context(hass, coordinator, **kwargs) → str`

Assembles seven numbered context blocks. Each block is wrapped in its own `try/except`. If a block fails, its section is replaced with `"  unavailable"` and assembly continues — a failure in one block never aborts the others.

| Block # | Section label | Data source |
|---|---|---|
| 1 | `CURRENT STATE` | `coordinator.data` + fresh HVAC runtime |
| 2 | `HVAC ENTITY` | `hass.states.get(climate_entity_id)` — `hvac_mode` and `current_temperature` |
| 3 | `LEARNING — COMPLIANCE SUMMARY` | `learning.get_compliance_summary()` |
| 3 | `LEARNING — THERMAL MODEL` | `learning.get_thermal_model()` |
| 3 | `LEARNING — WEATHER BIAS` | `learning.get_weather_bias()` |
| 3 | `LEARNING — ACTIVE SUGGESTIONS` | `learning.generate_suggestions()` — full suggestion text and evidence included |
| 3 | `LEARNING — LAST 14 DAILY RECORDS` | `learning._state.records[-14:]` — direct internal access |
| 3 | `THERMAL OBSERVATION PIPELINE` | `_build_thermal_pipeline_context(coordinator)` — per-type committed/rejected counts, top reason codes, pending observations, NEVER LEARNED flags (added v0.3.50, Issue #156) |
| 4 | `EVENT LOG` | `coordinator._event_log[-200:]` filtered to last N hours (`kwargs.get("hours", 48)`) |
| 5 | `RECENT AI ACTIVITY REPORTS` | `coordinator.get_ai_report_history()[-3:]` — timestamp and summary only |
| 6 | `CONFIGURATION` | `coordinator.config` copy with `ai_api_key` stripped |
| 7 | `CA OPERATIONAL DESIGN` | Hardcoded prose block (fan_status values, deadband, warm-day guard, natural vent, contradiction logic) |

### Thermal Observation Pipeline Context

`_build_thermal_pipeline_context(coordinator) → str`

Added in v0.3.50 (Issue #156). Builds the `=== THERMAL OBSERVATION PIPELINE ===` section appended at the end of block 3. Purpose: let the AI distinguish `k_active_cool=None because never tried` from `k_active_cool=None because every attempt failed` vs. `k_active_cool=None because pipeline bug silently discarded all observations`.

**Per-type rows:** For each obs_type (`hvac_heat`, `hvac_cool`, `passive_decay`, `fan_only_decay`, `ventilated_decay`, `solar_gain`), the section shows:
- Committed count / total attempts (committed + rejected)
- Top rejection reason code and occurrence count (or `"no rejections"`)
- `NEVER LEARNED — k_active_cool is None` flag when obs_type is `hvac_cool` and `k_active_cool` is `None`
- `NEVER LEARNED — k_active_heat is None` flag when obs_type is `hvac_heat` and `k_active_heat` is `None`

**Pending observations:** Any in-flight observations from `coordinator._pending_observations` are listed with type, phase (`active`/`post_heat`), elapsed minutes, and sample count.

**Engine status:** `get_engine_status()` output is appended via `_format_engine_status_for_ai()`.

**Section-local guard:** The entire call is wrapped in a `try/except` in `async_build_investigator_context`. On failure, the section reads `"  unavailable"` and assembly continues.

### THERMAL PIPELINE HEALTH System Prompt Rules

The investigator's `_SYSTEM_PROMPT` includes `THERMAL PIPELINE HEALTH rules:` that instruct the AI to:
- Flag `hvac_heat`/`hvac_cool` with 0 committed observations as a pipeline failure when HVAC has run — expected to learn within first few cycles under normal conditions
- Flag `k_active_cool=NEVER LEARNED` when AC has run in recent history as a pipeline failure; suggest checking rejection log and pending observations for `hvac_cool`
- Flag ≥ 3 `new_session_started` abandonments for an HVAC type as possible short-cycling thermostat (cycles too short to capture post-heat samples between 5-min ticks)

These rules make pipeline failures explicit rather than leaving them to be inferred from null values in the thermal model section.

**Appended after the seven blocks (not try/except guarded separately):**
- Version/release notes: last 5 entries from `RELEASE_NOTES` in `const.py`
- Behavioral invariant registry: all entries from `KNOWN_FIXES` in `const.py`, formatted with `[COVERED]` / `[NOT COVERED]` scope markers (Issue #144). The investigator system prompt instructs the AI to cross-check anomalies against this registry before hedging "could not verify."
- GitHub issues: fetched live from the GitHub API via `async_build_github_context()`; silently omitted (returns `""`) on any network error

**Optional focus:** `kwargs.get("focus", "")` is prepended as `=== INVESTIGATION FOCUS (USER-DIRECTED) ===` if non-empty.

### Config Redaction

The `CONFIGURATION` block is assembled from a `dict(coordinator.config or {})` copy. Before serialisation, `cfg.pop("ai_api_key", None)` is called. This is the only key stripped; all other config keys including `ai_model`, `ai_enabled`, and notification service names are included verbatim.

### Daily Records Access Pattern

Block 3's daily records section uses direct internal access:
```python
state_obj = getattr(learning, "_state", None)
records = getattr(state_obj, "records", None)
```
This bypasses the public `LearningEngine` API. The last 14 records are rendered with computed `window_rec` values (`"opened"` / `"not-opened"` / `"n/a"`) derived from `windows_recommended` and `windows_physically_opened` (with `windows_opened` as fallback field name).

### Response Parser

`parse_investigation_response(raw_text: str) → dict[str, Any]`

Splits on `## HEADER` lines. Expected headers and output keys:

| Claude header | Output key |
|---|---|
| `## INVESTIGATION SUMMARY` | `"summary"` |
| `## INCONGRUITIES FOUND` | `"incongruities"` |
| `## DATA QUALITY ISSUES` | `"data_quality"` |
| `## SYSTEM ERRORS / WARNINGS` | `"errors_warnings"` |
| `## HYPOTHESES` | `"hypotheses"` |
| `## RECOMMENDED ACTIONS` | `"recommended_actions"` |
| `## ASSUMPTIONS & CONFIDENCE` | `"assumptions"` |

`"full_text"` always holds the complete `raw_text` value, regardless of header parsing. The `_flush()` closure cannot overwrite `"full_text"` because that key is not in `_header_map`; `full_text` is explicitly restored after the loop.

**Malformed response handling:** same pattern as `parse_activity_response` — missing sections default to `""`, unknown headers are logged at DEBUG and discarded, parser never raises.

**Output schema:**

```python
{
    "summary": str,
    "incongruities": str,
    "data_quality": str,
    "errors_warnings": str,
    "hypotheses": str,
    "recommended_actions": str,
    "assumptions": str,
    "full_text": str,   # always populated; holds complete raw Claude response
}
```

### Fallback

`investigation_fallback(coordinator, **kwargs) → dict[str, Any]`

Deterministic scan without Claude. Checks:
- Event log for entries with "error" or "warning" in `type`, filtered to last `kwargs.get("hours", 48)` hours
- Last 14 daily records for `windows_recommended=True` AND `windows_opened=False` (non-compliant days)
- `get_compliance_summary()` cross-check: `window_compliance == 0.0` when a `low_window_compliance` suggestion exists
- `total_manual_overrides > 50` threshold check
- `frequent_overrides` suggestion evidence `override_count > 50` check

Returns the same 8-key schema as `parse_investigation_response`. `full_text` is `""` (no raw Claude response). `hypotheses` and `recommended_actions` note that AI was unavailable.

---

## Invariants

1. **`async_execute()` never raises.** All exceptions inside context building, Claude calls, parsing, and fallback invocation are caught internally and surfaced as structured error dicts.

2. **The return dict from `async_execute()` always has exactly six keys.** All code paths — `"ai"`, `"fallback"`, `"error"` — produce `{success, source, data, error, input_context, raw_response}`.

3. **Skills never modify coordinator state.** Context builders read from `coordinator.data`, `coordinator.config`, and coordinator internal attributes. No skill writes to any coordinator field, no skill calls any coordinator method that has side effects. The coordinator's state before and after a skill execution is identical.

4. **Duplicate registration overwrites silently.** The registry does not enforce unique names at a type level; duplicate registration is a WARNING, not an error. The last `register()` call wins.

5. **Learning suggestion text is not sent to Claude by `activity_report`.** Only count and `suggestion_type` list are included. This is enforced in `async_build_activity_context()` at context assembly time, not at parse time.

6. **`ai_api_key` is not sent to Claude by `investigator`.** The config copy is `.pop()`-cleaned before serialisation. The original `coordinator.config` is not mutated (a copy is made via `dict(coordinator.config or {})`).

7. **Investigator context build failures are section-local.** Each of the seven context blocks is wrapped in its own `try/except`. A failure marks that section as `"  unavailable"` but does not abort remaining sections.

8. **`parse_investigation_response()` always preserves `full_text`.** The loop's `_flush()` closure cannot overwrite `full_text` because it is not in `_header_map`; after the loop, `sections["full_text"] = raw_text` is re-assigned unconditionally.

---

## State Transitions

The registry itself is stateless with respect to skill execution (no execution queue, no locking). The only state mutation is `_skills: dict` which changes only on `register()`.

The execution pipeline has no persistent state. From the registry's perspective, each `async_execute()` call is independent — there is no concept of a "running" or "queued" execution.

---

## Error Conditions

| Failure | Handling | Caller receives |
|---|---|---|
| Unknown skill name | `_error_result("Unknown skill: {name}")` returned | `{success: False, source: "error", data: {}, error: "Unknown skill: ...", input_context: "", raw_response: ""}` |
| Context builder raises | Fallback invoked if defined; `_error_result()` otherwise | `{success: True/False, source: "fallback"/"error", ...}` |
| Claude API fails (`response.success=False`) | Fallback invoked if defined; `_error_result()` otherwise | `{success: True/False, source: "fallback"/"error", ...}` |
| Response parser raises | Fallback invoked if defined; `_error_result()` otherwise | `{success: True/False, source: "fallback"/"error", ...}` |
| Fallback raises | `_error_result("Both AI and fallback failed for {name}")` | `{success: False, source: "error", data: {}, error: "Both AI and fallback failed...", ...}` |
| Malformed Claude response (missing sections) | Parser returns dict with missing keys defaulted to `""` | `{success: True, source: "ai", data: {...sections...}}` — no error, partial data |

---

## Code Reference

- [`AISkillDefinition`](../custom_components/climate_advisor/ai_skills.py#L18) — dataclass definition
- [`AISkillRegistry.register()`](../custom_components/climate_advisor/ai_skills.py#L41) — registration with duplicate overwrite
- [`AISkillRegistry.get()`](../custom_components/climate_advisor/ai_skills.py#L48) — lookup returning `None` on miss
- [`AISkillRegistry.async_execute()`](../custom_components/climate_advisor/ai_skills.py#L56) — full execution pipeline
- [`_run_fallback()`](../custom_components/climate_advisor/ai_skills.py#L148) — fallback invocation with exception guard
- [`_error_result()`](../custom_components/climate_advisor/ai_skills.py#L174) — standard error dict builder
- [`async_build_activity_context()`](../custom_components/climate_advisor/ai_skills_activity.py#L61) — nine-section context builder
- [`parse_activity_response()`](../custom_components/climate_advisor/ai_skills_activity.py#L222) — five-section response parser
- [`activity_fallback()`](../custom_components/climate_advisor/ai_skills_activity.py#L276) — deterministic fallback
- [`register_activity_skill()`](../custom_components/climate_advisor/ai_skills_activity.py#L331) — wires activity_report into registry
- [`async_build_investigator_context()`](../custom_components/climate_advisor/ai_skills_investigator.py#L155) — seven-source context builder
- [`async_build_github_context()`](../custom_components/climate_advisor/ai_skills_investigator.py#L123) — live GitHub API fetch
- [`parse_investigation_response()`](../custom_components/climate_advisor/ai_skills_investigator.py#L537) — eight-key response parser
- [`investigation_fallback()`](../custom_components/climate_advisor/ai_skills_investigator.py#L598) — deterministic fallback scan
- [`register_investigator_skill()`](../custom_components/climate_advisor/ai_skills_investigator.py#L754) — wires investigator into registry with config key overrides
