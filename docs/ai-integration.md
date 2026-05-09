<!-- Nav: ← Context: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → Detail: [claude_api.py](../custom_components/climate_advisor/claude_api.py) · [ai_skills.py](../custom_components/climate_advisor/ai_skills.py) · [ai_skills_activity.py](../custom_components/climate_advisor/ai_skills_activity.py) · [ai_skills_investigator.py](../custom_components/climate_advisor/ai_skills_investigator.py) | ↔ Related: [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) · [Computation Reference](08-COMPUTATION-REFERENCE.md) -->

# AI Integration — Architecture Brief (Tier 2)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What stops the integration from hammering the Anthropic API during an outage? | A circuit breaker trips after `AI_CIRCUIT_BREAKER_THRESHOLD` consecutive failures, blocks all calls for `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS`, then probes once in half-open state before re-closing. | [Circuit Breaker](#circuit-breaker) |
| How are auto and manual AI requests counted separately, and what resets them? | `auto_requests_today` and `manual_requests_today` are distinct daily counters; `_reset_daily_counters_if_needed()` zeroes both at the top of every `async_request()` call when the date has changed. | [Rate Limiting](#rate-limiting) |
| What happens when a monthly budget is set and the cap is reached? | `_check_budget()` returns `ClaudeResponse(success=False, budget_exceeded=True)` and the API call is never made; the cap resets at calendar month boundary. Value `0` means no cap. | [Monthly Budget](#monthly-budget) |
| What is the fixed return shape from every skill execution, regardless of success or fallback? | Every `async_execute()` call returns a 6-key dict: `{success, source, data, error, input_context, raw_response}`. Source is `"ai"`, `"fallback"`, or `"error"`. | [Return Contract](#return-contract) |
| What context does the activity report build, and how does it guard against false contradiction alerts? | It assembles nine labeled sections from coordinator.data, live climate state, and config. A contradiction warning (`hvac_mode=off` but action in `{heating, cooling, fan}`) is suppressed when the CA fan is actively running. | [activity\_report](#activity_report) |
| What makes the investigator skill different from the activity report skill? | The investigator resolves seven independent context sources (including GitHub issues and CA design prose), uses per-skill model/token/reasoning overrides, and gates on a separate `_investigator_requests_today` counter. | [investigator](#investigator) |
| What are the full pre/post/invariant contracts for `ClaudeAPIClient.async_request()` and the circuit breaker? | The Tier 3 spec covers the 5-row circuit breaker transition table, guard sequence for `async_request()`, `ClaudeResponse` mutual-exclusivity invariants, budget reset trigger, and all four retried exception types. | [Claude API Client — Territory Spec](claude-api-spec.md) |
| What are the full contracts for the skill registry, execution pipeline, and both registered skills? | The Tier 3 spec covers `AISkillRegistry` registration and lookup, the 6-step execution pipeline, return contract enforcement, `activity_report` and `investigator` context/parse/fallback contracts, and caching behavior. | [AI Skills Framework — Territory Spec](ai-skills-spec.md) |

---

## Scope

**Owns:**
- All Anthropic API communication (`ClaudeAPIClient` in `claude_api.py`)
- Circuit breaker state machine, rate-limit counters, monthly budget accumulator, and retry logic
- Cost estimation per model and per request
- Persistence of stats (counters, monthly cost) across HA restarts
- AI skill registry: registration, lookup, and execution pipeline (`AISkillRegistry` in `ai_skills.py`)
- Skill definitions: `AISkillDefinition` blueprints including context builders, response parsers, and fallbacks
- Context assembly for the `"activity_report"` skill (`ai_skills_activity.py`)
- Context assembly for the `"investigator"` skill (`ai_skills_investigator.py`)
- Cross-validation logic (HVAC contradiction check, comfort band flag) before each Claude call
- Fallback paths for both skills when Claude is unavailable or returns an error

**Explicitly does NOT own:**
- HA service registration for `ai_activity_report`, `get_ai_report`, `clear_ai_reports` — owned by `coordinator.py` / `__init__.py`
- Sensor entity for `sensor.climate_advisor_ai_status` — owned by `sensor.py`
- Report history storage (`get_ai_report_history()`, `store_ai_report()`) — owned by `coordinator.py`
- Thermal model computation — owned by `learning.py`
- Daily record persistence — owned by `coordinator.py` + `learning.py`
- REST API endpoints — owned by `api.py`

---

## Responsibilities

- Authenticate with the Anthropic API using the HA config-entry API key; recreate the client when the key changes via `update_config()`
- Enforce a circuit breaker: count consecutive failures, open the breaker on threshold, block calls while open, probe with one request in half-open state
- Enforce daily rate limits independently for auto-triggered and manual-triggered requests
- Enforce a separate investigator rate limit gated also on `CONF_AI_INVESTIGATOR_ENABLED`
- Enforce a monthly cost cap; accumulate `estimated_cost` on every successful call; reset at calendar month boundary
- Retry failed API calls with exponential backoff (`AI_MAX_RETRIES` attempts); skip backoff after the final attempt
- Estimate cost per request from a model-prefix lookup table (`_MODEL_COSTS`)
- Support extended thinking by adding a reasoning block and forcing `temperature=1` when `reasoning_effort == AI_REASONING_HIGH`
- Persist all counters and monthly cost (`get_persistent_stats()` / `restore_persistent_stats()`) so stats survive HA reboots
- Maintain a capped request history deque (no raw content, no API key)
- Provide a skill registry: register, overwrite-with-warning, and execute skills by name
- Execute the skill pipeline: context build → config-override resolution → Claude call → response parse → fallback on failure
- Always return a fixed 6-key dict from `async_execute()`, never raise
- Assemble nine-section context for the `"activity_report"` skill; compute pre-call cross-validation flags
- Assemble seven-section context for the `"investigator"` skill; append version notes and live GitHub issues
- Provide deterministic fallbacks for both skills that return the same output schema without calling Claude

---

## Components

| Module | Role |
|---|---|
| `claude_api.py` | `ClaudeAPIClient`: all Anthropic API access, circuit breaker, rate limits, budget, retry, cost estimation, persistence |
| `ai_skills.py` | `AISkillRegistry` + `AISkillDefinition`: skill registration, lookup, execution pipeline, return contract enforcement |
| `ai_skills_activity.py` | `"activity_report"` skill: context builder, HVAC cross-validation, output schema parser, deterministic fallback |
| `ai_skills_investigator.py` | `"investigator"` skill: seven-source context builder, GitHub context, per-skill model overrides, deterministic fallback |

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
    context_builder: Callable        # async (hass, coordinator, **kwargs) → str
    response_parser: Callable        # (content: str) → dict
    fallback: Callable | None        # (coordinator, context, **kwargs) → dict; None = use _error_result()
    triggered_by: str                # "auto" | "manual" (default "manual")
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
    "data": dict,                  # parsed skill output or fallback output
    "error": str | None,           # error message; None on success
    "input_context": str,          # the assembled context string sent to Claude
    "raw_response": str,           # Claude's raw text response; "" on failure
}
```

`async_execute()` never raises; all exceptions are caught and surfaced through `"error"`.

---

## Skills

### activity_report

**Skill name:** `"activity_report"` · **triggered_by:** `"manual"` · **No per-skill model overrides**

#### Context Sources

The context builder assembles nine labeled sections in order:

| Section | Data source |
|---|---|
| STATE CROSS-VALIDATION | Computed locally before Claude call (see below) |
| CLASSIFICATION | `coordinator.data`: day type, trend direction/magnitude, hvac_mode, windows recommended |
| AUTOMATION STATE | `coordinator.data`: automation status, last action, next action |
| OCCUPANCY | `coordinator.data`: occupancy mode |
| FAN | `coordinator.data`: fan status |
| CONTACT SENSORS | `coordinator.data`: contact status |
| LEARNING | `coordinator.data`: learning suggestions (count + type list only — no suggestion text or evidence) |
| CONFIGURATION | `coordinator.config`: comfort temps, schedule times, feature flags |
| ACTIVE FEATURES | `coordinator.config`: enabled/disabled feature flags |

Additional data woven into context: fresh HVAC runtime from `coordinator._today_record` and live climate entity state from HA.

#### Cross-Validation

Before the Claude call, two flags are computed and inserted into the STATE CROSS-VALIDATION section:

1. **Contradiction warning:** `hvac_mode=off` but `hvac_action` is one of `{heating, cooling, fan}`. Suppressed (no flag emitted) when the CA fan status is `"active"`, `"running (manual override)"`, or `"running (untracked)"`.
2. **Comfort band check:** numeric comparison of `current_indoor_temp` vs `comfort_heat` (lower bound) and `comfort_cool` (upper bound). Emits `[FLAG]` if out of band, `[OK]` if within band.

#### Output Schema

The response parser splits Claude's response on `## HEADER` lines to produce:

```python
{
    "summary": str,
    "timeline": str,
    "decisions": str,
    "anomalies": str,
    "diagnostics": str,
}
```

Missing sections default to `""`.

#### Fallback

The fallback is deterministic: it reads the same `coordinator.data` fields and assembles a plain-text response in the same five-key output shape. No Claude call is made.

---

### investigator

**Skill name:** `"investigator"` · **triggered_by:** `"manual"` · **Has per-skill overrides:** `CONF_AI_INVESTIGATOR_MODEL`, `CONF_AI_INVESTIGATOR_MAX_TOKENS`, `CONF_AI_INVESTIGATOR_REASONING`

This is the only skill in the registry with per-skill config key overrides.

#### Context Sources

Seven context blocks are assembled independently. Each is wrapped in its own `try/except`; a failure in one block does not abort the others — the section is marked as unavailable and assembly continues.

| # | Block | Data source |
|---|---|---|
| 1 | Current state | `coordinator.data` + fresh HVAC runtime |
| 2 | Live HVAC entity state | `hass.states.get(climate_entity_id)` |
| 3 | Learning engine data | Compliance summary, thermal model, weather bias, suggestions, last 14 daily records |
| 4 | Event log | Last 200 event log entries filtered to last N hours (`kwargs["hours"]`, default 48); includes counts by type and extracted error/warning entries |
| 5 | Recent AI report history | Last 3 activity reports (timestamp + summary only) via `coordinator.get_ai_report_history()` |
| 6 | Configuration | All `coordinator.config` entries — `ai_api_key` stripped via `.pop()` before inclusion |
| 7 | CA operational design | Hardcoded prose block explaining: fan_status values, deadband behavior, warm-day comfort guard, natural vent mode, contradiction suppression logic |

**Additional context appended after the seven blocks:**
- Version/release notes (last 5 versions)
- Live GitHub issues (fetched from the GitHub API via HTTP; silently omitted on network error)

#### Optional Focus

`kwargs.get("focus", "")` is prepended to the assembled context as a user-directed investigation focus if present.

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
    "full_text": str,           # always holds complete raw Claude response
}
```

`full_text` is always populated; the other keys are section-split from that text. Missing sections default to `""`.

#### Fallback

The fallback is deterministic: scans the event log for errors in the last 48 hours, checks window compliance issues, and counts overrides. Returns the same 8-key dict with `source="fallback"`. Does not call Claude.

#### Cross-Skill Dependency

The investigator does NOT import or invoke `ai_skills_activity`. It reads the same raw coordinator data independently. It uses `coordinator.get_ai_report_history()` to include prior activity report summaries, enabling contradiction-checking between successive reports.

---

## Interfaces

Key public entry points called by external modules:

| Symbol | Module | Caller(s) | Purpose |
|---|---|---|---|
| `ClaudeAPIClient.async_request()` | `claude_api.py` | `ai_skills.py` (`async_execute`) | Single gate for all Anthropic API calls |
| `ClaudeAPIClient.update_config()` | `claude_api.py` | `coordinator.py` (options flow update) | Hot-reload config; recreates client on key change |
| `ClaudeAPIClient.get_persistent_stats()` / `restore_persistent_stats()` | `claude_api.py` | `coordinator.py` (startup/shutdown) | Persist counters and monthly cost across reboots |
| `ClaudeAPIClient.check_investigator_rate_limit()` | `claude_api.py` | `coordinator.py` (investigator service handler) | Gate investigator calls before dispatching |
| `AISkillRegistry.register()` | `ai_skills.py` | `ai_skills_activity.py`, `ai_skills_investigator.py` (module init) | Register a skill definition |
| `AISkillRegistry.async_execute()` | `ai_skills.py` | `coordinator.py` (service handlers) | Run a named skill end-to-end |

---

## Data Structures

```python
@dataclass
class ClaudeResponse:
    success: bool
    content: str                  # Claude's raw text; "" on failure
    input_tokens: int
    output_tokens: int
    estimated_cost: float         # USD; computed from _MODEL_COSTS
    latency_ms: int
    error: str | None
    rate_limited: bool
    circuit_open: bool
    budget_exceeded: bool

@dataclass
class _CircuitBreaker:
    state: str                    # "closed" | "open" | "half_open"
    consecutive_failures: int
    opened_at: datetime | None    # set when state → "open"

@dataclass
class _RateLimitCounters:
    auto_requests_today: int
    manual_requests_today: int
    counter_date: date            # date of last reset

@dataclass
class _BudgetTracker:
    monthly_cost: float           # USD accumulated this calendar month
    budget_month: int             # calendar month (1–12) of current accumulation window

@dataclass
class AISkillDefinition:
    name: str
    description: str
    system_prompt: str
    context_builder: Callable     # async (hass, coordinator, **kwargs) → str
    response_parser: Callable     # (content: str) → dict
    fallback: Callable | None
    triggered_by: str             # "auto" | "manual"
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

7. **Learning suggestion text is never sent to Claude by the activity report.** Only the count and type list are included in context; raw suggestion text and evidence are filtered out before context assembly.

8. **Investigator config block strips `ai_api_key` before inclusion.** `coordinator.config.pop("ai_api_key")` is called on a copy before the config is serialised into context.

9. **Request history deque is capped.** Entries beyond `AI_REQUEST_HISTORY_CAP` are evicted from the left; the deque never grows without bound.

10. **Extended thinking forces `temperature=1`.** Any caller-supplied temperature is overridden when `reasoning_effort == AI_REASONING_HIGH`; this is the only path that does so.

---

## Disclosure Path

← Tier 1: [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md)
← Tier 2 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3: [Claude API Client spec](claude-api-spec.md) · [AI Skills Framework spec](ai-skills-spec.md)
↔ Siblings: [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) · [Computation Reference](08-COMPUTATION-REFERENCE.md) · [State Persistence](state-persistence.md)
