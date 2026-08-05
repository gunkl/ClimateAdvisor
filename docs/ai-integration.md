<!-- Nav: ← Context: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → Detail: [claude_api.py](../custom_components/climate_advisor/claude_api.py) · [ai_skills.py](../custom_components/climate_advisor/ai_skills.py) · [ai_skills_investigator.py](../custom_components/climate_advisor/ai_skills_investigator.py) · [ai_skills_context.py](../custom_components/climate_advisor/ai_skills_context.py) | ↔ Related: [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) · [Computation Reference](08-COMPUTATION-REFERENCE.md) -->

# AI Integration — Architecture Brief (Tier 2)

**Issue #563 (this pass):** the former `"activity_report"` and `"investigator"` skills have
been merged into one `"investigator"` skill with two entry modes — silent/scheduled
narration (no `focus`) and on-demand investigation (`focus` supplied). The old
`ai_skills_activity.py` module is deleted; its genuinely non-redundant context (state
cross-validation, override/fan-ownership details, historical daily summaries) now lives as
context providers in `ai_skills_context.py`, registered in the same `ContextProviderRegistry`
the investigator already used. See [Issue #563](https://github.com/gunkl/ClimateAdvisor/issues/563)
for the full audit of what was ported vs. retired as redundant.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What stops the integration from hammering the Anthropic API during an outage? | A circuit breaker trips after `AI_CIRCUIT_BREAKER_THRESHOLD` consecutive failures, blocks all calls for `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS`, then probes once in half-open state before re-closing. | [Circuit Breaker](#circuit-breaker) |
| How are auto and manual AI requests counted separately, and what resets them? | `auto_requests_today` and `manual_requests_today` are distinct daily counters; `_reset_daily_counters_if_needed()` zeroes both at the top of every `async_request()` call when the date has changed. | [Rate Limiting](#rate-limiting) |
| What happens when a monthly budget is set and the cap is reached? | `_check_budget()` returns `ClaudeResponse(success=False, budget_exceeded=True)` and the API call is never made; the cap resets at calendar month boundary. Value `0` means no cap. | [Monthly Budget](#monthly-budget) |
| What is the fixed return shape from every skill execution, regardless of success or fallback? | Every `async_execute()` call returns a 6-key dict: `{success, source, data, error, input_context, raw_response}`. Source is `"ai"`, `"fallback"`, or `"error"`. | [Return Contract](#return-contract) |
| Is there still a separate "activity report" skill? | No (Issue #563) — it was merged into `"investigator"`. The dashboard's silent/scheduled report and the on-demand focus-driven investigation are now two entry modes of the same skill, same prompt, same context-provider registry. | [investigator](#investigator) |
| Does the investigator still have its own model/token/reasoning config overrides? | No (Issue #563) — it shares the single `ai_model`/`ai_reasoning_effort`/`ai_max_tokens` config used everywhere else. `CONF_AI_INVESTIGATOR_MODEL`/`_MAX_TOKENS`/`_REASONING` constants remain defined (unused) only so the historical v13→v14 config migration doesn't break old installs. | [investigator](#investigator) |
| Why did the KNOWN-FIXED ISSUES context section used to grow without bound? | `_fix_is_relevant()`'s escape-hatch rule matched any entry with a non-empty `scope_not_covered` field, which was mandatory on every entry — so all 169 entries always passed regardless of the intended version-scoping. Fixed by removing the field entirely and bounding by recency count instead. | [Known Fixes Context](#known-fixes-context) |
| What are the full pre/post/invariant contracts for `ClaudeAPIClient.async_request()` and the circuit breaker? | The Tier 3 spec covers the 5-row circuit breaker transition table, guard sequence for `async_request()`, `ClaudeResponse` mutual-exclusivity invariants, budget reset trigger, and all four retried exception types. | [Claude API Client — Territory Spec](claude-api-spec.md) |
| What are the full contracts for the skill registry, execution pipeline, and the registered skill? | The Tier 3 spec covers `AISkillRegistry` registration and lookup, the 6-step execution pipeline, return contract enforcement, the merged `investigator` skill's context/parse/fallback contracts, and caching behavior. | [AI Skills Framework — Territory Spec](ai-skills-spec.md) |

---

## Scope

**Owns:**
- All Anthropic API communication (`ClaudeAPIClient` in `claude_api.py`)
- Circuit breaker state machine, rate-limit counters, monthly budget accumulator, and retry logic
- Cost estimation per model and per request
- Persistence of stats (counters, monthly cost) across HA restarts
- AI skill registry: registration, lookup, and execution pipeline (`AISkillRegistry` in `ai_skills.py`)
- Skill definitions: `AISkillDefinition` blueprints including context builders, response parsers, and fallbacks
- Context assembly for the merged `"investigator"` skill (`ai_skills_investigator.py` orchestrates; individual context providers live in `ai_skills_context.py`)
- Cross-validation logic (HVAC contradiction check, comfort band flag) before each Claude call
- The deterministic fallback path when Claude is unavailable or returns an error

**Explicitly does NOT own:**
- Sensor entity for `sensor.climate_advisor_ai_status` — owned by `sensor.py`
- Report history storage (`get_investigation_report_history()`, `store_investigation_report()`) — owned by `coordinator.py`. The AI Activity Report skill/service and its separate `get_ai_report_history()`/`_ai_report_history` storage were retired entirely in Issue #578 (superseded by the deterministic, non-AI Activity Record).
- Thermal model computation — owned by `learning.py`
- Daily record persistence — owned by `coordinator.py` + `learning.py`
- REST API endpoints — owned by `api.py`

---

## Responsibilities

- Authenticate with the Anthropic API using the HA config-entry API key; recreate the client when the key changes via `update_config()`
- Enforce a circuit breaker: count consecutive failures, open the breaker on threshold, block calls while open, probe with one request in half-open state
- Enforce daily rate limits independently for auto-triggered and manual-triggered requests
- Enforce a separate investigator rate limit gated also on `CONF_AI_INVESTIGATOR_ENABLED` — this now gates only the on-demand/focus-driven call path (`ClimateAdvisorInvestigateView`), not skill registration itself, so the silent/narration mode stays available to anyone with AI enabled (Issue #563)
- Enforce a monthly cost cap; accumulate `estimated_cost` on every successful call; reset at calendar month boundary
- Retry failed API calls with exponential backoff (`AI_MAX_RETRIES` attempts); skip backoff after the final attempt
- Estimate cost per request from a model-prefix lookup table (`_MODEL_COSTS`)
- Support extended thinking by adding a reasoning block and forcing `temperature=1` when `reasoning_effort == AI_REASONING_HIGH`
- Persist all counters and monthly cost (`get_persistent_stats()` / `restore_persistent_stats()`) so stats survive HA reboots
- Maintain a capped request history deque (no raw content, no API key)
- Provide a skill registry: register, overwrite-with-warning, and execute skills by name
- Execute the skill pipeline: context build → config-override resolution → Claude call → response parse → fallback on failure
- Always return a fixed 6-key dict from `async_execute()`, never raise
- Assemble the merged `"investigator"` skill's context from the `ContextProviderRegistry` (current state, HVAC entity, learning, thermal pipeline, event log + timing correlations + override false-positives + restart history, activity timeline, state cross-validation, override details, daily summaries, AI report history, config, operational design, known-fixed issues, version notes, live GitHub issues); compute pre-call cross-validation flags
- Provide a deterministic fallback that returns the same output schema without calling Claude

---

## Components

| Module | Role |
|---|---|
| `claude_api.py` | `ClaudeAPIClient`: all Anthropic API access, circuit breaker, rate limits, budget, retry, cost estimation, persistence |
| `ai_skills.py` | `AISkillRegistry` + `AISkillDefinition`: skill registration, lookup, execution pipeline, return contract enforcement |
| `ai_skills_investigator.py` | The merged `"investigator"` skill: system prompt, response parser, deterministic fallback, thin context-assembly orchestrator |
| `ai_skills_context.py` | `ContextProviderRegistry` + every individual context provider (16, as of Issue #563) the investigator's context is assembled from, including the render/timeline functions and the state-cross-validation/override-details/daily-summaries providers ported from the retired `ai_skills_activity.py` |

---

## ClaudeAPIClient

### Circuit Breaker

The circuit breaker is a three-state machine stored in `_CircuitBreaker`:

| State | Meaning | Transition |
|---|---|---|
| `"closed"` | Normal operation; all calls proceed | → `"open"` after `AI_CIRCUIT_BREAKER_THRESHOLD` consecutive failures |
| `"open"` | All calls blocked; returns `ClaudeResponse(circuit_open=True)` immediately | → `"half_open"` after `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS` from `opened_at` |
| `"half_open"` | One probe call allowed | → `"closed"` (zeros `consecutive_failures`) on success; → `"open"` on failure |

`_check_circuit_breaker()` is called at the top of `async_request()`, before rate limiting, budget checking, or any network activity.

### Rate Limiting

Two independent daily counters track separate request populations:

| Counter | Triggered by | Config cap |
|---|---|---|
| `auto_requests_today` | `triggered_by="auto"` | `CONF_AI_AUTO_REQUESTS_PER_DAY` |
| `manual_requests_today` | `triggered_by="manual"` | `CONF_AI_MANUAL_REQUESTS_PER_DAY` |

A third counter, `_investigator_requests_today`, is checked only by `check_investigator_rate_limit()`, which also verifies `CONF_AI_INVESTIGATOR_ENABLED` before allowing investigator calls.

`_reset_daily_counters_if_needed()` runs at the top of every `async_request()`. It compares today's date to `_RateLimitCounters.counter_date`; if they differ, all three daily counters are zeroed and the date is updated.

### Monthly Budget

`_BudgetTracker` maintains a running `monthly_cost` and a `budget_month` marker.

- `_check_budget()` compares `monthly_cost` to `CONF_AI_MONTHLY_BUDGET`. If the cap is exceeded, it returns `ClaudeResponse(success=False, budget_exceeded=True)` and the API call is skipped entirely.
- Setting `CONF_AI_MONTHLY_BUDGET = 0` disables the cap (no check performed).
- On every successful request: `monthly_cost += response.estimated_cost`.
- At calendar month boundary (detected in `_check_budget()`): `monthly_cost` is reset to zero and `budget_month` is updated.

### Cost Estimation

`_MODEL_COSTS` is a dict keyed by model-name prefix substrings mapping to `(input_cost_per_1M, output_cost_per_1M)` tuples:

| Model prefix | Input ($/1M tokens) | Output ($/1M tokens) |
|---|---|---|
| `sonnet` | $3.00 | $15.00 |
| `opus` | $15.00 | $75.00 |
| `haiku` | $0.80 | $4.00 |

Match is by substring of the model name; unrecognized models default to Sonnet rates ($3.00/$15.00 per 1M tokens).

### Retry Policy

`_async_call_with_retry()` attempts up to `AI_MAX_RETRIES` calls. Retries are triggered on:
- `RateLimitError`
- `APITimeoutError`
- `APIError`
- any other `Exception`

Backoff formula: `AI_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))`. The sleep is skipped after the final attempt. A successful attempt returns immediately; consecutive failure increments the circuit breaker counter.

### Extended Thinking

When `reasoning_effort == AI_REASONING_HIGH`, `async_request()` injects a thinking block into the API call and forces `temperature=1` (required by the Anthropic API for extended thinking). This is the only code path that overrides the caller-supplied temperature.

### Authentication

The API key is read from `config.get(CONF_AI_API_KEY, "")` at client construction. The key is static — no token refresh. When `update_config()` detects a key change (by comparing old vs. new values), it tears down and recreates the `AsyncAnthropic` client. The key is never written to request history, log statements, or API responses.

### Persistence

`get_persistent_stats()` returns a dict of all counters (`auto_requests_today`, `manual_requests_today`, `_investigator_requests_today`, `counter_date`) and monthly cost (`monthly_cost`, `budget_month`). `restore_persistent_stats()` rehydrates them on coordinator startup, preserving rate-limit and budget state across HA reboots.

Request history is a `deque` capped at `AI_REQUEST_HISTORY_CAP`. Each entry records: timestamp, skill name, input tokens, output tokens, cost, latency, success, error. No raw prompt content. No API key fragments.

---

## AISkillRegistry

### Skill Registration

A skill is described by an `AISkillDefinition` dataclass:

```python
@dataclass
class AISkillDefinition:
    name: str
    description: str
    system_prompt: str
    context_builder: Callable  # async (hass, coordinator, **kwargs) → str
    response_parser: Callable  # (content: str) → dict
    fallback: Callable | None  # (coordinator, context, **kwargs) → dict; None = use _error_result()
    triggered_by: str  # "auto" | "manual" (default "manual")
    # optional per-skill config key overrides:
    config_key_model: str | None
    config_key_max_tokens: str | None
    config_key_reasoning: str | None
```

`registry.register(skill)` stores the definition under `skill.name`. If a skill with that name already exists, a warning is logged and the entry is overwritten. No type enforcement on callable signatures.

### Execution Pipeline

`async_execute(name, hass, coordinator, claude_client, **kwargs) → dict` runs the following steps in order:

1. Look up `AISkillDefinition` by `name`; return `_error_result("skill not found")` if missing
2. `await skill.context_builder(hass, coordinator, **kwargs)` → `context: str`; on exception, call `skill.fallback(...)` if defined and return, else return `_error_result()`
3. Resolve per-skill config overrides: read `coordinator.config[model_config_key]` etc. if the keys are set; fall back to global defaults
4. `await claude_client.async_request(skill.system_prompt, context, triggered_by=skill.triggered_by, model=..., max_tokens=..., reasoning_effort=...)`
5. On `ClaudeResponse.success=True`: `skill.response_parser(response.content)` → `data` dict; return `{success: True, source: "ai", data: data, ...}`
6. On `ClaudeResponse.success=False`: call `skill.fallback(coordinator, context=context, **kwargs)` if defined → return `{success: False, source: "fallback", ...}`; else return `_error_result()`

### Return Contract

Every `async_execute()` call returns exactly this shape, regardless of path taken:

```python
{
    "success": bool,
    "source": "ai" | "fallback" | "error",
    "data": dict,  # parsed skill output or fallback output
    "error": str | None,  # error message; None on success
    "input_context": str,  # the assembled context string sent to Claude
    "raw_response": str,  # Claude's raw text response; "" on failure
}
```

`async_execute()` never raises; all exceptions are caught and surfaced through `"error"`.

---

## Skills

### investigator (merged, Issue #563)

**Skill name:** `"investigator"` · **triggered_by:** `"manual"` · **No per-skill model overrides** — shares
the single `ai_model`/`ai_reasoning_effort`/`ai_max_tokens` config used everywhere else.

This is the only skill in the registry. It replaces the former `"activity_report"` and
`"investigator"` skills, which duplicated prompts, config, context assembly, and frontend
streaming code. Two entry modes share one prompt, one context-provider registry, one parser,
one fallback:

- **Silent / scheduled narration** — no `focus` kwarg. Reads as an occupant-facing activity
  readout (what happened, why, anything worth knowing).
- **On-demand investigation** — `focus` kwarg carries the user's described problem. The
  `focus` text is prepended to the assembled context as a user-directed investigation target.

#### Context Sources

Sixteen context providers are registered in `ai_skills_context.py`'s `ContextProviderRegistry`
(`ai_skills_investigator.py:async_build_investigator_context` is a thin orchestrator that calls
`registry.select(focus)` and concatenates the result). Each provider is wrapped in its own
`try/except`; a failure in one does not abort the others — the section is marked unavailable
and assembly continues.

| Provider | Data source |
|---|---|
| `current_state` | `coordinator.data` + fresh HVAC runtime |
| `hvac_entity` | `hass.states.get(climate_entity_id)` |
| `state_cross_validation` | HVAC mode/action contradiction check + comfort-band deadband/swing check (ported from the retired activity context, Issue #563) |
| `last_briefing` | Most recent daily briefing text |
| `learning` | Compliance summary, thermal model, weather bias, suggestions, recent daily records |
| `thermal_pipeline` | Per-obs-type rejection/commit counts, `NEVER LEARNED`/`***PIPELINE FAILURE***` markers, engine status |
| `event_log` | Last 200 event-log entries filtered to last N hours (`kwargs["hours"]`, default 168, clamped 1–720); event-type counts, `SYSTEM LOG RECORDS` (real captured WARNING+/ERROR log records via `log_capture.py`, Issue #578 — replaces an earlier check that only matched the substring "error"/"warning" in a CA event's `type` field), `TIMING CORRELATIONS` (manual events near known automation cycle periods), `KNOWN OVERRIDE FALSE POSITIVES` (Issue #205 pattern: `override_detected` within 60s of an automation event), `RESTART HISTORY` (restart count by cause — `user_restart`/`version_changed` filtered out as benign, only `cause=unknown` is noteworthy) |
| `activity_timeline` | Deterministic markdown event timeline table (ported from the retired activity context; never LLM-authored) |
| `override_details` | Setpoint override count/history/current-override-duration, Issue #321 stuck-grace critical warning, fan ownership transitions plus a fan override count (Issue #578) (ported, Issue #563) |
| `daily_summaries` | Historical multi-day trend summary, only populated when `hours > 36` (ported, Issue #563) |
| `config` | ~11 curated `coordinator.config` fields (comfort/setback temps, schedule, `ai_model`, `learning_enabled`) — not a full config dump |
| `operational_design` | Static prose block explaining fan_status values, deadband behavior, warm-day comfort guard, natural vent mode, contradiction suppression logic |
| `known_fixes` | `KNOWN_FIXES` entries bounded to the most recent `_KNOWN_FIXES_RECENT_COUNT` (15) plus any not-yet-deployed entry — rendered as the matching `RELEASE_NOTES` bullet, not the internal `title`/`scope_covered` engineering prose (Issue #563; see anchor above) |
| `version` | Last 5 versions' `RELEASE_NOTES` |
| `github` | Live open + closed GitHub issues (TTL-cached; trimmed to `number`/`title`/`state`/`labels` before caching, Issue #563), silently omitted on network error |

#### Output Schema

```python
{
    "summary": str,
    "incongruities": str,
    "data_quality": str,
    "errors_warnings": str,
    "hypotheses": str,
    "recommended_actions": str,
    "assumptions": str,
    "full_text": str,  # always holds complete raw Claude response
}
```

`full_text` is always populated; the other keys are section-split from that text. Missing
sections default to `""`. Every section's leading sentence is required (by prompt contract,
Issue #563) to be occupant-outcome language — the human-observable effect before the internal
state that caused it — with `[source: key, value: X]`-style citations demoted to a trailing
parenthetical rather than the subject of the sentence.

#### Fallback

The fallback is deterministic: scans the event log for errors in the last 48 hours, checks
window compliance issues, and counts overrides. Returns the same 8-key dict with
`source="fallback"`. Does not call Claude.

---

## Interfaces

Key public entry points called by external modules:

| Symbol | Module | Caller(s) | Purpose |
|---|---|---|---|
| `ClaudeAPIClient.async_request()` | `claude_api.py` | `ai_skills.py` (`async_execute`) | Single gate for all Anthropic API calls |
| `ClaudeAPIClient.update_config()` | `claude_api.py` | `coordinator.py` (options flow update) | Hot-reload config; recreates client on key change |
| `ClaudeAPIClient.get_persistent_stats()` / `restore_persistent_stats()` | `claude_api.py` | `coordinator.py` (startup/shutdown) | Persist counters and monthly cost across reboots |
| `ClaudeAPIClient.check_investigator_rate_limit()` | `claude_api.py` | `api.py` (`ClimateAdvisorInvestigateView`) | Gate the on-demand/focus-driven call path only — not skill registration (Issue #563) |
| `AISkillRegistry.register()` | `ai_skills.py` | `ai_skills_investigator.py` (module init, via `coordinator.py`) | Register the merged skill definition |
| `AISkillRegistry.async_execute()` / `async_execute_streaming()` | `ai_skills.py` | `api.py` (`ClimateAdvisorInvestigateView`) | Run the skill end-to-end |

---

## Data Structures

```python
@dataclass
class ClaudeResponse:
    success: bool
    content: str  # Claude's raw text; "" on failure
    input_tokens: int
    output_tokens: int
    estimated_cost: float  # USD; computed from _MODEL_COSTS
    latency_ms: int
    error: str | None
    rate_limited: bool
    circuit_open: bool
    budget_exceeded: bool


@dataclass
class _CircuitBreaker:
    state: str  # "closed" | "open" | "half_open"
    consecutive_failures: int
    opened_at: datetime | None  # set when state → "open"


@dataclass
class _RateLimitCounters:
    auto_requests_today: int
    manual_requests_today: int
    counter_date: date  # date of last reset


@dataclass
class _BudgetTracker:
    monthly_cost: float  # USD accumulated this calendar month
    budget_month: int  # calendar month (1–12) of current accumulation window


@dataclass
class AISkillDefinition:
    name: str
    description: str
    system_prompt: str
    context_builder: Callable  # async (hass, coordinator, **kwargs) → str
    response_parser: Callable  # (content: str) → dict
    fallback: Callable | None
    triggered_by: str  # "auto" | "manual"
    model_config_key: str | None
    max_tokens_config_key: str | None
    reasoning_config_key: str | None
```

**Persistence:** `get_persistent_stats()` / `restore_persistent_stats()` serialise all `_RateLimitCounters` and `_BudgetTracker` fields to/from the coordinator's state JSON. No separate file; written as part of the coordinator's atomic state save.

**Request history:** in-memory `deque` only; not persisted across reboots. Capped at `AI_REQUEST_HISTORY_CAP` entries.

---

## Invariants

1. **Circuit breaker is always checked first.** `async_request()` calls `_check_circuit_breaker()` before rate limiting, before budget checking, and before any network I/O. A `circuit_open=True` response is returned immediately with no side effects on counters.

2. **Daily counters reset at most once per calendar day.** `_reset_daily_counters_if_needed()` runs unconditionally at the top of `async_request()`; the reset is a no-op if `counter_date == today`.

3. **Budget cap blocks the API call; it never truncates an in-flight call.** `_check_budget()` returns a synthetic `ClaudeResponse(budget_exceeded=True)` before `_async_call_with_retry()` is invoked.

4. **`async_execute()` never raises.** All exceptions in context building, parsing, and fallback invocation are caught and surfaced as `{success: False, source: "error", error: "..."}`.

5. **The return dict from `async_execute()` always has exactly six keys.** All code paths (`"ai"`, `"fallback"`, `"error"`) produce the same key set.

6. **The API key is never recorded.** It is not written to request history entries, not logged in any log statement, and not included in any API response or sensor attribute.

7. **Learning suggestion text and evidence ARE sent to Claude.** Corrected during Issue #563 doc convergence — this invariant previously claimed suggestion text was filtered to count+type only, which was true for the retired `activity_report` skill but was never true for `investigator` (the survivor): `build_learning_context()`'s `ACTIVE SUGGESTIONS` section includes each suggestion's full `text` and `evidence` dict. No suggestion-level filtering is currently applied.

8. **Config block strips `ai_api_key` before inclusion.** `coordinator.config.pop("ai_api_key")` is called on a copy before the config is serialised into context (`build_config_context()`, which sends a curated ~11-field subset, not the full config).

9. **Request history deque is capped.** Entries beyond `AI_REQUEST_HISTORY_CAP` are evicted from the left; the deque never grows without bound.

10. **Extended thinking forces `temperature=1`.** Any caller-supplied temperature is overridden when `reasoning_effort == AI_REASONING_HIGH`; this is the only path that does so.

---

## Disclosure Path

← Tier 1: [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md)
← Tier 2 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3: [Claude API Client spec](claude-api-spec.md) · [AI Skills Framework spec](ai-skills-spec.md)
↔ Siblings: [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) · [Computation Reference](08-COMPUTATION-REFERENCE.md) · [State Persistence](state-persistence.md)
