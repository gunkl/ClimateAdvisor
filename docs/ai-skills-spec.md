<!-- Nav: ← Context: [AI Integration Brief](ai-integration.md) | → Detail: [ai_skills.py](../custom_components/climate_advisor/ai_skills.py) · [ai_skills_investigator.py](../custom_components/climate_advisor/ai_skills_investigator.py) · [ai_skills_context.py](../custom_components/climate_advisor/ai_skills_context.py) (source) | ↔ Related: [claude_api.py spec](claude-api-spec.md) (pending) -->

# AI Skill Framework — Territory Spec (Tier 3)

**Issue #563 (this pass):** the former `"activity_report"` skill (`ai_skills_activity.py`) is
retired — deleted entirely. The registry now holds exactly one skill, `"investigator"`, serving
both the silent/scheduled narration use case (formerly `activity_report`, no `focus` kwarg) and
the on-demand investigation use case (`focus` kwarg supplied). Its context is assembled from 16
providers registered in `ai_skills_context.py`'s `ContextProviderRegistry` — including three
ported from the retired activity context (state cross-validation, override/fan-ownership
details, historical daily summaries) and three new deterministic pre-computed sections that
replaced prompt-text rules the model previously had to re-derive (Issue #205 override
false-positive detection, restart-cause-filtered history, and a bounded `KNOWN_FIXES`
cross-check). See [Issue #563](https://github.com/gunkl/ClimateAdvisor/issues/563) for the full
audit.

## Anchors

| Question | Short answer (≤2 sentences) | → Full answer |
|---|---|---|
| What fields must an `AISkillDefinition` provide, and which are optional? | `name`, `description`, `system_prompt`, `context_builder`, and `response_parser` are required. `fallback`, `triggered_by`, `config_key_model`, `config_key_max_tokens`, and `config_key_reasoning` are optional (defaulting to `None`, `"manual"`, and three `None`s). | [AISkillDefinition contract](#aiskill-definition-contract) |
| What happens when `register()` is called with a name that already exists? | A WARNING is logged and the existing entry is silently overwritten. No exception is raised. | [Registration — duplicate handling](#registration) |
| What does `registry.get()` return for an unknown skill name? | It returns `None`. The caller is responsible for handling the `None` case; `async_execute()` handles it by returning a standard error dict. | [Lookup contract](#lookup-contract) |
| What six keys does `async_execute()` always return, and what does each hold on the error path? | `success=False`, `source="error"`, `data={}`, `error="<message>"`, `input_context=""` (or the assembled context if available), `raw_response=""`. All six keys are always present regardless of which code path is taken. | [Return contract](#return-contract) |
| Is there still a separate `activity_report` skill? | No (Issue #563). It's retired; `ai_skills_activity.py` is deleted. The merged `investigator` skill's silent/no-`focus` call is its functional replacement. | [investigator Skill](#investigator-skill) |
| Why did the KNOWN-FIXED ISSUES context section grow without bound before Issue #563? | `_fix_is_relevant()`'s first rule matched any entry with a non-empty `scope_not_covered` field, which was mandatory on every entry per the release checklist — so all 169 entries always passed regardless of the intended version-scoping. Fixed by removing the field and bounding by recency count (`_select_relevant_fixes()`, 15 most recent + any not-yet-deployed). | [Known Fixes Context](#known-fixes-context) |
| What is the Issue #205 override false-positive pattern, and how is it detected now? | An `override_detected` event within 60 seconds of an automation-initiated event is a known code-path false positive, not a real user override. `_build_known_override_false_positives()` computes this deterministically and hands the model the result — the prompt no longer asks the model to re-derive it from raw timestamps. | [Event Log Provider](#event-log-provider) |
| What sensitive config key does `investigator` strip before including config in context? | `ai_api_key` is removed via `.pop()` on a copy of `coordinator.config` before the config block is serialised into the context string. The config block itself is a curated ~11-field subset, not a full dump. | [Config Provider](#config-provider) |
| What does the `THERMAL OBSERVATION PIPELINE` section in the investigator context show? | Per-type committed/rejected counts with top reason codes, NEVER LEARNED flags when `k_active_cool` or `k_active_heat` is None, pending in-flight observations, and engine status. The system prompt now points at these pre-computed markers directly rather than restating the diagnostic rules as prose (Issue #563). | [§Thermal Observation Pipeline Context](#thermal-observation-pipeline-context) |
| Why do `observation_count_heat`/`_cool` in `LEARNING — THERMAL MODEL` not match `committed` in `THERMAL OBSERVATION PIPELINE`? | They measure different scopes, both by design: `observation_count_heat`/`_cool` are all-time cumulative counters (confidence-grading input, never trimmed); `committed` is a live, 90-day-windowed count capped at `THERMAL_OBS_CAP` (200). A large gap is expected on installs with history and is not data loss (Issue #586). | [§Thermal Observation Pipeline Context](#thermal-observation-pipeline-context) |
| Does the registry cache skill responses? | No. The registry has no response cache. Every `async_execute()` call builds a fresh context, calls Claude, and parses a new response. | [Caching](#caching) |
| What invariant holds for `async_execute()` with respect to exceptions? | `async_execute()` never raises to the caller. Every exception inside context building, Claude call, parsing, and fallback invocation is caught and surfaced as a structured error dict. | [Invariants](#invariants) |
| How does a caller know a report was cut off before Claude finished writing it? | `ClaudeAPIClient` inspects the Anthropic API's `stop_reason` on every request; `truncated=True` (present only on the AI success path) iff `stop_reason == "max_tokens"`. A stream ending on `max_tokens` is otherwise indistinguishable from a normal completion — no exception is raised (Issue #420). | [Return contract](#return-contract) |

---

## Scope

This spec covers the AI skill framework — the registry, execution pipeline, and the single
built-in skill implementation.

- **File 1:** `custom_components/climate_advisor/ai_skills.py` — `AISkillRegistry`, `AISkillDefinition`, `async_execute()`, `async_execute_streaming()`, `_run_fallback()`, `_error_result()`
- **File 2:** `custom_components/climate_advisor/ai_skills_investigator.py` — the merged `"investigator"` skill: system prompt, response parser, deterministic fallback, thin context-assembly orchestrator, registration
- **File 3:** `custom_components/climate_advisor/ai_skills_context.py` — `ContextProviderRegistry` + all 15 individual context providers, including the render/timeline functions and the providers ported from the retired `ai_skills_activity.py`

**Does NOT cover:**
- Anthropic API transport, circuit breaker, rate limiting, cost estimation — covered by `claude_api.py` (spec pending at `docs/claude-api-spec.md`)
- HA service registration for the `investigator` skill's calls — owned by `coordinator.py`. The separate `ai_activity_report` service and skill entry point were retired entirely in Issue #578 (superseded by the deterministic, non-AI Activity Record).
- Report history storage (`store_investigation_report()`, `get_investigation_report_history()`) — owned by `coordinator.py`

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

**Note:** The merged `investigator` skill sets all three config-key overrides to `None` — it shares the global `ai_model`, `ai_reasoning_effort`, and `ai_max_tokens` defaults (Issue #563).

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
    "success": bool,  # True only on the AI success path
    "source": str,  # "ai" | "fallback" | "error"
    "data": dict,  # parsed skill output, fallback output, or {} on error
    "error": str | None,  # error message string; None on success
    "input_context": str,  # assembled context string; "" if context build failed early
    "raw_response": str,  # Claude's raw text; "" on failure/fallback
    "truncated": bool,  # True iff Claude's stop_reason was "max_tokens" (AI success path only)
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

## `investigator` Skill

**Registered name:** `"investigator"` · **triggered_by:** `"manual"` · **No per-skill config overrides** (Issue #563 — shares the global `ai_model`/`ai_reasoning_effort`/`ai_max_tokens` config; `CONF_AI_INVESTIGATOR_MODEL`/`_MAX_TOKENS`/`_REASONING` constants remain defined only so the historical v13→v14 config migration doesn't break old installs — nothing reads them)

This is the sole skill in the registry — the former `activity_report` skill is retired. Two
entry modes share this one definition:
- **Silent / scheduled narration** — no `focus` kwarg (functional replacement for the retired
  `activity_report`)
- **On-demand investigation** — `focus` kwarg carries the user's described problem

### Context Builder

`async_build_investigator_context(hass, coordinator, **kwargs) → str`

A thin orchestrator: calls `ContextProviderRegistry.select(focus, narration=...)`
(`ai_skills_context.py`), sorted by `priority` and filtered either by a `focus` keyword or,
for the narration path, by a flat `priority <= 1` cutoff, then concatenates each provider's
output. Each provider is wrapped in its own `try/except`. If a provider fails, its section is
replaced with `"  unavailable"` and assembly continues — a failure in one provider never
aborts the others. As of Issue #578 there are 15 registered providers; treat the
registration list in `ai_skills_context.py` (search `_PROVIDER_REGISTRY.register`) as
authoritative if this table and the code ever disagree.

**Narration vs investigation scope (historical, Issue #563):** the skill's `narration=True`
kwarg caps providers to `priority <= 1` (current-state and recent-activity data only). Its only
callers — `ClimateAdvisorAIActivityView` and the `ai_activity_report` service — were retired in
Issue #578 along with the AI Activity Report feature, so no live code path currently sets
`narration=True`; the `priority` gating remains in `ai_skills_investigator.py` in case a future
skill entry point needs it. The on-demand `ai_investigate` (SSE) path never sets `narration`, so
an empty `focus` there means "audit everything" (all 15 providers). The `Narration`
column below reflects the `priority` cutoff, not a separately maintained list.

| Provider name | Priority | Narration | Section label | Data source |
|---|---|---|---|---|
| `current_state` | 0 | ✅ | `CURRENT STATE` | `coordinator.data` + fresh HVAC runtime |
| `hvac_entity` | 0 | ✅ | `HVAC ENTITY` | `hass.states.get(climate_entity_id)` — `hvac_mode` and `current_temperature` |
| `state_cross_validation` | 0 | ✅ | `STATE CROSS-VALIDATION` | HVAC mode/action contradiction check + comfort-band deadband/swing check — ported from the retired activity context (Issue #563) |
| `last_briefing` | 1 | ✅ | `LAST BRIEFING` | `coordinator._last_briefing` — the most recently rendered daily briefing text, verbatim |
| `learning` | 1 | ✅ | `LEARNING — *` (5 sub-sections) | Compliance summary, thermal model, weather bias, active suggestions (full text + evidence, unfiltered), last N daily records |
| `thermal_pipeline` | 1 | ✅ | `THERMAL OBSERVATION PIPELINE` | Per-type committed/rejected counts, top reason codes, pending observations, `NEVER LEARNED` / `*** PIPELINE FAILURE ***` markers |
| `event_log` | 1 | ✅ | `EVENT LOG` + `SYSTEM LOG RECORDS` + `TIMING CORRELATIONS` + `KNOWN OVERRIDE FALSE POSITIVES` + `RESTART HISTORY` | `coordinator._event_log[-200:]` + `log_capture` ring buffer, filtered to last N hours (`kwargs.get("hours", 168)`, clamped 1–720); see [Event Log Provider](#event-log-provider) |
| `activity_timeline` | 1 | ✅ | `ACTIVITY TIMELINE` | Deterministic markdown event timeline table — ported from the retired activity context (Issue #563); never LLM-authored |
| `override_details` | 1 | ✅ | `MANUAL OVERRIDES TODAY` + `FAN OWNERSHIP HISTORY` | Override count/history/current-duration, Issue #321 stuck-grace critical warning, fan ownership transitions — ported (Issue #563) |
| `daily_summaries` | 2 | ❌ | `HISTORICAL DAILY SUMMARIES` | Only populated when `hours > 36` — ported (Issue #563) |
| `config` | 2 | ❌ | `CONFIGURATION` | See [Config Provider](#config-provider) |
| `operational_design` | 3 | ❌ | `CA OPERATIONAL DESIGN` | Static prose block (fan_status values, deadband, warm-day guard, natural vent, contradiction logic) |
| `known_fixes` | 3 | ❌ | `KNOWN-FIXED ISSUES` | See [Known Fixes Context](#known-fixes-context) |
| `version` | 3 | ❌ | `RUNNING VERSION` + `RECENT RELEASE NOTES` | Last 5 versions with a `user_summary`, streamed from `fix_history.jsonl` via `fix_history.py` (Issue #702; formerly `RELEASE_NOTES` in `const.py`) |
| `github` | 4 | ❌ | `GITHUB REPOSITORY` + `RECENT GITHUB ISSUES` | Live open + closed GitHub issues (TTL-cached; trimmed to `number`/`title`/`state`/`labels` before caching — Issue #563); silently omitted on network error |

**Optional focus:** `kwargs.get("focus", "")` is prepended as `=== INVESTIGATION FOCUS (USER-DIRECTED) ===` if non-empty. Never combined with `narration=True` in practice.

### Thermal Observation Pipeline Context

`build_thermal_pipeline_context(hass, coordinator, **kwargs) → str` (`ai_skills_context.py`)

Builds the `=== THERMAL OBSERVATION PIPELINE ===` section. Purpose: let the AI distinguish `k_active_cool=None because never tried` from `k_active_cool=None because every attempt failed` vs. `k_active_cool=None because pipeline bug silently discarded all observations`.

**Per-type rows:** For each obs_type (`hvac_heat`, `hvac_cool`, `passive_decay`, `fan_only_decay`, `ventilated_decay`, `solar_gain`), the section shows:
- Committed count / total attempts (committed + rejected)
- Top rejection reason code and occurrence count (or `"no rejections"`)
- `NEVER LEARNED — k_active_cool is None` flag when obs_type is `hvac_cool` and `k_active_cool` is `None`
- `NEVER LEARNED — k_active_heat is None` flag when obs_type is `hvac_heat` and `k_active_heat` is `None`
- `*** PIPELINE FAILURE INDICATOR ***` when 0 committed HVAC observations exist despite non-zero rejections

**Committed/rejected scope (Issue #586):** `committed` here is a live count of `thermal_observations`, windowed to 90 days and hard-capped at `THERMAL_OBS_CAP` (200); `rejected` is a live count of `_rejection_log[obs_type]`, capped at 100 per type (FIFO-evicted, rendered as `"100+ (capped)"` once at the cap). Both are intentionally different in scope from `observation_count_heat`/`observation_count_cool` in the `LEARNING — THERMAL MODEL` section, which are all-time cumulative counters that drive confidence grading and are never trimmed. A large gap between the two (e.g. cumulative 24 vs. windowed 0 committed) is expected on installs with enough history and does not indicate lost data — the section now carries an explicit NOTE to this effect rather than leaving the two numbers unexplained.

**Pending observations:** Any in-flight observations from `coordinator._pending_observations` are listed with type, phase (`active`/`post_heat`), elapsed minutes, and sample count.

**Engine status:** `get_engine_status()` output is appended via `format_engine_status_for_ai()`.

**System prompt no longer restates these rules (Issue #563).** The `THERMAL PIPELINE HEALTH rules:` prose block that used to ask the model to re-derive pipeline health from raw counts has been deleted; the prompt now instructs the model to trust the `***`/`NEVER LEARNED`/`NOTE:` markers this provider already computes, rather than re-deriving them.

### Event Log Provider

`build_event_log_context(hass, coordinator, **kwargs) → str` (`ai_skills_context.py`)

Assembles five sub-sections:

1. **`EVENT LOG`** (from `coordinator._event_log`) — event-type counts only, last N hours. Prior to Issue #578 this section also tried to extract "error"/"warning" entries by checking whether a CA event's `type` string happened to contain those substrings (coincidental naming, not severity — CA event-log entries have no severity field) — that check is now `SYSTEM LOG RECORDS` below.
1b. **`SYSTEM LOG RECORDS`** (Issue #578, `log_capture.py`) — real WARNING+/ERROR Python `LogRecord`s captured by a `logging.Handler` attached to the `custom_components.climate_advisor` logger namespace at integration setup (see `log_capture.install()` in `__init__.py`), filtered to the last N hours. Captures every existing `_LOGGER.warning()`/`.error()` call site in the package automatically.
2. **`TIMING CORRELATIONS`** — manual events whose delay from a prior automation event matches a known automation cycle period (30/90/5/10 min ± 2 min tolerance) — suggests the "manual" event may actually be automation-caused.
3. **`KNOWN OVERRIDE FALSE POSITIVES`** (Issue #563) — `override_detected` events within 60 seconds of an automation-initiated event (e.g., `nat_vent_*`, `classification_applied`, `grace_started`). Distinct from Timing Correlations above — a deterministic ≤60s match, not a cycle-period match.
4. **`RESTART HISTORY`** (Issue #563) — `_build_restart_summary()` breaks down `system_restarted` events by `cause` (already computed by `coordinator.py`'s restart classification, Issue #403/#413: `user_restart`/`version_changed`/`unknown`). Only `cause=unknown` restarts are presented as noteworthy; benign restarts are never narrated as problems — this closes the "6 restarts today" hallucination class, which previously happened because only a raw count was visible with no cause breakdown.

### Known Fixes Context

`build_known_fixes_context(hass, coordinator, **kwargs) → str` (`ai_skills_context.py`)

**Migration rationale:** Prior releases stored `RELEASE_NOTES`/`KNOWN_FIXES` in `const.py`, which made up 91% of the file and was imported on every installation regardless of whether AI was enabled (Issue #702). The data now lives in `fix_history.jsonl` and is streamed on-demand via `fix_history.py`.

**Ranking and relevance:** `build_known_fixes_context()` calls `async_search_fix_history()`, which ranks by relevance to the investigation's `focus` keyword (when given) rather than recency alone. Always-included entries: not-yet-deployed fixes (`version_fixed > current`) + the 15 most recent by count. A matching fix from three releases ago outranks an unrelated fix from last release (the old recency-only window couldn't do this).

**Rendering fallback:** Each entry renders its `user_summary` field when present, falling back to `title` — internal refactors with no occupant-facing outcome omit the summary.

### Config Provider

`build_config_context(hass, coordinator, **kwargs) → str` (`ai_skills_context.py`)

Assembled from `dict(coordinator.config or {})`. Before serialisation, `cfg.pop("ai_api_key", None)` is called. **This is a curated ~11-field subset** (`comfort_heat`, `comfort_cool`, `setback_heat`, `setback_cool`, `wake_time`, `sleep_time`, `briefing_time`, `ai_enabled`, `ai_model`, `learning_enabled`), not a full config dump — an earlier draft of this doc/investigation incorrectly described it as a full dump; verified against source during the Issue #563 pass.

### Daily Records Access Pattern

The `learning` provider's daily records sub-section uses direct internal access:
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

**Malformed response handling:** missing sections default to `""`, unknown headers are logged at DEBUG and discarded, parser never raises.

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
    "full_text": str,  # always populated; holds complete raw Claude response
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

5. **Learning suggestion text and evidence ARE sent to Claude.** Corrected during Issue #563 doc convergence — this invariant previously claimed suggestion text was omitted, which was true only for the retired `activity_report` skill and was never true for `investigator` (the survivor): the `learning` provider's `ACTIVE SUGGESTIONS` sub-section includes each suggestion's full `text` and `evidence`. No suggestion-level filtering is currently applied.

6. **`ai_api_key` is not sent to Claude.** The `config` provider's config copy is `.pop()`-cleaned before serialisation. The original `coordinator.config` is not mutated (a copy is made via `dict(coordinator.config or {})`).

7. **Investigator context build failures are provider-local.** Each of the 15 registered context providers is wrapped in its own `try/except`. A failure marks that provider's section as `"  unavailable"` but does not abort the others.

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
- [`async_build_investigator_context()`](../custom_components/climate_advisor/ai_skills_investigator.py) — thin orchestrator calling `ContextProviderRegistry.select(focus)`
- [`parse_investigation_response()`](../custom_components/climate_advisor/ai_skills_investigator.py) — seven-section + `full_text` response parser
- [`investigation_fallback()`](../custom_components/climate_advisor/ai_skills_investigator.py) — deterministic fallback scan
- [`register_investigator_skill()`](../custom_components/climate_advisor/ai_skills_investigator.py) — registers the sole skill, no per-skill config overrides
- [`ContextProviderRegistry`](../custom_components/climate_advisor/ai_skills_context.py) — provider registration, priority sort, `focus`-tag filtering (`select()`)
- [`build_event_log_context()`](../custom_components/climate_advisor/ai_skills_context.py) — `EVENT LOG` + `TIMING CORRELATIONS` + `KNOWN OVERRIDE FALSE POSITIVES` + `RESTART HISTORY`
- [`_build_known_override_false_positives()`](../custom_components/climate_advisor/ai_skills_context.py) — Issue #205 deterministic false-positive detector (Issue #563)
- [`_build_restart_summary()`](../custom_components/climate_advisor/ai_skills_context.py) — restart-cause-filtered history (Issue #563)
- [`search_records()`](../custom_components/climate_advisor/fix_history.py) — relevance-ranked + not-yet-deployed fix-history selection, streamed from `fix_history.jsonl` (Issue #702; replaces the removed `_select_relevant_fixes()`/`_release_note_bullet()`, Issue #563)
- [`build_state_cross_validation_context()`](../custom_components/climate_advisor/ai_skills_context.py) — ported from the retired activity context (Issue #563)
- [`build_override_details_context()`](../custom_components/climate_advisor/ai_skills_context.py) — ported, includes Issue #321 stuck-grace detection (Issue #563)
- [`build_daily_summaries_context()`](../custom_components/climate_advisor/ai_skills_context.py) — ported, `hours > 36` only (Issue #563)
- [`build_activity_timeline_context()`](../custom_components/climate_advisor/ai_skills_context.py) — deterministic timeline table, ported (Issue #563)
