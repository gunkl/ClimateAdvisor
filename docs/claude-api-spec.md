<!-- Nav: ← Context: [AI Integration Brief](ai-integration.md) | → Detail: [claude_api.py](../custom_components/climate_advisor/claude_api.py) | ↔ Related: [ai-skills-spec.md](ai-skills-spec.md) -->

# Claude API Client — Territory Spec (Tier 3)

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
| A configured model rejects a request parameter, or needs a different extended-thinking shape, or silently returns zero visible text while consuming the full `max_tokens` budget — does the client keep failing forever? | No. `_build_request_kwargs()` is the single source of truth for both request paths; two per-model in-memory capability caches (`_unsupported_params`, `_adaptive_thinking_models`) are populated reactively — from a 400 error naming the rejected parameter/shape, or (for the zero-output case, which is a "successful" empty response, not an exception) from observing `truncated_empty=True`. Once learned, every subsequent request for that model is built correctly from the start; only the first occurrence per model per client-instance lifetime pays the cost of discovering it. | [Reactive Per-Model Capability Detection](#reactive-per-model-capability-detection) |

---

## Scope

- **File:** `custom_components/climate_advisor/claude_api.py`
- **Approximate line range:** L1 – L1365 (entire file)
- **Primary entry points:** `ClaudeAPIClient.async_request()` (L278), `ClaudeAPIClient.__init__()` (L215)

This spec covers `ClaudeAPIClient` and the supporting dataclasses (`ClaudeResponse`, `_CircuitBreaker`, `_RateLimitCounters`, `_BudgetTracker`). It does NOT cover:

- The `AISkillRegistry` and `AISkillDefinition` execution pipeline — covered by `ai-skills-spec.md`
- Context assembly for `"activity_report"` and `"investigator"` skills — covered by `ai-skills-activity-spec.md` and `ai-skills-investigator-spec.md`
- The `async_test_connection()` diagnostic helper — not part of the production request path

---

## Pre-conditions

For `async_request()` to proceed past the guard layer and reach the network:

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

10. **Extended thinking forces `temperature=1`.** `_build_request_kwargs()` (the single kwargs-building source for both the streaming and non-streaming paths — see [Reactive Per-Model Capability Detection](#reactive-per-model-capability-detection)) overwrites `kwargs["temperature"]` to `1`, regardless of the caller-supplied temperature, whenever it attaches a `thinking` parameter: unconditionally at `reasoning_effort == AI_REASONING_HIGH` (legacy `{"type": "enabled", "budget_tokens": N}` shape), and at every reasoning tier once a model has been learned to need the newer `{"type": "adaptive"}` + `output_config.effort` shape (Issue #565). The per-model unsupported-parameter strip always runs last, after this override, so a model already known to reject `temperature` never has it silently re-added.

11. **The circuit breaker failure counter is never decremented.** It is only zeroed (on success, in `async_request()`) or incremented (on failure, in `async_request()`). There is no partial-credit decay.

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
| `model` | `model` arg | `CONF_AI_MODEL` | `DEFAULT_AI_MODEL` ("claude-sonnet-4-6") |
| `reasoning_effort` | `reasoning_effort` arg | `CONF_AI_REASONING_EFFORT` | `DEFAULT_AI_REASONING_EFFORT` ("medium") |

**Post-call counter updates (applied regardless of retry count):**

- Success: zero `consecutive_failures`, set state `"closed"`, add cost to `monthly_cost`, increment appropriate daily counter
- Failure: increment `consecutive_failures` and `_error_count`; if `consecutive_failures >= AI_CIRCUIT_BREAKER_THRESHOLD`: set state `"open"`, record `opened_at`
- Always: increment `_total_requests`, record `_last_request_time`, append to `request_history`

---

## Reactive Per-Model Capability Detection

Anthropic's Models API exposes `id`/`display_name`/`created_at` — never a per-model sampling-parameter or extended-thinking-shape schema. There is no way to know ahead of time that a given model no longer accepts `temperature`, or that it requires a different `thinking` parameter shape than an older model in the same family. `ClaudeAPIClient` handles this by learning per-model, in-memory, from the API's own error responses (or from a symptom that isn't an error at all — see below) — never by hardcoding a model-ID list.

Two per-model capability caches, both `dict`/`set` keyed on the resolved model ID, both reset on integration reload (in-memory only, same acceptable cost as `_models_cache`):

| Cache | Populated when | Effect on `_build_request_kwargs()` |
|---|---|---|
| `self._unsupported_params: dict[str, set[str]]` | A 400 whose message matches `` `(\w+)` is deprecated for this model `` (`_detect_deprecated_param()`, Issue #563 follow-on) | Every key ever learned for that model is popped from `kwargs` — this strip always runs LAST, after every other kwargs-building step, so a forced value (e.g. `temperature=1` from extended thinking) is never silently re-added |
| `self._adaptive_thinking_models: set[str]` | A 400 whose message names `thinking.type.adaptive` (`_detect_adaptive_thinking_required()`, Issue #565), OR a "successful" response with `truncated_empty=True` (see below) | At every `reasoning_effort` tier (not just `"high"`), `kwargs["thinking"] = {"type": "adaptive"}` and `kwargs["output_config"] = {"effort": reasoning_effort}` are attached instead of the legacy `{"type": "enabled", "budget_tokens": N}` shape |

### Why `_adaptive_thinking_models` exists (Issue #565)

Confirmed live (`claude-sonnet-5`, direct-API diagnostic bypassing this client entirely): some newer models reject the legacy `thinking.type.enabled` shape outright, and — critically — before that's discovered, they still perform their own internal reasoning even when `reasoning_effort != "high"` and no `thinking` parameter is sent at all. Because `_build_request_kwargs()` previously only ever attached thinking control at the `"high"` tier, a `"medium"`/`"low"` request to such a model left its internal reasoning completely uncapped. On a production-sized prompt this reasoning alone consumed the *entire* `max_tokens` budget before the model ever started the visible answer — `stop_reason == "max_tokens"`, `response.truncated_empty == True` (or, for streaming, `any_text_yielded == False` — see the `ClaudeResponse`/stream `"stop"` event fields), with no exception raised anywhere; the API call is a "successful" HTTP response that is simply unusable. This is the failure mode `_adaptive_thinking_models` exists to self-heal from — see the retry rules below.

### Retry rules (asymmetric between the two request paths)

Both `_async_call_with_retry()` (non-streaming) and `async_request_streaming()` (streaming) catch a 400 matching either detector and learn the corresponding cache entry, then retry once with corrected kwargs — mirroring each other exactly for the exception case. They diverge only for the *non-exception* `truncated_empty` symptom, because a streaming generator cannot un-yield content already shown to the caller:

- **Non-streaming** (`_async_call_with_retry()`): nothing is exposed to the caller until the whole `ClaudeResponse` is returned, so a `truncated_empty` result is safe to retry in place — one extra `_single_api_call()` with `_adaptive_thinking_models` now including this model, same call.
- **Streaming** (`async_request_streaming()`): thinking deltas may already have streamed to the caller (visible in the UI) by the time `truncated_empty` is known, at the very end of the generator. This call surfaces the failure exactly as before the fix; the only thing that changes is `self._adaptive_thinking_models.add(resolved_model)` fires before the generator ends, so the *next* call for this model — streaming or non-streaming, same client instance — builds corrected kwargs from the start and does not repeat the failure.

A brand-new model's very first streaming call can therefore still exhibit one zero-output truncation before self-healing; every call after that (for that model, for the lifetime of this client instance) does not.

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

**Persistence:** `monthly_cost` and `budget_month` are included in `get_persistent_stats()` and restored by `restore_persistent_stats()`. Month-roll is re-applied by `_reset_daily_counters_if_needed()` / `_check_budget()` on the first call after HA restarts across a month boundary.

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

### `__init__()` Config Keys Read

`ClaudeAPIClient.__init__()` reads exactly one config key at construction time:

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
| `restore_persistent_stats()` with bad `counter_date` string | `date.fromisoformat()` | `except (KeyError, ValueError)`: resets `counter_date = date.today()` | Counter starts fresh for the day |
| `_estimate_cost()` with unrecognized model | Prefix lookup miss | Falls through to Sonnet rates as default | Cost estimate uses $3.00/$15.00 per 1M tokens |
| 400 naming a deprecated request parameter (e.g. `` `temperature` is deprecated for this model `` ) | `_detect_deprecated_param()` in the `APIError` branch of `_async_call_with_retry()` / `async_request_streaming()` | Learn into `_unsupported_params[model]`; retry once immediately (no backoff) with that param stripped | Success on retry: normal response. Retry also fails: falls through to the generic `APIError` last-error path |
| 400 naming the adaptive thinking shape (`thinking.type.adaptive`) | `_detect_adaptive_thinking_required()`, same branches as above | Learn into `_adaptive_thinking_models`; retry once immediately with `thinking.type.adaptive` + `output_config.effort` | Same success/failure shape as the deprecated-parameter case |
| Full `max_tokens` budget consumed with zero visible answer text (`truncated_empty=True`), no exception at all — see [Reactive Per-Model Capability Detection](#reactive-per-model-capability-detection) | Computed post-response in `_single_api_call()` / after the stream completes in `async_request_streaming()` | Non-streaming: learn + retry once in place. Streaming: learn only (cannot retry in place); this call still surfaces the empty result | Non-streaming: usually a real answer on retry. Streaming: `{"type": "stop", "truncated_empty": True}`; caller must treat this as no usable content, same as before the fix, but only once per model |

---

## Code Reference

- [`ClaudeAPIClient.__init__`](../custom_components/climate_advisor/claude_api.py#L215) — construction, client init, dataclass setup, capability-cache init
- [`ClaudeAPIClient.async_request`](../custom_components/climate_advisor/claude_api.py#L278) — main public entry point; guard sequence + counter updates
- [`async_request_streaming`](../custom_components/climate_advisor/claude_api.py#L425) — streaming entry point; capability-detection retry loop (before any content yielded), post-hoc `truncated_empty` handling
- [`_check_circuit_breaker`](../custom_components/climate_advisor/claude_api.py#L862) — state machine query; transitions `"open"` → `"half_open"` on cooldown expiry
- [`_build_request_kwargs`](../custom_components/climate_advisor/claude_api.py#L952) — single kwargs-building source for both request paths; extended-thinking shape selection + unsupported-parameter strip (see [Reactive Per-Model Capability Detection](#reactive-per-model-capability-detection))
- [`_single_api_call`](../custom_components/climate_advisor/claude_api.py#L1010) — one atomic non-streaming API call + response parsing; no retry policy of its own
- [`_async_call_with_retry`](../custom_components/climate_advisor/claude_api.py#L1154) — non-streaming retry loop with exponential backoff; capability-detection retries; `truncated_empty` in-place retry
- [`_detect_deprecated_param`](../custom_components/climate_advisor/claude_api.py#L89) — regex match for a "param deprecated for this model" 400 (Issue #563 follow-on)
- [`_detect_adaptive_thinking_required`](../custom_components/climate_advisor/claude_api.py#L104) — regex match for a "use thinking.type.adaptive" 400 (Issue #565)
- [`_check_budget`](../custom_components/climate_advisor/claude_api.py#L890) — month-roll detection and cap check
- [`_check_rate_limit`](../custom_components/climate_advisor/claude_api.py#L817) — daily counter check for `"auto"` vs `"manual"` callers
- [`check_investigator_rate_limit`](../custom_components/climate_advisor/claude_api.py#L834) — separate investigator gate; also checks `CONF_AI_INVESTIGATOR_ENABLED`
- [`update_config`](../custom_components/climate_advisor/claude_api.py#L791) — hot-reload config; tears down and recreates client on key change
- [`get_persistent_stats`](../custom_components/climate_advisor/claude_api.py#L725) — serializes counters + monthly cost for HA state persistence
- [`restore_persistent_stats`](../custom_components/climate_advisor/claude_api.py#L747) — rehydrates counters after HA restart
- [`ClaudeResponse`](../custom_components/climate_advisor/claude_api.py#L123) — response dataclass; all fields documented above
- [`_CircuitBreaker`](../custom_components/climate_advisor/claude_api.py#L147) — state machine storage dataclass
- [`_MODEL_COSTS`](../custom_components/climate_advisor/claude_api.py#L110) — per-model-prefix cost rates
