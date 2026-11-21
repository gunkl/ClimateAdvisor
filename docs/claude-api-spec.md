<!-- Nav: ← Context: [AI Integration Brief](ai-integration.md) | → Detail: [claude_api.py](../custom_components/climate_advisor/claude_api.py) | ↔ Related: [ai-skills-spec.md](ai-skills-spec.md) -->

# Claude API Client — Territory Spec (Tier 3)

**Issue #1336:** `ClaudeAPIClient` is now the install's AI client for both providers. The main
transport is Home Assistant's AI Task ([AI Task Transport](#ai-task-transport-issue-1336));
the Anthropic transport described in most of this spec is **built-in Claude, deprecated**,
kept until a removal release the owner has not named yet. When it is removed this spec
becomes the AI client spec.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What transitions the circuit breaker from `"open"` to `"half_open"`, and how many probe requests are allowed? | The breaker transitions to `"half_open"` when `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS` (300 s) have elapsed since `opened_at`; exactly one probe is allowed through because `_check_circuit_breaker()` returns `True` for `"half_open"` every time it is called — the single-probe guarantee is enforced by the transition to `"closed"` or back to `"open"` after the result arrives. | [Circuit Breaker State Machine](#circuit-breaker-state-machine) |
| What does `async_request()` return when the circuit is open, and does it update any counters? | It returns `ClaudeResponse(success=False, circuit_open=True)` immediately. No rate-limit counter, budget accumulator, error counter, or request history entry is updated. | [async\_request() Contract](#async_request-contract) |
| When is `budget_exceeded=True` compatible with `success=True` on a `ClaudeResponse`? | Never. `budget_exceeded=True` is only set in the pre-flight synthetic response returned before the API is called; `success` is always `False` in that response. All successful API responses set `success=True` and leave `budget_exceeded` at its default `False`. | [ClaudeResponse Mutual-Exclusivity Invariant](#clauderesponse-data-class) |
| How does the monthly budget reset, and what does `CONF_AI_MONTHLY_BUDGET = 0` mean? | `_check_budget()` compares `date.today().month` to `_BudgetTracker.budget_month`; on mismatch it zeroes `monthly_cost` and updates `budget_month`. A configured value of `0` disables the cap entirely — `_check_budget()` returns `True` without comparing accumulated cost. | [Budget Tracking](#budget-tracking) |
| Which error types cause a retry, and what is the backoff schedule for `AI_MAX_RETRIES = 3`? | All four exception types retry: `RateLimitError`, `APITimeoutError`, `APIError`, and bare `Exception`. Delays are `1.0 s`, `2.0 s` between attempts 1→2 and 2→3; no sleep after attempt 3. | [Rate Limiting and Retry](#rate-limiting-and-retry) |
| What happens to the circuit breaker failure counter when the circuit is half-open and the probe fails? | The failure counter increments (`consecutive_failures += 1`) via the same post-call path as any other failure. If it meets or exceeds `AI_CIRCUIT_BREAKER_THRESHOLD` (5), the breaker transitions back to `"open"` and `opened_at` is reset. | [Circuit Breaker State Machine](#circuit-breaker-state-machine) |
| What config keys does `ClaudeAPIClient.__init__()` read, and what happens if `CONF_AI_API_KEY` is absent or empty? | It reads only `CONF_AI_API_KEY` at init time. If absent or empty, `self._client` remains `None` and a WARNING is logged; all subsequent `async_request()` calls return `ClaudeResponse(success=False, error="Anthropic client not initialized…")`. | [Initialization and Configuration](#initialization-and-configuration) |
| How does the client know what request shape (thinking control, temperature) a given model needs? | It doesn't discover this live anymore (Issue #572). `_build_request_kwargs()` looks up the model in the static `AI_MODEL_CAPABILITIES` table in `const.py`, verified against the live Anthropic API ahead of time. A model not in the table falls back to the legacy shape (the behavior proven safe for years prior to claude-sonnet-5) and logs a WARNING naming it — no learning step, no per-model failure, no persistence to lose. | [Per-Model Capability Table](#per-model-capability-table-issue-572) |
| How does the client call a Home Assistant AI provider instead of Anthropic (Issue #1336)? | When `config[ai_provider] == "ha_ai_task"`, `async_request` / `async_request_streaming` call `ai_task.async_generate_data(hass, task_name=AI_TASK_NAME, entity_id=ai_task_entity or None, instructions=system_prompt + "\n\n" + user_message)` inside `asyncio.timeout(AI_TASK_TIMEOUT_SECONDS)`. Circuit breaker, daily limits and history are reused; cost and tokens are 0 and the budget check is skipped. | [AI Task Transport](#ai-task-transport-issue-1336) |
| Which `error_code` does a failed AI Task call get? | A `HomeAssistantError` is classified by message or translation key: Anthropic `response_max_tokens` or OpenAI "max output tokens" → `ai_output_limit`; Anthropic `response_context_window_exceeded` → `ai_context_limit`; "No entity_id provided and no preferred entity set" or `no_entity` → `ai_task_no_entity`; anything else → `ai_provider_error`. | [Error classification](#error-classification) |
| Which reasoning levels does the client accept (Issue #1336)? | `low` and `medium` (`AI_REASONING_OPTIONS`). A stored `high` is migrated to `medium` (config entry v21); the legacy-shape extended-thinking branch (16384-token budget) has no caller. | [Reasoning Options](#reasoning-options-low-and-medium-issue-1336) |
| Is there one client per zone? | No, one per install since Issue #1336: `hass.data[f"{DOMAIN}_ai"]["client"]`, shared by every zone's coordinator; each zone's setup or reload calls `update_config()` with its (identical) entry data. | [Initialization and Configuration](#initialization-and-configuration) |
| How do I tell, from logs alone, what request shape was actually sent for a given AI call, without a live diagnostic script? | `_log_request_kwargs_decision()` logs one DEBUG line before every API call: model, reasoning_effort, `thinking_type` (`"none"`/`"enabled"`/`"adaptive"`), max_tokens, and whether `temperature`/`output_config` made it into the built kwargs. "Response finished" lines also log `input_tokens` alongside `output_tokens`. Any `APIError` — always unexpected now, since the shape is table-verified — logs a distinct `"Unexpected API error"` WARNING with the full raw message and is never retried with a different shape. | [Observability](#observability) |

---

## Scope

- **File:** `custom_components/climate_advisor/claude_api.py`
- **Approximate line range:** L1 – L1266 (entire file)
- **Primary entry points:** `ClaudeAPIClient.async_request()` (L252), `ClaudeAPIClient.__init__()` (L215)

This spec covers `ClaudeAPIClient` and the supporting dataclasses (`ClaudeResponse`, `_CircuitBreaker`, `_RateLimitCounters`, `_BudgetTracker`). Since Issue #1336 the client has two transports, chosen per request by `config[ai_provider]`: the **AI Task transport** (`ha_ai_task`, the main path, [below](#ai-task-transport-issue-1336)) and the **Anthropic transport** (`builtin_claude`, deprecated; everything else in this spec). The guard layer, counters and history are shared. It does NOT cover:

- The `AISkillRegistry` and `AISkillDefinition` execution pipeline — covered by `ai-skills-spec.md`
- Context assembly for `"activity_report"` and `"investigator"` skills — covered by `ai-skills-activity-spec.md` and `ai-skills-investigator-spec.md`
- The `async_test_connection()` diagnostic helper — not part of the production request path

---

## Pre-conditions

For `async_request()` to proceed past the guard layer and reach the network (Anthropic transport; the AI Task transport's guard list is in [AI Task Transport](#ai-task-transport-issue-1336)):

1. `self._circuit_breaker.state` must be `"closed"` or `"half_open"` — checked by `_check_circuit_breaker()`.
2. The daily request counter for the given `triggered_by` (`"auto"` or `"manual"`) must be below its configured cap — checked by `_check_rate_limit(triggered_by)`.
3. `_BudgetTracker.monthly_cost` must be below `CONF_AI_MONTHLY_BUDGET`, or the budget must be `0` (no cap) — checked by `_check_budget()`.
4. `self._client` must be a live `AsyncAnthropic` instance (not `None`) — checked inline after the three guard calls.
5. The `anthropic` package must be importable (`ANTHROPIC_AVAILABLE = True`); if not, `self._client` is always `None` and condition 4 fails.

For `restore_persistent_stats()` to be safe:

6. The `data` argument must be a dict (callers are responsible for type validation before calling; `restore_persistent_stats()` itself uses `.get()` with defaults on every key, so missing keys are safe; malformed types on individual keys will raise `ValueError` or `TypeError` from `int()`/`float()` casts).

---

## Post-conditions

After a successful `async_request()` call (`response.success = True`):

1. `self._circuit_breaker.state` is `"closed"` and `self._circuit_breaker.consecutive_failures` is `0`.
2. `self._budget.monthly_cost` has increased by `response.estimated_cost`.
3. Either `self._rate_counters.auto_requests_today` or `self._rate_counters.manual_requests_today` has incremented by 1, depending on `triggered_by`.
4. `self._total_requests` has incremented by 1.
5. `self._last_request_time` holds the `time.time()` value captured at the end of the call.
6. A metadata-only entry (no content, no key) has been appended to `self.request_history`; if the deque was at `AI_REQUEST_HISTORY_CAP` capacity, the oldest entry was evicted.

After a failed `async_request()` call (`response.success = False`):

7. `self._circuit_breaker.consecutive_failures` has incremented by 1.
8. `self._error_count` has incremented by 1.
9. If `consecutive_failures` now equals or exceeds `AI_CIRCUIT_BREAKER_THRESHOLD` (5), `self._circuit_breaker.state` is `"open"` and `self._circuit_breaker.opened_at` holds the current `time.monotonic()` value.
10. `self._total_requests` has incremented by 1.
11. A metadata entry is appended to `self.request_history` with `success=False` and the error string.

**Exception:** post-conditions 2, 3, 6–11 do NOT apply when a guard returns a synthetic failure response (circuit open, rate-limited, or budget exceeded). In those cases, only the log message is emitted — no counter, accumulator, or history is updated.

---

## Invariants

1. **Guard order is fixed.** Within `async_request()`, checks always run in this sequence: circuit breaker → rate limit → monthly budget → client-is-None. No reordering occurs regardless of config.

2. **`async_request()` never raises.** All exceptions from `_async_call_with_retry()` are caught internally; the caller always receives a `ClaudeResponse` instance.

3. **`ClaudeResponse.success` and `ClaudeResponse.circuit_open` are mutually exclusive.** A response with `circuit_open=True` always has `success=False`. No code path sets both to `True`.

4. **`ClaudeResponse.success` and `ClaudeResponse.budget_exceeded` are mutually exclusive.** Budget-exceeded responses are synthetic pre-flight rejections with `success=False`.

5. **`ClaudeResponse.success` and `ClaudeResponse.rate_limited` are mutually exclusive.** Rate-limited responses are synthetic pre-flight rejections with `success=False`.

6. **The API key is never recorded.** It is not written to `request_history`, not included in any log statement beyond "key configured"/"key updated", and not returned by `get_status()` or `get_persistent_stats()`.

7. **Daily counters reset at most once per calendar day.** `_reset_daily_counters_if_needed()` is called at the top of every `async_request()` and `get_status()`. The reset is a no-op if `counter_date == date.today()`.

8. **Monthly cost accumulates only on `success=True`.** Retried-and-failed calls contribute zero to `monthly_cost`; cost is added only after a successful API response is returned by `_async_call_with_retry()`.

9. **Request history is capped.** `self.request_history` is a `deque(maxlen=AI_REQUEST_HISTORY_CAP)` — Python enforces the cap automatically; no explicit eviction logic is required.

10. **A thinking parameter forces `temperature=1` (when the model supports temperature at all).** `_build_request_kwargs()` (the single kwargs-building source for both the streaming and non-streaming paths — see [Per-Model Capability Table](#per-model-capability-table-issue-572)) overwrites `kwargs["temperature"]` to `1` whenever it attaches a `thinking` parameter, which since Issue #1336 means at every reasoning tier for an `"adaptive"`-shape model only. The `"legacy"`-shape `high` extended-thinking branch has no caller (reasoning is `low`/`medium`). A model whose `AI_MODEL_CAPABILITIES` entry sets `supports_temperature: False` never gets `temperature` added at all, at any tier.

11. **The circuit breaker failure counter is never decremented.** It is only zeroed (on success, in `async_request()`) or incremented (on failure, in `async_request()`). There is no partial-credit decay.

12. **The AI Task transport never charges the budget.** It records `estimated_cost = 0.0`, `input_tokens = output_tokens = 0`, and skips `_check_budget()` (Issue #1336).

13. **Every AI Task failure carries an `error_code`** from the four `AI_ERROR_*` codes; none is left unclassified (Issue #1336).

14. **One client per install.** Every zone's `coordinator.claude_client` is the same object (`hass.data[f"{DOMAIN}_ai"]["client"]`), so the circuit breaker, daily counters and request history are install-wide (Issue #1336).

---

## Circuit Breaker State Machine

`_CircuitBreaker` holds three fields: `state: str`, `consecutive_failures: int`, `opened_at: float` (monotonic timestamp, `0.0` when unused).

### State Transition Table

| From state | Trigger | To state | Side effects |
|---|---|---|---|
| `"closed"` | `consecutive_failures` reaches `AI_CIRCUIT_BREAKER_THRESHOLD` (5) after a failed API call | `"open"` | `opened_at = time.monotonic()`; logs ERROR with failure count |
| `"open"` | `time.monotonic() - opened_at >= AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS` (300 s), detected in `_check_circuit_breaker()` | `"half_open"` | Logs INFO "transitioning to half-open"; one probe request is allowed through |
| `"open"` | Cooldown has NOT elapsed, detected in `_check_circuit_breaker()` | `"open"` (no change) | Returns `False`; `async_request()` immediately returns `ClaudeResponse(circuit_open=True)` |
| `"half_open"` | Probe API call succeeds | `"closed"` | `consecutive_failures = 0`; logs INFO "reset to closed" |
| `"half_open"` | Probe API call fails | `"open"` (if `consecutive_failures >= threshold`) | `consecutive_failures += 1`; `opened_at` reset to `time.monotonic()` |
| Any | `update_config()` called | unchanged | Circuit breaker state is not reset by config updates |

### Single-Probe Semantics

`_check_circuit_breaker()` returns `True` for both `"closed"` and `"half_open"`. The method does not transition back to `"open"` on entry — it does not know whether the probe succeeded yet. The "single probe" behavior emerges from the fact that either outcome immediately moves the breaker out of `"half_open"`: success closes it, failure re-opens it. A second concurrent call arriving while `"half_open"` would also pass `_check_circuit_breaker()`, because `async_request()` is not synchronized. Concurrent callers are therefore not strictly serialized; the guard is soft, not mutex-enforced.

---

## `async_request()` Contract

**Signature:**
```python
async def async_request(
    self,
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    triggered_by: str = "manual",
) -> ClaudeResponse:
```

**Guard sequence (in order, short-circuits on first failure):**

1. `_reset_daily_counters_if_needed()` — always runs; no-op if date unchanged
2. `_check_circuit_breaker()` → `False`: return `ClaudeResponse(success=False, circuit_open=True)`; no other side effects
3. `_check_rate_limit(triggered_by)` → `False`: return `ClaudeResponse(success=False, rate_limited=True)`; no other side effects
4. `_check_budget()` → `False`: return `ClaudeResponse(success=False, budget_exceeded=True)`; no other side effects
5. `self._client is None` → return `ClaudeResponse(success=False, error="Anthropic client not initialized…")`

**Resolution order for call parameters (per-call override → config → default):**

| Parameter | Override arg | Config key | Default constant |
|---|---|---|---|
| `max_tokens` | `max_tokens` arg | `CONF_AI_MAX_TOKENS` | `DEFAULT_AI_MAX_TOKENS` (4096) |
| `temperature` | `temperature` arg | `CONF_AI_TEMPERATURE` | `DEFAULT_AI_TEMPERATURE` (0.3) |
| `model` | `model` arg | `CONF_AI_MODEL` | `DEFAULT_AI_MODEL` ("claude-sonnet-5") |
| `reasoning_effort` | `reasoning_effort` arg | `CONF_AI_REASONING_EFFORT` | `DEFAULT_AI_REASONING_EFFORT` ("low") |

**Post-call counter updates (applied regardless of retry count):**

- Success: zero `consecutive_failures`, set state `"closed"`, add cost to `monthly_cost`, increment appropriate daily counter
- Failure: increment `consecutive_failures` and `_error_count`; if `consecutive_failures >= AI_CIRCUIT_BREAKER_THRESHOLD`: set state `"open"`, record `opened_at`
- Always: increment `_total_requests`, record `_last_request_time`, append to `request_history`

---

## AI Task Transport (Issue #1336)

**What the occupant experiences.** With an AI Task entity chosen (or HA's preferred one), the
Investigator runs on the occupant's own Home Assistant AI provider. The report arrives in one
piece when it is done. If the provider refuses (output limit, context window, no entity, any
other failure), the occupant gets the non-AI fallback report plus a notice that says why and,
for a limit, which setting to raise.

### Selection

| `config[CONF_AI_PROVIDER]` | Transport |
|---|---|
| `"ha_ai_task"` (`AI_PROVIDER_HA_AI_TASK`) | AI Task, this section |
| `"builtin_claude"` (`AI_PROVIDER_BUILTIN_CLAUDE`, deprecated) | Anthropic SDK, the rest of this spec |
| absent (pre-v21 data) | `ai_settings_sync.provider_of()`: a key means `builtin_claude`, otherwise `ha_ai_task`. Migration v21 writes the key, so this is only a safety net |

### Pre-conditions

1. `self.hass` is set (the coordinator sets the `hass` attribute when it takes the shared client).
2. AI is configured for the install: `ai_settings_sync.is_ai_configured(entry.data)`. On AI Task no key is needed; an empty `ai_task_entity` is valid and means HA's preferred AI Task entity.
3. `manifest.json` lists `"after_dependencies": ["ai_task"]`, so `ai_task` is set up first when the occupant has it.

### Guard sequence

Same order as the Anthropic transport, minus the budget:

1. `_reset_daily_counters_if_needed()`
2. `_check_circuit_breaker()` → `False`: `ClaudeResponse(success=False, circuit_open=True)`
3. `_check_rate_limit(triggered_by)` → `False`: `ClaudeResponse(success=False, rate_limited=True)`
4. **Budget: skipped.** No cost is known.

### The call

```python
async with asyncio.timeout(AI_TASK_TIMEOUT_SECONDS):  # 300 s; ai_task has no timeout of its own
    result = await homeassistant.components.ai_task.async_generate_data(
        hass,
        task_name=AI_TASK_NAME,  # "Climate Advisor investigation"
        entity_id=config[CONF_AI_TASK_ENTITY] or None,  # None = HA's preferred AI Task entity
        instructions=system_prompt + "\n\n" + user_message,
    )
```

- No `structure`: `result.data` is plain text, which `parse_investigation_response` splits
  as before.
- `instructions` carries Climate Advisor's whole system prompt, including the
  `=== OUTPUT LANGUAGE ===` block, because AI Task always uses HA's fixed system prompt with
  `language=None`.
- `max_tokens`, `temperature`, `model` and `reasoning_effort` arguments are ignored on this
  transport: they are the provider's own settings (see
  [Provider requirements](ai-integration.md#provider-requirements-issue-1336)).

### Post-conditions

- **Success:** `ClaudeResponse(success=True, content=<result text>, input_tokens=0, output_tokens=0, estimated_cost=0.0, latency_ms=<elapsed>)`. Circuit breaker, daily counter, `_total_requests` and `request_history` are updated exactly as on the Anthropic transport (post-conditions 1 and 3–6 above). `monthly_cost` is unchanged. `truncated` is never set: AI Task returns no stop reason.
- **Failure:** post-conditions 7–11 above, plus the response carries `error_code` (one of the four below) and `error` (the provider's message). The caller ([AI skills pipeline](ai-skills-spec.md#cut-short-and-error-codes-issue-1336)) runs the deterministic fallback.

### Streaming

`async_request_streaming()` on this transport yields exactly:

1. one `{"type": "text", "text": <the whole result text>}` event, then
2. `{"type": "stop", "stop_reason": "end_turn"}`.

No thinking or delta events: AI Task has no stream for callers. On failure it yields the same
error the non-streaming path returns, with its `error_code`.

### Error Classification

Every `HomeAssistantError` (which also covers `ServiceValidationError`) is classified by its
message or translation key:

| Provider error (HA 2026.10.4) | `error_code` | Constant | What the occupant is told |
|---|---|---|---|
| Anthropic translation key `response_max_tokens`; OpenAI "max output tokens" | `ai_output_limit` | `AI_ERROR_OUTPUT_LIMIT` | The provider stopped at its output limit; raise max tokens to at least 8192 in that integration |
| Anthropic translation key `response_context_window_exceeded` | `ai_context_limit` | `AI_ERROR_CONTEXT_LIMIT` | The request did not fit the model's context window; it needs at least 16k tokens |
| "No entity_id provided and no preferred entity set" (2026.10.4), or translation key `no_entity` (HA `dev`, **[unverified against a release]**) | `ai_task_no_entity` | `AI_ERROR_NO_ENTITY` | Choose an AI Task entity in AI settings, or set a default under Settings > System > AI |
| Anything else | `ai_provider_error` | `AI_ERROR_PROVIDER` | The AI provider failed; the fallback report is shown |

A known limitation: providers other than Anthropic and OpenAI were not checked for their
output-limit errors, so theirs land in `ai_provider_error`. The pre-run limit check and the
`cut_short` notice cover that gap.

### What the transport does not do

- No retry with a different shape and no model fallback (those are Anthropic-SDK concerns).
- No write to the provider's integration; the only other read is the limit lookup in
  `ai_limits.py` ([boundary exception §3](HA-BOUNDARY-EXCEPTIONS.md#3-ai-task-generate-data-and-the-limit-lookup-issue-1336)).

---

## Reasoning Options: low and medium (Issue #1336)

`AI_REASONING_OPTIONS = [AI_REASONING_LOW, AI_REASONING_MEDIUM]`. The owner decided on medium
at most, which keeps a normal report plus thinking inside an 8192-token output limit.

| Stored value | After migration v21 | Built-in Claude request |
|---|---|---|
| `low` | `low` | legacy shape: no thinking control; adaptive shape: `thinking = {"type": "adaptive"}`, `output_config = {"effort": "low"}` |
| `medium` | `medium` | legacy shape: no thinking control; adaptive shape: as above with `"medium"` |
| `high` | **`medium`** | — (no caller) |

**Removed:** the high extended-thinking path, `kwargs["thinking"] = {"type": "enabled", "budget_tokens": AI_REASONING_BUDGET_TOKENS[high]}` (16384), and its forced `temperature=1`. It was the only path that sent a 16384-token thinking budget. `AI_REASONING_HIGH` stays defined only so migration v21 and old stored values are recognised. The low and medium budgets (1024, 4096) remain as reference values.

On the AI Task transport the reasoning level is the provider's own option (Anthropic `thinking_effort`, OpenAI `reasoning_effort`), documented as "set medium".

---

## Per-Model Capability Table (Issue #572)

`ClaudeAPIClient` used to *discover* per-model request-shape quirks live, reactively, from the API's own error responses or from a "successful but unusable" symptom — see history below. Issue #572 replaced that entirely: this product supports a small, known set of Claude models, not an arbitrary universe of them, so the correct request shape is hardcoded in `AI_MODEL_CAPABILITIES` (`const.py`), verified against the live Anthropic API ahead of time rather than discovered from a live failure.

```python
AI_MODEL_CAPABILITIES: dict[str, dict] = {
    "claude-sonnet-4-6": {"thinking_shape": "legacy", "supports_temperature": True},
    "claude-opus-4-6": {"thinking_shape": "legacy", "supports_temperature": True},
    "claude-haiku-4-5-20251001": {"thinking_shape": "legacy", "supports_temperature": True},
    "claude-sonnet-5": {"thinking_shape": "adaptive", "supports_temperature": False},
    "claude-opus-5": {"thinking_shape": "adaptive", "supports_temperature": False},
    "claude-fable-5": {"thinking_shape": "adaptive", "supports_temperature": False},
}
```

`_build_request_kwargs()` is a pure lookup against this table (falling back to `AI_MODEL_CAPABILITIES_DEFAULT` — the legacy shape — for a model not yet in it, logging a WARNING naming the model):

| `thinking_shape` | Effect on `_build_request_kwargs()` |
|---|---|
| `"legacy"` | No thinking control at `"low"`/`"medium"`, unchanged for claude-sonnet-4-6/opus-4-6/haiku-4-5. The former `"high"` branch (`kwargs["thinking"] = {"type": "enabled", "budget_tokens": 16384}`) has no caller since Issue #1336 ([Reasoning Options](#reasoning-options-low-and-medium-issue-1336)). |
| `"adaptive"` | `kwargs["thinking"] = {"type": "adaptive"}` and `kwargs["output_config"] = {"effort": reasoning_effort}` at **every** reasoning tier, from the very first request — no learning step. |

`supports_temperature: False` omits `temperature` from `kwargs` entirely (some newer models reject it outright with a 400).

### Why this replaced reactive learning (history: Issues #563/#565/#568/#569)

Confirmed live (`claude-sonnet-5`, direct-API diagnostic): some newer models reject the legacy `thinking.type.enabled` shape outright, and — critically — before that was discovered reactively, they performed their own internal reasoning even when `reasoning_effort != "high"` and no `thinking` parameter was sent at all. Because `_build_request_kwargs()` previously only ever attached thinking control at the `"high"` tier, a `"medium"`/`"low"` request to such a model left its internal reasoning completely uncapped — on a production-sized prompt this consumed the *entire* `max_tokens` budget before the model ever started the visible answer, with no exception raised anywhere (a "successful" HTTP response that was simply unusable, `truncated_empty == True`).

The reactive fix (#565, later persisted across reloads/restarts by #568/#569) still guaranteed the *first-ever* request to such a model would silently fail once, in the foreground, during whatever user action triggered it — confirmed live on 2026-08-05: a claude-sonnet-5 first call after a model switch hung with a completely static UI for ~90 seconds before failing. Worse, #568/#569's persistence fix only covered the config-reload shutdown path (`async_unload_entry()` → `async_shutdown()` → `_async_save_state()`); a genuine HA restart (`EVENT_HOMEASSISTANT_STOP`) never flushed the learned capability, so the "one-time" cost kept recurring on an install that restarts/redeploys regularly. Rather than patch that persistence gap again, Issue #572 removed the entire reactive-learning subsystem (`_unsupported_params`, `_adaptive_thinking_models`, `_detect_deprecated_param()`, `_detect_adaptive_thinking_required()`, the same-call retry loops in both request paths) in favor of the static, pre-verified table above — eliminating the failure class rather than shortening it.

`truncated_empty` (full budget consumed, zero visible text) is still detected and logged, but now only as a signal: if a **table-verified** model still produces it, that means Anthropic changed the model's behavior out from under a known-good entry — a WARNING names the model and points at `AI_MODEL_CAPABILITIES`, with no automatic retry or relearning.

### Observability

`_log_request_kwargs_decision()` logs one DEBUG line immediately before every API call (both request paths) with `model`, `reasoning_effort`, `thinking_type` (`"none"`/`"enabled"`/`"adaptive"`), `max_tokens`, and whether `temperature`/`output_config` made it into the built kwargs — so the actual shape sent is always visible from `tools/ha_logs.py --filter claude` without a live diagnostic script. The "response finished" DEBUG lines also include `input_tokens` alongside `output_tokens` — relevant because claude-sonnet-5's updated tokenizer is documented to parse the same source text into meaningfully more input tokens than claude-sonnet-4-6, which could push a large prompt toward the model's context window on the input side. Any `APIError` — always unexpected now that the shape is table-verified — logs a distinct `"Unexpected API error for model '%s': %s"` WARNING and is never retried with a different shape.

---

## Budget Tracking

`_BudgetTracker` maintains two fields:

| Field | Type | Meaning |
|---|---|---|
| `monthly_cost` | `float` | Accumulated USD cost for the current calendar month |
| `budget_month` | `int` | Month number (1–12) of the current accumulation window |

**Reset trigger:** `_check_budget()` compares `date.today().month` to `budget_month`. On mismatch: `monthly_cost = 0.0`, `budget_month = date.today().month`. The reset happens inside the budget check itself — not in a scheduled callback.

**Cap semantics:**

- `CONF_AI_MONTHLY_BUDGET = 0` (default): `_check_budget()` returns `True` unconditionally after the month-roll check; no cost comparison is performed.
- `CONF_AI_MONTHLY_BUDGET > 0`: `_check_budget()` returns `monthly_cost < monthly_budget` (strict less-than; a cost exactly equal to the cap is blocked).

**Accumulation timing:** `monthly_cost += response.estimated_cost` runs in `async_request()` only after `_async_call_with_retry()` returns a success response. Retries that ultimately fail contribute zero.

**Persistence:** `monthly_cost` and `budget_month` are included in `get_persistent_stats()` and restored by `restore_persistent_stats()`. Month-roll is re-applied by `_reset_daily_counters_if_needed()` / `_check_budget()` on the first call after HA restarts across a month boundary. Per-model request-shape capability is no longer part of this persisted state at all (Issue #572) — it's a static table in `const.py`, not per-instance learned state.

---

## Rate Limiting and Retry

### Daily Rate Limits

Three independent daily counters exist:

| Counter | Config cap key | Default | Scope |
|---|---|---|---|
| `auto_requests_today` | `CONF_AI_AUTO_REQUESTS_PER_DAY` | 5 | `triggered_by="auto"` calls to `async_request()` |
| `manual_requests_today` | `CONF_AI_MANUAL_REQUESTS_PER_DAY` | 20 | `triggered_by="manual"` calls to `async_request()` |
| `_investigator_requests_today` | `CONF_AI_INVESTIGATOR_RPD` | 3 | Checked by `check_investigator_rate_limit()` only; not checked in `async_request()` |

All three counters are zeroed by `_reset_daily_counters_if_needed()` when `date.today() != counter_date`. The investigator counter uses a separate UTC-date string tracked by `_reset_investigator_counter_if_needed()` and is not zeroed by the main reset.

`check_investigator_rate_limit()` also verifies `CONF_AI_INVESTIGATOR_ENABLED`; if disabled, it returns `(False, "Investigative agent is not enabled")` regardless of the counter value.

### Exponential Backoff Retry

`_async_call_with_retry()` implements up to `AI_MAX_RETRIES` (3) attempts.

**Retry-eligible exception types (all trigger retry):**

| Exception | Source |
|---|---|
| `RateLimitError` | Anthropic SDK — API-level rate limit |
| `APITimeoutError` | Anthropic SDK — network timeout |
| `APIError` | Anthropic SDK — other API error |
| `Exception` (bare) | Any unexpected error |

**Backoff schedule** (`AI_RETRY_BASE_DELAY_SECONDS = 1.0`):

| After attempt | Sleep before next attempt |
|---|---|
| 1 | 1.0 s (`1.0 * 2^0`) |
| 2 | 2.0 s (`1.0 * 2^1`) |
| 3 (final) | No sleep — falls through to failure return |

A successful attempt on any retry number returns immediately without sleep.

**Terminal failure:** after all `AI_MAX_RETRIES` attempts fail, `_async_call_with_retry()` returns `ClaudeResponse(success=False, error=last_error, latency_ms=<total elapsed>)`. The latency covers the full retry window including all sleep intervals.

---

## `ClaudeResponse` Data Class

```python
@dataclass
class ClaudeResponse:
    success: bool  # True iff the API call completed and returned a valid response
    content: str  # Claude's raw text response; always "" when success=False
    input_tokens: int  # Tokens consumed from the prompt; 0 when success=False
    output_tokens: int  # Tokens in the response; 0 when success=False
    estimated_cost: float  # USD cost estimate from _MODEL_COSTS; 0.0 when success=False
    latency_ms: float  # Wall-clock ms from first attempt to return (includes retries)
    error: str | None  # Error message string; None when success=True
    rate_limited: bool  # True iff rejected by the daily rate limit guard (pre-flight)
    circuit_open: bool  # True iff rejected by the circuit breaker guard (pre-flight)
    budget_exceeded: bool  # True iff rejected by the monthly budget guard (pre-flight)
```

### Mutual-Exclusivity Invariant

The four boolean flags are mutually exclusive with `success=True`:

| Flag set to `True` | `success` value | Meaning |
|---|---|---|
| `circuit_open=True` | Always `False` | Pre-flight rejection; no API call was made |
| `rate_limited=True` | Always `False` | Pre-flight rejection; no API call was made |
| `budget_exceeded=True` | Always `False` | Pre-flight rejection; no API call was made |
| All flags `False` | `True` or `False` | API was called; `success` reflects the outcome |

No code path produces a `ClaudeResponse` with `success=True` and any of `circuit_open`, `rate_limited`, or `budget_exceeded` set to `True`. A caller receiving `success=True` can assert that all three guard flags are `False`.

When `success=False` and all guard flags are `False`, the failure came from inside `_async_call_with_retry()` (all retries exhausted, or `self._client is None`); `error` will contain the diagnostic string.

---

## Initialization and Configuration

### One shared client per install (Issue #1336)

The client is created once per install, when the first zone whose entry data passes `ai_settings_sync.is_ai_configured()` sets up, and stored with the skill registry in `hass.data[f"{DOMAIN}_ai"] = {"client": ClaudeAPIClient, "skills": AISkillRegistry}`. Every zone's `coordinator.claude_client` / `coordinator.ai_skills` point at those objects, and the coordinator sets `client.hass`. Each zone's setup or reload calls `client.update_config(entry.data)`; all entries hold the same AI values ([install-wide settings](ai-integration.md#install-wide-settings-and-the-shared-client)), so the order does not matter. The last zone to unload removes it, in the same `if not remaining:` block of `async_unload_entry()` that removes the panel.

### `__init__()` Config Keys Read

`ClaudeAPIClient.__init__()` reads exactly one config key at construction time (the Anthropic transport's key; the AI Task transport reads `ai_provider` and `ai_task_entity` at call time):

| Key | Purpose |
|---|---|
| `CONF_AI_API_KEY` | Authenticate the `AsyncAnthropic` client |

All other config keys (`CONF_AI_MODEL`, `CONF_AI_MAX_TOKENS`, etc.) are read lazily at call time from `self._config`. `self._config` is the full HA config-entry data dict stored by reference — live changes made via `update_config()` are immediately visible to subsequent calls.

### Absent or Empty API Key

If `config.get(CONF_AI_API_KEY, "")` returns an empty string:

1. `self._client` is set to `None` (no `AsyncAnthropic` is constructed).
2. A WARNING is logged: "No AI API key configured; Claude API client will not be active".
3. Every subsequent `async_request()` call passes the three guard checks (assuming circuit is closed, counters are within limits, budget is under cap) and then fails the client-is-None check, returning `ClaudeResponse(success=False, error="Anthropic client not initialized (missing package or API key)")`.

### `update_config()` Behavior

Called by the coordinator when the HA options flow updates config:

1. Old and new API key values are compared.
2. If unchanged: only `self._config` is replaced; `self._client` is left intact.
3. If changed to a non-empty value: a new `AsyncAnthropic(api_key=new_key)` is constructed; old client is discarded.
4. If changed to empty: `self._client = None`; a WARNING is logged.
5. Circuit breaker state, counters, and budget are NOT reset by `update_config()`.

### Missing `anthropic` Package

If `from anthropic import ...` raises `ImportError`:

- `ANTHROPIC_AVAILABLE = False`
- `AsyncAnthropic`, `APIError`, `APITimeoutError`, `RateLimitError` are all aliased to `None` or `Exception` at module level.
- `__init__()` skips client construction (the `if self._client is None and ANTHROPIC_AVAILABLE:` guard is `False`).
- `async_test_connection()` returns `(False, "anthropic package is not installed")` immediately.

---

## Error Conditions

| Failure | Where detected | Handling | Caller receives |
|---|---|---|---|
| `anthropic` package not installed | Module import | `ANTHROPIC_AVAILABLE = False`; client stays `None` | `ClaudeResponse(success=False, error="Anthropic client not initialized…")` |
| API key absent or empty | `__init__()` | `self._client = None`; WARNING logged | Same as above |
| Circuit breaker open | `async_request()` pre-flight | Immediate return; no network call | `ClaudeResponse(success=False, circuit_open=True)` |
| Daily rate limit reached | `async_request()` pre-flight | Immediate return; no network call | `ClaudeResponse(success=False, rate_limited=True)` |
| Monthly budget exceeded | `async_request()` pre-flight | Immediate return; no network call | `ClaudeResponse(success=False, budget_exceeded=True)` |
| `RateLimitError` from Anthropic | `_async_call_with_retry()` | Retry up to `AI_MAX_RETRIES`; exponential backoff | `ClaudeResponse(success=False, error="Rate limit error: …")` after all retries |
| `APITimeoutError` | `_async_call_with_retry()` | Same retry/backoff | `ClaudeResponse(success=False, error="Timeout error: …")` after all retries |
| `APIError` | `_async_call_with_retry()` | Same retry/backoff | `ClaudeResponse(success=False, error="API error: …")` after all retries |
| Unexpected exception | `_async_call_with_retry()` | Same retry/backoff | `ClaudeResponse(success=False, error="Unexpected error: …")` after all retries |
| AI Task: provider stopped at its output limit (Anthropic `response_max_tokens`, OpenAI "max output tokens") | AI Task transport | Classified; failure path (breaker, fallback) | `ClaudeResponse(success=False, error_code="ai_output_limit")` |
| AI Task: context window exceeded (Anthropic `response_context_window_exceeded`) | AI Task transport | Same | `error_code="ai_context_limit"` |
| AI Task: no entity picked and no preferred entity set | AI Task transport | Same | `error_code="ai_task_no_entity"` |
| AI Task: any other `HomeAssistantError` | AI Task transport | Same | `error_code="ai_provider_error"` |
| `restore_persistent_stats()` with bad `counter_date` string | `date.fromisoformat()` | `except (KeyError, ValueError)`: resets `counter_date = date.today()` | Counter starts fresh for the day |
| `_estimate_cost()` with unrecognized model | Prefix lookup miss | Falls through to Sonnet rates as default | Cost estimate uses $3.00/$15.00 per 1M tokens |
| `APIError` for a table-verified model (Issue #572 — always unexpected now, since the shape is pre-verified) | `APIError` branch of `_async_call_with_retry()` / `async_request_streaming()` | Logged as `"Unexpected API error for model '%s'"`; normal retry/backoff (non-streaming) or immediate re-raise (streaming) — never a different-shape retry | Same as the generic `APIError` row above |
| Full `max_tokens` budget consumed with zero visible answer text (`truncated_empty=True`), no exception at all — see [Per-Model Capability Table](#per-model-capability-table-issue-572) | Computed post-response in `_single_api_call()` / after the stream completes in `async_request_streaming()` | Log-only WARNING naming the model and pointing at `AI_MODEL_CAPABILITIES` — no retry, no relearning (Issue #572) | Non-streaming: `ClaudeResponse(success=True, truncated_empty=True)`. Streaming: `{"type": "stop", "truncated_empty": True}`; caller must treat either as no usable content |

---

## Code Reference

- [`ClaudeAPIClient.__init__`](../custom_components/climate_advisor/claude_api.py#L198) — construction, client init, dataclass setup
- [`ClaudeAPIClient.async_request`](../custom_components/climate_advisor/claude_api.py#L252) — main public entry point; guard sequence + counter updates
- [`async_request_streaming`](../custom_components/climate_advisor/claude_api.py#L399) — streaming entry point; single attempt, no capability-detection retry (Issue #572)
- [`_check_circuit_breaker`](../custom_components/climate_advisor/claude_api.py#L811) — state machine query; transitions `"open"` → `"half_open"` on cooldown expiry
- [`_build_request_kwargs`](../custom_components/climate_advisor/claude_api.py#L901) — single kwargs-building source for both request paths; pure `AI_MODEL_CAPABILITIES` table lookup (see [Per-Model Capability Table](#per-model-capability-table-issue-572))
- [`_log_request_kwargs_decision`](../custom_components/climate_advisor/claude_api.py#L962) — DEBUG-logs the resolved request shape before every API call
- [`_single_api_call`](../custom_components/climate_advisor/claude_api.py#L980) — one atomic non-streaming API call + response parsing; no retry policy of its own
- [`_async_call_with_retry`](../custom_components/climate_advisor/claude_api.py#L1126) — non-streaming retry loop with exponential backoff; same-tier model-fallback on `NotFoundError`; no capability-detection retries (Issue #572)
- [`_check_budget`](../custom_components/climate_advisor/claude_api.py#L839) — month-roll detection and cap check
- [`_check_rate_limit`](../custom_components/climate_advisor/claude_api.py#L766) — daily counter check for `"auto"` vs `"manual"` callers
- [`check_investigator_rate_limit`](../custom_components/climate_advisor/claude_api.py#L783) — separate investigator gate; also checks `CONF_AI_INVESTIGATOR_ENABLED`
- [`update_config`](../custom_components/climate_advisor/claude_api.py#L740) — updates config in place; since Issue #1336 called by each zone's setup or reload to refresh the shared client (a config change still takes effect through the zone reload)
- `homeassistant.components.ai_task.async_generate_data` — the AI Task transport's only call (Issue #1336); error classification into `AI_ERROR_*` in `claude_api.py`
- [`ai_limits.async_read_limits`](../custom_components/climate_advisor/ai_limits.py) — provider limit reader shared by the options flow and `GET ai_limits` (Issue #1336)
- [`get_persistent_stats`](../custom_components/climate_advisor/claude_api.py#L674) — serializes counters + monthly cost for HA state persistence
- [`restore_persistent_stats`](../custom_components/climate_advisor/claude_api.py#L696) — rehydrates counters after HA restart or config reload
- [`ClaudeResponse`](../custom_components/climate_advisor/claude_api.py#L95) — response dataclass; all fields documented above
- [`_CircuitBreaker`](../custom_components/climate_advisor/claude_api.py#L119) — state machine storage dataclass
- [`_MODEL_COSTS`](../custom_components/climate_advisor/claude_api.py#L82) — per-model-prefix cost rates
- [`AI_MODEL_CAPABILITIES`](../custom_components/climate_advisor/const.py) — static per-model request-shape table (Issue #572), the source of truth `_build_request_kwargs` reads from
