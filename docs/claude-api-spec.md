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

---

## Scope

- **File:** `custom_components/climate_advisor/claude_api.py`
- **Approximate line range:** L1 – L742 (entire file)
- **Primary entry points:** `ClaudeAPIClient.async_request()` (L150), `ClaudeAPIClient.__init__()` (L113)

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

10. **Extended thinking forces `temperature=1`.** When `reasoning_effort == AI_REASONING_HIGH`, the `kwargs["temperature"]` value is overwritten to `1` inside `_async_call_with_retry()`, regardless of the caller-supplied temperature. This is the only code path that does so.

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
    success: bool          # True iff the API call completed and returned a valid response
    content: str           # Claude's raw text response; always "" when success=False
    input_tokens: int      # Tokens consumed from the prompt; 0 when success=False
    output_tokens: int     # Tokens in the response; 0 when success=False
    estimated_cost: float  # USD cost estimate from _MODEL_COSTS; 0.0 when success=False
    latency_ms: float      # Wall-clock ms from first attempt to return (includes retries)
    error: str | None      # Error message string; None when success=True
    rate_limited: bool     # True iff rejected by the daily rate limit guard (pre-flight)
    circuit_open: bool     # True iff rejected by the circuit breaker guard (pre-flight)
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

---

## Code Reference

- [`ClaudeAPIClient.__init__`](../custom_components/climate_advisor/claude_api.py#L113) — construction, client init, dataclass setup
- [`ClaudeAPIClient.async_request`](../custom_components/climate_advisor/claude_api.py#L150) — main public entry point; guard sequence + counter updates
- [`_check_circuit_breaker`](../custom_components/climate_advisor/claude_api.py#L505) — state machine query; transitions `"open"` → `"half_open"` on cooldown expiry
- [`_async_call_with_retry`](../custom_components/climate_advisor/claude_api.py#L595) — retry loop with exponential backoff; extended thinking injection
- [`_check_budget`](../custom_components/climate_advisor/claude_api.py#L533) — month-roll detection and cap check
- [`_check_rate_limit`](../custom_components/climate_advisor/claude_api.py#L460) — daily counter check for `"auto"` vs `"manual"` callers
- [`check_investigator_rate_limit`](../custom_components/climate_advisor/claude_api.py#L477) — separate investigator gate; also checks `CONF_AI_INVESTIGATOR_ENABLED`
- [`update_config`](../custom_components/climate_advisor/claude_api.py#L434) — hot-reload config; tears down and recreates client on key change
- [`get_persistent_stats`](../custom_components/climate_advisor/claude_api.py#L368) — serializes counters + monthly cost for HA state persistence
- [`restore_persistent_stats`](../custom_components/climate_advisor/claude_api.py#L390) — rehydrates counters after HA restart
- [`ClaudeResponse`](../custom_components/climate_advisor/claude_api.py#L68) — response dataclass; all fields documented above
- [`_CircuitBreaker`](../custom_components/climate_advisor/claude_api.py#L84) — state machine storage dataclass
- [`_MODEL_COSTS`](../custom_components/climate_advisor/claude_api.py#L56) — per-model-prefix cost rates
