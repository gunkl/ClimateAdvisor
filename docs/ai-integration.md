<!-- Nav: ← Context: [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) | → Detail: [claude_api.py](../custom_components/climate_advisor/claude_api.py) · [ai_skills.py](../custom_components/climate_advisor/ai_skills.py) · [ai_skills_investigator.py](../custom_components/climate_advisor/ai_skills_investigator.py) · [ai_skills_context.py](../custom_components/climate_advisor/ai_skills_context.py) | ↔ Related: [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) · [Computation Reference](08-COMPUTATION-REFERENCE.md) -->

# AI Integration — Architecture Brief (Tier 2)

**Issue #563 (this pass):** the former `"activity_report"` and `"investigator"` skills have
been merged into one `"investigator"` skill. It is **manual-only**: the sole caller is the
dashboard's Investigate button (`ClimateAdvisorInvestigateView`) and nothing schedules it. A
call without `focus` (the retired activity-report shape) is still accepted, but no live path
makes one. The old
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
| Is there still a separate "activity report" skill? | No (Issue #563) — it was merged into `"investigator"`. Its no-`focus` shape is the same skill with an empty focus (no live path uses it). The investigator is manual-only, never scheduled. | [investigator](#investigator) |
| Does the investigator still have its own model/token/reasoning config overrides? | No (Issue #563) — it shares the single `ai_model`/`ai_reasoning_effort`/`ai_max_tokens` config used everywhere else. `CONF_AI_INVESTIGATOR_MODEL`/`_MAX_TOKENS`/`_REASONING` constants remain defined (unused) only so the historical v13→v14 config migration doesn't break old installs. | [investigator](#investigator) |
| Why did the KNOWN-FIXED ISSUES context section used to grow without bound? | `_fix_is_relevant()`'s escape-hatch rule matched any entry with a non-empty `scope_not_covered` field, which was mandatory on every entry — so all 169 entries always passed regardless of the intended version-scoping. Fixed by removing the field entirely and bounding by recency count instead. | [Known Fixes Context](#known-fixes-context) |
| What are the full pre/post/invariant contracts for `ClaudeAPIClient.async_request()` and the circuit breaker? | The Tier 3 spec covers the 5-row circuit breaker transition table, guard sequence for `async_request()`, `ClaudeResponse` mutual-exclusivity invariants, budget reset trigger, and all four retried exception types. | [Claude API Client — Territory Spec](claude-api-spec.md) |
| What are the full contracts for the skill registry, execution pipeline, and the registered skill? | The Tier 3 spec covers `AISkillRegistry` registration and lookup, the 6-step execution pipeline, return contract enforcement, the merged `investigator` skill's context/parse/fallback contracts, and caching behavior. | [AI Skills Framework — Territory Spec](ai-skills-spec.md) |

---

## Scope

**Owns:**
- All AI provider communication: the shared `ClaudeAPIClient` wrapper and `AnthropicBackend` (`claude_api.py`), the provider seam (`ai_backend.py`, `ai_providers.py`) and the ChatGPT-plan provider (`chatgpt_backend.py`, `chatgpt_account.py`)
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
- Enforce a separate investigator rate limit gated also on `CONF_AI_INVESTIGATOR_ENABLED` — this now gates only the on-demand/focus-driven call path (`ClimateAdvisorInvestigateView`), not skill registration itself, so skill registration does not depend on that toggle (Issue #563)
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
| `claude_api.py` | `ClaudeAPIClient` (provider-neutral wrapper: circuit breaker, rate limits, budget, retry, cost estimation, persistence) and `AnthropicBackend` |
| `ai_backend.py` / `ai_providers.py` | `AIBackend` Protocol, `AIBackendError`, `ProviderSpec`, `PROVIDERS`; provider registration |
| `chatgpt_backend.py` | `ChatGPTBackend`: ChatGPT-plan transport (Responses API, SSE) |
| `chatgpt_account.py` | `ChatGPTAccountManager`: shared tokens, owner, state, quota cooldown, keepalive |
| `ai_skills.py` | `AISkillRegistry` + `AISkillDefinition`: skill registration, lookup, execution pipeline, return contract enforcement |
| `ai_skills_investigator.py` | The merged `"investigator"` skill: system prompt, response parser, deterministic fallback, thin context-assembly orchestrator |
| `ai_skills_context.py` | `ContextProviderRegistry` + every individual context provider (15, as of Issue #563) the investigator's context is assembled from, including the render/timeline functions and the state-cross-validation/override-details/daily-summaries providers ported from the retired `ai_skills_activity.py` |

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

Backoff formula: `AI_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))`. The sleep is skipped after the final attempt. A successful attempt returns immediately; consecutive failure increments the circuit breaker counter. If the zone's backend has been replaced since the request started (`async_apply_ai_settings()`), no further attempt or tier fallback is sent, on either provider: the request ends with a `no_retry` error, outside the breaker.

### Extended Thinking

When `reasoning_effort == AI_REASONING_HIGH`, `async_request()` injects a thinking block into the API call and forces `temperature=1` (required by the Anthropic API for extended thinking). This is the only code path that overrides the caller-supplied temperature.

### Authentication

(Anthropic provider.) The API key is read from `config.get(CONF_AI_API_KEY, "")` at client construction. The ChatGPT provider has no key: it reads tokens from the shared account manager (see [AI Providers](#ai-providers)). The key is static — no token refresh. When `update_config()` detects a key change (by comparing old vs. new values), it tears down and recreates the `AsyncAnthropic` client. The key is never written to request history, log statements, or API responses.

### Persistence

`get_persistent_stats()` returns a dict of all counters (`auto_requests_today`, `manual_requests_today`, `_investigator_requests_today`, `counter_date`) and monthly cost (`monthly_cost`, `budget_month`). `restore_persistent_stats()` rehydrates them on coordinator startup, preserving rate-limit and budget state across HA reboots.

Request history is a `deque` capped at `AI_REQUEST_HISTORY_CAP`. Each entry records: timestamp, skill name, input tokens, output tokens, cost, latency, success, error. No raw prompt content. No API key fragments.

---

## AI Providers

`CONF_AI_PROVIDER` (per zone, default `anthropic`) selects the provider. Existing installs have no such key and behave exactly as before. Contracts, specs and wrapper behaviour: [claude-api-spec.md § Provider Seam](claude-api-spec.md#provider-seam). `ClaudeAPIClient` and `ClaudeResponse` are provider-neutral and keep their historical names.

| Provider | Credential | Notes |
|---|---|---|
| `anthropic` (default) | API key in the zone's config entry | Cost tracking, monthly budget, tuning (temperature, max tokens, reasoning), tier fallback |
| `chatgpt` | One shared ChatGPT account for the whole HA instance | No cost tracking or tuning; manual-only and connected-owner-only; account-wide quota cooldown |

Adding a provider needs one backend module, one `ProviderSpec` in `PROVIDERS`, one entry in `ai_providers._PROVIDER_MODULES` (imported separately, so a broken provider module is logged and the others still register), and strings (labels for its own key and model fields, if it has them). The spec's `credential_setting` and `model_setting` name those config keys, and the options flow, settings rows, coordinator gate, diagnostics redaction and model fallback all read them from the spec. The wrapper, `chatgpt_account.py` and callers do not change (`tests/test_ai_backend.py` proves this with a fake API-key provider).

### Shared ChatGPT account (`chatgpt_account.py`)

Climate Advisor runs one config entry (and one AI client) per zone, but a refresh token rotates and has exactly one holder, so the tokens live once per HA instance in `ChatGPTAccountManager`:

- **Location:** `hass.data["climate_advisor_chatgpt_account"]`, never `hass.data[DOMAIN]` (coordinators only). `async_setup_entry` creates it and awaits its load on the event loop before the coordinator restores state; the loaded manager is handed to each zone's client by reference and read on every request, so connecting needs no reload.
- **Persistence:** `Store(hass, 1, "climate_advisor.chatgpt_account", private=True, atomic_writes=True)`. Created on the first sign-in, so a Claude-only install has no such file. Holds tokens, a stable host ID (never overwritten), `owner_user_id`, account display fields, state (`disconnected`, `connected`, `reauth_required`), the quota cooldown and its probe lease. A save is immediate and is read back to prove it ([HA-BOUNDARY-EXCEPTIONS §3](HA-BOUNDARY-EXCEPTIONS.md#3-chatgpt-account-store)). If a sign-in's read-back fails, `status()` reports `save_uncertain` (never stored) until the next sign-in or token request re-reads the store: tokens found there are adopted, keeping any newer quota cooldown; otherwise they are revoked.
- **Tokens:** `async_get_access_token()` uses one lock shared by connect, refresh, disconnect and keepalive, refreshes only when needed (5-minute skew, honouring `earliest_refresh_at`), saves the rotated token and only then releases it. A failed refresh keeps the credentials, and a rotated token it received, and is `no_retry`, as is a 401 after a refresh, so one request sends at most one refresh grant (see Known limits). A 401 for a token another zone has since replaced retries once with the saved token, with no grant. A terminal refresh error (`invalid_grant`, `refresh_token_expired`/`invalidated`/`reused`, ...) moves the account to `reauth_required` and raises the repair issue `chatgpt_account_reauth_required`. There is no reauth flow and these failures never raise `ConfigEntryAuthFailed` or `ConfigEntryNotReady`, so HVAC automation keeps running.
- **Setup:** runtime and SDK failures, and a failure in `chatgpt_backend.py` or `chatgpt_account.py`, do not fail setup. An import failure of `claude_api.py`, with AI on, gives `SETUP_ERROR`, as it did before providers existed. An exception from the `ClaudeAPIClient` or `AnthropicBackend` constructor is caught and the zone loads with no client; before providers existed it failed setup.
- **Keepalive:** an hourly timer that refreshes only if the last refresh is over 7 days old (refresh tokens last 30 days and the investigator may go unused). It exists only while connected and while at least one entry is loaded. It is an auth call, not an inference request.
- **Unload is not removal:** unload (which also runs on reload) only cancels the timer when the last loaded entry goes. `async_remove_entry` on the final entry revokes the token and deletes the store. If the delete fails the store is blanked instead, so a later setup starts disconnected with a new host ID. If that also fails, an error asks for `.storage/climate_advisor.chatgpt_account` to be deleted by hand, because re-adding the integration would load the old account. Tokens in a store that cannot be read cannot be revoked, and removal reports that as unconfirmed.
- **Redaction:** token key names are in `REDACTED_CONFIG_KEYS`; `tools/validate.py` scans for literal token values. Tokens never reach logs, exception text, `status()`, the REST status view or diagnostics.

### Setting up the ChatGPT provider

Sign-in runs entirely inside Home Assistant's options flow. There is no SSH, no file copy and no token copying, and no laptop script. It needs a Home Assistant **administrator** using the HA web interface (the flow takes the connecting user from the HTTP request; that was proven on HA 2026.10.4 only, not on 2026.8.3).

1. **Settings → Devices & services → Climate Advisor → Configure** (on any zone) **→ AI Settings**. The first screen asks only for the provider: choose the ChatGPT subscription. The next screen shows that provider's options (nothing is saved until you submit it): enable AI and save. Saving leads to the **ChatGPT Account** menu. Each zone picks its provider separately, but the account is shared by all zones, so you sign in once. (The provider is a separate screen because the form cannot change its fields as you pick; live conditional fields via `data_entry_flow` `visible` could replace it once the HA minimum reaches the release that adds them.)
2. Choose **Sign in with ChatGPT**.
3. The screen says that requests send this home's climate data to OpenAI under the connected ChatGPT plan, and shows a **Sign in with ChatGPT** link. Open it in the same browser and sign in. When you finish, the browser is sent to `http://127.0.0.1:54321/auth/callback?...`. **That page fails to load; this is expected.** Copy the full address from the address bar and paste it into the flow.
4. Home Assistant parses the pasted address (it is never fetched), checks it against the sign-in it started, and exchanges the code with OpenAI itself. It saves the account, records you as the **owner**, and checks the account with `GET /v1/models` (not an inference request). The last screen says you are signed in and shows the outcome of that check. A temporary 429/5xx there is reported as "not ready" and the sign-in is kept.
   If this zone has no model yet, the flow then shows the model list from your account and saves your choice (nothing is saved or reloaded before sign-in finishes). If the list cannot be loaded, the sign-in is kept and the last screen says to pick a model in AI Settings later.

   Your choice takes effect straight away, with no reload: the flow hands this zone's saved `ai_*` settings to the running coordinator and client (`async_apply_ai_settings`), also when the model is picked later in AI Settings. The last screen says AI is ready only if this zone can run a request now; otherwise it says what is missing (a model, AI turned off, or a reload). Saving a zone away from the ChatGPT subscription applies the same way, so it stops using the account at once. The zone stops starting requests before the build begins. The new provider is built against a copy of the settings, then the zone's settings and client change together and the stop lifts. If the build fails, the settings stay saved, the flow says so, and the zone starts no new AI requests until a reload applies them: it never keeps starting requests on the provider it was saved away from, though one already in an HTTP call or queued on the account lock can finish there. While AI is on and the zone has a client, the panel shows why; otherwise the flow's failure message and the repair notice are what explain it. A save on a zone already on an API-key provider still waits for a reload like every other setting.

If the pasted address is wrong or belongs to another attempt, the screen asks again (the attempt is kept). Each flow cancels only its own attempt (`pending_login_id`), so abandoning one flow never cancels another's sign-in. Any other failure, or an expired attempt (15 minutes), uses the attempt up and the retry shows a fresh link. Abandoning the flow drops an unfinished attempt. Errors show fixed messages only.

**This paste route is not documented by OpenAI and could break.** OpenAI documents a `127.0.0.1` callback and transferring credentials to a remote machine; pasting the failed redirect into Home Assistant is neither documented nor, as far as we know, forbidden. There is no fallback sign-in route: if OpenAI rejects this, sign-in will not work until a different route is built.

**Security and privacy facts**

- **Backups:** the saved tokens are in `.storage` and therefore in Home Assistant backups, which may be copied off the box. Treat backups as sensitive. Restoring an old backup restores a rotated-out refresh token: the next refresh is rejected, a repair issue appears and you sign in again.
- **Owner-only triggering:** only the connected owner can start an investigation (HTTP 403 for anyone else, on both the streaming and non-streaming paths), only by clicking Investigate. The investigator has no scheduled or automatic mode.
- The panel shows "Using ChatGPT plan", a "Manage usage" link and a "What is sent" notice at the Investigate control, and points to the flow for signing in; it does not sign in itself.

**Disconnect.** **Configure → AI Settings → ChatGPT Account → Disconnect ChatGPT** (confirm with the switch; offered when connected or when a new sign-in is needed), or the Disconnect button in the panel's ChatGPT Account card on the Settings tab. While an account is saved, signing in with a different one is refused with `different_account` and its new tokens are revoked: disconnect first. Any administrator may disconnect, so a departed owner cannot strand the install. It revokes the refresh token with OpenAI, then clears the account for every zone. If revocation cannot be confirmed (network failure, no revocation endpoint, non-200 answer after retries) the account is still cleared locally and Home Assistant says to also disconnect the app in ChatGPT Settings. The host ID is kept. Removing the last Climate Advisor entry also revokes and deletes the store.

**If the account stops working:** a `reauth_required` state raises the repair issue "ChatGPT sign-in needed" (it points to Configure, AI Settings, Sign in with ChatGPT) and AI features are paused; HVAC automation is unaffected. Sign in again from the same menu. Only the plan usage-limit error (`subscription_sharing_usage_limit_exceeded`, as a 429 or in the stream) pauses every zone for one hour; "Manage usage" is the action then. Any other 429 fails that one request as a temporary rate limit: other zones keep working, and the message gives the `Retry-After` seconds when OpenAI sends them. Run it again later; it is never retried automatically.

### Known limits

- Built on OpenAI's Sign in with ChatGPT access, which is documented as a preview; the API, error codes and limits may change.
- A refresh whose answer is lost (timeout, connection error, 5xx, unreadable 200) cannot be told apart from one the server never processed. The request stops after that one POST and the saved token is kept. The next user of that token sends it again, because it is the only one there is: a later request, another zone's request queued behind the lock, the model list on the AI Settings form, or the hourly keepalive tick once the last refresh is a week old. If the server had not rotated, that works. If it had, the new token died with the lost answer, the refresh is refused, and the account moves to `reauth_required` for every zone. There is no recovery short of a new sign-in.
- Output length cannot be capped (no output-limit parameter is sent), and `truncated` is never reported for this provider.
- Not exercised against the live service: a real plan usage-limit (429) response, a first install with no saved model, and a token scan of a real diagnostics download.
- Values that are **our own choices, not documented by OpenAI**: the 1-hour quota cooldown (`QUOTA_COOLDOWN_SECONDS`), the 15-minute probe lease (`QUOTA_PROBE_LEASE_SECONDS`), the 503 retries (2, after 1 s and 3 s), the weekly keepalive threshold and the 5-minute refresh skew. No reset time is inferred from a 429.
- The sign-in callback port (`54321`) is our own choice; the pasted-URL route depends on OpenAI accepting a `127.0.0.1` redirect that no process is listening on.
- Unconfirmed assumptions: the `earliest_refresh_at` format, `response.incomplete` reason values, revocation endpoint behaviour, and whether a second host ID can be registered on one account.
- The shared wrapper keys on `AIBackendError.no_retry` only (no provider or category check). A `no_retry` failure is returned or raised at once (never retried), still counts in `error_count` and request history, but does not touch `consecutive_failures` or the breaker state, on both the non-streaming and streaming paths. The ChatGPT backend sets it for every 429-class failure (`quota` and `rate_limit`, HTTP or stream code, including a 429 on token refresh), the early refusal under active cooldown, and `auth` with `reauth_required`. The account manager sets it on every refresh failure and the backend on a 401 after a refresh, so such failures do not open the breaker. Anthropic never sets it, so its 402 (`quota`) is still retried and counted.
- Whether household members may view output generated on the owner's plan, and whether backups containing tokens are acceptable to the provider, are open questions with the provider. This page makes no claim about the provider's terms.
- Requires an eligible ChatGPT plan; eligibility is decided by OpenAI. A 403 (`subscription_sharing_user_not_eligible` and similar) is surfaced and not retried.

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
streaming code. It is **manual-only**: started from the dashboard's Investigate button, with no
scheduled or automatic mode. The optional `focus` kwarg carries the user's described problem and
is prepended to the assembled context as a user-directed investigation target; with no `focus`
the same prompt, context-provider registry, parser and fallback run on the default provider
subset.

#### Context Sources

Fifteen context providers are registered in `ai_skills_context.py`'s `ContextProviderRegistry`
(`ai_skills_investigator.py:async_build_investigator_context` is a thin orchestrator that calls
`registry.select(focus)` and concatenates the result). Each provider is wrapped in its own
`try/except`; a failure in one does not abort the others — the section is marked unavailable
and assembly continues.

| Provider | Data source |
|---|---|
| `current_state` | `coordinator.data` + fresh HVAC runtime |
| `hvac_entity` | `hass.states.get(climate_entity_id)` |
| `state_cross_validation` | HVAC mode/action contradiction check + comfort-band deadband/swing check (ported from the retired activity context, Issue #563); the swing is the thermostat's learned one-sided hysteresis (Issue #1234), applied by the comfort incident's own rule (mode gate + 0.5 F floor + 2.5 F cap, Issue #1258); the LEARNING - THERMAL MODEL section also states what the swing means, the accepted-cycle count, per-reason reject counts and the coast (Issue #1233) |
| `last_briefing` | Most recent daily briefing text |
| `learning` | Compliance summary, thermal model, weather bias, suggestions, recent daily records |
| `thermal_pipeline` | Per-obs-type rejection/commit counts, `NEVER LEARNED`/`***PIPELINE FAILURE***` markers, engine status |
| `event_log` | Last 200 entries (last N hours, clamped 1–720). Includes: event-type counts, SYSTEM LOG RECORDS, TIMING CORRELATIONS, KNOWN OVERRIDE FALSE POSITIVES, RESTART HISTORY. [See ai-skills-spec.md](ai-skills-spec.md#event-log-provider) for sub-section details. |
| `activity_timeline` | Deterministic markdown event timeline table (ported from the retired activity context; never LLM-authored) |
| `override_details` | Setpoint override count/history/current-override-duration, Issue #321 stuck-grace critical warning, fan ownership transitions plus a fan override count (Issue #578) (ported, Issue #563) |
| `daily_summaries` | Historical multi-day trend summary, only populated when `hours > 36` (ported, Issue #563) |
| `config` | ~11 curated `coordinator.config` fields (comfort/setback temps, schedule, `ai_model`, `learning_enabled`) — not a full config dump |
| `operational_design` | Static prose block explaining fan_status values, deadband behavior, warm-day comfort guard, natural vent mode, contradiction suppression logic |
| `known_fixes` | Fix-history entries from `fix_history.jsonl` (streamed via `fix_history.py`, Issue #702), ranked by relevance to `focus` when given (else recency), bounded to `_KNOWN_FIXES_RECENT_COUNT` (15) plus any not-yet-deployed entry — rendered as the entry's `user_summary` when present, else `title` |
| `version` | Last 5 versions with a `user_summary`, from `fix_history.jsonl` (Issue #702; formerly `RELEASE_NOTES`) |
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

## Provider-aware settings rows and status card

`GET /config` builds the AI SETTINGS rows per zone from the zone's `ProviderSpec`: API key shown only for `credential_kind == api_key`, under the spec's `credential_setting` and only ever as "configured" or "not set"; reasoning effort, max tokens and temperature only with `supports_tuning`; budget only with `tracks_cost`; Auto Requests Per Day hidden when `plan_restricted`. Specs with `uses_claude_wording=True` (Anthropic) keep the original `CONFIG_METADATA` text unchanged; any other spec gets neutral wording and an "AI Provider" row (label plus account state). A spec whose `model_setting` is not `ai_model` shows that setting (value is what is sent). `get_status()["model"]` comes from `configured_model()` (the spec's resolver); an empty model shows "not set" and investigations are refused with a 400 until one is chosen.
