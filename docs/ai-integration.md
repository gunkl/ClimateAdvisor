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

**Issue #1336 (0.9.51):** the AI Investigator can run on any AI provider set up in Home
Assistant, through HA's AI Task. Built-in Claude (Climate Advisor's own Anthropic client and
key) still works but is deprecated. AI settings are install-wide. See
[Provider model](#provider-model-ai-task-first-issue-1336).

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Which AI providers can the Investigator use (Issue #1336)? | Any Home Assistant integration that offers an AI Task entity: OpenAI, Google, Anthropic, Ollama, OpenRouter, Nabu Casa Cloud, or a custom one. The occupant picks the entity in AI settings; empty = HA's preferred AI Task entity. Climate Advisor keeps no key. Built-in Claude still works but is deprecated (removal release not yet named by the owner). | [Provider model](#provider-model-ai-task-first-issue-1336) |
| What does the AI provider need to be set to (Issue #1336)? | A context window of at least 16k tokens for input (owner decision) and an output limit of at least 8192 tokens with reasoning at medium. A window that holds both (Ollama `num_ctx`) needs 24k; suggest 32768. Token sizes are estimates from character counts. | [Provider requirements](#provider-requirements-issue-1336) |
| Which provider limits can Climate Advisor read and warn about? | Anthropic, OpenAI, Google `max_tokens` (defaults 3000, 3000, 19400) and Ollama `num_ctx` (default 8192). OpenRouter, Nabu Casa Cloud and custom integrations can't be checked. Limits only ever warn; nothing blocks. | [Provider limits](#provider-limits-climate-advisor-can-read) |
| When is the occupant warned about a too-small limit? | Three times, from one reader (`ai_limits.async_read_limits`): after saving AI settings (`ai_limits_warning` step), before each run (`GET ai_limits`, *Run anyway* / *Cancel*), and after a run that hit a limit or came back cut short (error-code and cut-short notices). | [Limit checks](#limit-checks-warn-never-block) |
| Are AI settings per zone? | No, install-wide since 0.9.51. The same values sit on every zone's entry (saving in one zone copies them to all), one shared client serves every zone, and migration v21 unified existing per-zone settings. | [Install-wide settings](#install-wide-settings-and-the-shared-client) |
| What did the move to AI Task give up? | Live streaming (the report appears all at once), cost tracking and the monthly budget, and the "partial report" flag (HA's Anthropic and OpenAI integrations raise an error at the output limit). Accepted by the owner. | [What AI Task does not give](#what-ai-task-does-not-give-accepted-losses) |
| Which reasoning levels are offered? | Low and medium only (owner decision). A stored `high` becomes `medium` at migration v21, and the built-in client's extended-thinking path (16384-token budget) has no caller any more. | [Reasoning: medium at most](#reasoning-medium-at-most) |
| What stops the integration from hammering the AI provider during an outage? | A circuit breaker trips after `AI_CIRCUIT_BREAKER_THRESHOLD` consecutive failures, blocks all calls for `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS`, then probes once in half-open state before re-closing. Same breaker for both providers, and install-wide since the client is shared. | [Circuit Breaker](#circuit-breaker) |
| How are auto and manual AI requests counted separately, and what resets them? | `auto_requests_today` and `manual_requests_today` are distinct daily counters; `_reset_daily_counters_if_needed()` zeroes both at the top of every `async_request()` call when the date has changed. | [Rate Limiting](#rate-limiting) |
| What happens when a monthly budget is set and the cap is reached? | Built-in Claude only: `_check_budget()` returns `ClaudeResponse(success=False, budget_exceeded=True)` and the API call is never made; the cap resets at calendar month boundary. Value `0` means no cap. On AI Task the budget check is skipped (no cost is known). | [Monthly Budget](#monthly-budget) |
| What is the fixed return shape from every skill execution, regardless of success or fallback? | Every `async_execute()` call returns a 6-key dict: `{success, source, data, error, input_context, raw_response}`. Source is `"ai"`, `"fallback"`, or `"error"`. | [Return Contract](#return-contract) |
| Is there still a separate "activity report" skill? | No (Issue #563) — it was merged into `"investigator"`. The dashboard's silent/scheduled report and the on-demand focus-driven investigation are now two entry modes of the same skill, same prompt, same context-provider registry. | [investigator](#investigator-merged-issue-563) |
| Does the investigator still have its own model/token/reasoning config overrides? | No (Issue #563) — it shares the single `ai_model`/`ai_reasoning_effort`/`ai_max_tokens` config used everywhere else. `CONF_AI_INVESTIGATOR_MODEL`/`_MAX_TOKENS`/`_REASONING` constants remain defined (unused) only so the historical v13→v14 config migration doesn't break old installs. | [investigator](#investigator-merged-issue-563) |
| Why did the KNOWN-FIXED ISSUES context section used to grow without bound? | `_fix_is_relevant()`'s escape-hatch rule matched any entry with a non-empty `scope_not_covered` field, which was mandatory on every entry — so all 169 entries always passed regardless of the intended version-scoping. Fixed by removing the field entirely and bounding by recency count instead. | [Known Fixes Context](ai-skills-spec.md#known-fixes-context) |
| What are the full pre/post/invariant contracts for `ClaudeAPIClient.async_request()` and the circuit breaker? | The Tier 3 spec covers the 5-row circuit breaker transition table, guard sequence for `async_request()`, `ClaudeResponse` mutual-exclusivity invariants, budget reset trigger, all four retried exception types, and (Issue #1336) the AI Task transport and its error classification. | [Claude API Client — Territory Spec](claude-api-spec.md), [AI Task transport](claude-api-spec.md#ai-task-transport-issue-1336) |
| How do I move an install from built-in Claude to AI Task, and how is it tested on the owner's house? | Add HA's Anthropic integration and an AI Task subentry, untick *Recommended model settings*, set max tokens to 8192 or more and thinking effort to medium, then pick its entity in AI settings. The owner's live-house checklist (baseline, switch, negative check, compare) is in the migration doc. | [AI provider migration](ai-provider-migration.md), [Built-in Claude deprecation](builtin-claude-deprecated.md) |
| What are the full contracts for the skill registry, execution pipeline, and the registered skill? | The Tier 3 spec covers `AISkillRegistry` registration and lookup, the 6-step execution pipeline, return contract enforcement, the merged `investigator` skill's context/parse/fallback contracts, and caching behavior. | [AI Skills Framework — Territory Spec](ai-skills-spec.md) |

---

## Provider model: AI Task first (Issue #1336)

**What the occupant experiences.** In AI settings the occupant picks the AI provider they
already set up in Home Assistant, from an entity picker of AI Task entities (OpenAI, Google,
Anthropic, OpenRouter, Ollama, Nabu Casa Cloud, or any custom integration that offers AI
Task). Leaving it empty uses HA's preferred AI Task entity (Settings > System > AI).
Climate Advisor keeps no key, and one setting covers every zone. When they press
*Investigate* they get the same sectioned report, in their Home Assistant language. It
appears all at once instead of writing itself live, and it shows no cost. A household that
keeps their data at home can point it at a local Ollama model.

**Two providers, one setting (`ai_provider`):**

| `ai_provider` | What it is | Status | Who gets it |
|---|---|---|---|
| `ha_ai_task` | `homeassistant.components.ai_task.async_generate_data` on `ai_task_entity` (`""` = HA's preferred AI Task entity) | **Main path** | Default for new installs; migration v21 gives it to every install without a Claude key |
| `builtin_claude` | Climate Advisor's own Anthropic client and key (`claude_api.py`, `anthropic` package) | **Deprecated**, still works. Removal release to be decided by the owner (Phase 4 of the plan, held back) | Migration v21 keeps it on every install that had a key, so nobody's Investigator stops working on upgrade |

While `builtin_claude` is selected on any configured zone, two notices tell the occupant how
to move: the AI settings step's description, and one install-wide Repairs issue,
`builtin_claude_deprecated` (`ISSUE_BUILTIN_CLAUDE_DEPRECATED`, fixed id, like
`zone_resolution_ambiguous`). Setup raises it when any configured entry uses
`builtin_claude` and deletes it otherwise. The steps are in
[Built-in Claude deprecation](builtin-claude-deprecated.md).

**Boundary.** Calling `ai_task.generate_data`, and reading the chosen entity's registry entry
and config subentry options for the limit check, is an owner-approved exception
(2026-11-20): [HA Boundary Exceptions §3](HA-BOUNDARY-EXCEPTIONS.md#3-ai-task-generate-data-and-the-limit-lookup-issue-1336).
`manifest.json` adds `"after_dependencies": ["ai_task"]` (load order only; AI Task is not
required for the rest of Climate Advisor). `iot_class` stays `cloud_polling` (the GitHub
issues context and built-in Claude still call the cloud).

**What changes inside Climate Advisor: very little.** Without a `structure`, AI Task returns
plain text, which is what the `## SECTION` parser expects. The system prompt, the
16-language output instruction, the parser, the fallback, the circuit breaker, the daily
limits and request history are reused unchanged. Only the transport inside
`ClaudeAPIClient` differs ([AI Task transport](claude-api-spec.md#ai-task-transport-issue-1336)):

- HA's AI Task always uses its own fixed system prompt and `language=None`, so Climate
  Advisor sends `system_prompt + "\n\n" + user_message` as the task's `instructions`.
- The call runs inside `asyncio.timeout(AI_TASK_TIMEOUT_SECONDS)` (300 s), because `ai_task`
  sets no timeout of its own.
- It returns no token usage, no stop reason and no stream. Cost and tokens are recorded as
  0, and the monthly budget check is skipped.
- Every `HomeAssistantError` becomes a failure with a stable `error_code` (`ai_output_limit`,
  `ai_context_limit`, `ai_task_no_entity`, `ai_provider_error`) and goes down the existing
  failure path: circuit breaker, then the deterministic fallback.

### Install-wide settings and the shared client

**What the occupant experiences.** AI settings are the same for every zone. The AI settings
step in any zone's options says "applies to all zones", and saving it there sets every zone.
The Investigator is available on every zone's dashboard once AI is on anywhere. The daily
request limits count across all zones together.

- **Same values on every entry.** Climate Advisor still has one config entry per zone and no
  install-level entry. The AI keys (`ai_settings_sync.AI_SHARED_KEYS`: `ai_enabled`,
  `ai_provider`, `ai_task_entity`, the built-in Claude keys, the daily limits,
  `ai_investigator_enabled`, ...) are kept identical on every entry, so every existing
  per-zone reader keeps working.
- **On save** the AI step writes its own entry through `_commit_section` (which raises the
  usual `reload_needed_<entry_id>` Repairs issue), then
  `ai_settings_sync.async_copy_to_other_entries()` copies the values to every other zone.
  Values take effect on reload, like every other options section (Issue #573); reloading the
  edited zone refreshes the shared client.
- **One shared client** lives in `hass.data[f"{DOMAIN}_ai"]` as
  `{"client": ClaudeAPIClient, "skills": AISkillRegistry}`, outside `hass.data[DOMAIN]` so
  the zone count and `zone_registry` lookups are unchanged. `coordinator.claude_client` and
  `coordinator.ai_skills` point at the shared objects, so callers don't change. It is created
  when `ai_settings_sync.is_ai_configured(entry.data)` (AI on, and a key for built-in Claude;
  AI Task can rely on HA's preferred entity). Each zone's setup or reload calls
  `client.update_config(...)` with its entry data. The last zone to unload removes it, in
  the same `if not hass.data[DOMAIN]:` block that removes the panel.
- **What stays per zone:** each investigation still sends only its own zone's context and
  saves to its own zone's report history (Issue #1415). The circuit breaker, the daily
  counters and `ai_status` are install-wide: every zone shows the same AI status.
- Detail: [Multi-Zone Spec: AI settings](multi-zone-spec.md#ai-settings-are-install-wide-issue-1336).

### Migration v21 (config entry 20 → 21)

**What the occupant experiences on the first start of 0.9.51.** Nothing breaks. An install
with a Claude key keeps using built-in Claude and sees the deprecation notice. If exactly one
zone had AI on (the usual case), its settings now apply to every zone. If several zones had
AI on with different settings, one Repairs notice says which zone's settings were kept.

`__init__.async_migrate_entry`, `version == 20` step, per entry:

1. **Provider.** `ai_provider = ai_settings_sync.provider_of(...)`: an entry with a key stays
   `builtin_claude`; otherwise it gets `ha_ai_task`.
2. **Reasoning.** `ai_reasoning_effort` `"high"` becomes `"medium"`.
3. **Unify.** `pick_install_ai_values([e.data for e in hass.config_entries.async_entries(DOMAIN)])`
   picks the values of the stably-first zone with AI configured (the
   `hass.config_entries.async_entries(DOMAIN)` order that `get_default_coordinator()` and
   `list_zones()` share), from each zone's original values.
   - No zone has AI configured: nothing is unified.
   - Otherwise every entry's data becomes `unified_entry_data(entry.data, values)`.
   - **Backup key.** An entry whose own original AI values differ from the chosen ones keeps
     them under `ai_settings_backup_1336` (`AI_SETTINGS_BACKUP_KEY`), so no key or setting is
     lost. The backup is written once and is removed together with built-in Claude.
   - **Conflict notice.** If another configured zone had different values, the install-wide
     Repairs issue `ai_settings_unified` (`ISSUE_AI_SETTINGS_UNIFIED`, fixed id) is raised
     with placeholder `zone` = the kept zone's title: "AI settings now apply to all zones;
     Climate Advisor kept <zone>'s settings."

Downgrades are unsupported, so the new keys need only a release-notes warning.

## Provider requirements (Issue #1336)

**What the occupant experiences.** With a provider set to these values, a normal report
comes back whole. Below them the report can be cut off, and on HA's Anthropic and OpenAI
integrations a cut-off reply is an error: the occupant loses the whole AI report and gets the
non-AI fallback instead.

| Requirement | Value | Constant | Basis |
|---|---|---|---|
| Context window for input | **at least 16k tokens** (16384) | `AI_REQUIRED_CONTEXT_TOKENS` | Owner decision |
| Output limit, reasoning at **medium** | **at least 8192 tokens** | `AI_REQUIRED_OUTPUT_TOKENS` | Recommended; to be confirmed by the measured baseline ([migration step 1](ai-provider-migration.md)) |
| Combined window holding both (Ollama `num_ctx`) | **at least 24k** (24576); **suggest 32768** | `AI_REQUIRED_COMBINED_TOKENS` | 16k in + 8k out; 32768 is a convenient power of two, not a measured need |

**Token sizes are estimates.** Every token figure below is the character count divided by 4
**[estimate]**. No real token counts have been measured yet: the built-in client logs them
only at DEBUG (`claude_api.py`, "response finished" lines) and no retained log holds one. The
owner's baseline ([AI provider migration](ai-provider-migration.md), step 1) measures them.

**Why 16k in.** The system prompt is about 13,000 characters (≈3.3k tokens). The five stored
reports on the owner's install carried 19,656–48,074 characters of context (≈4.9k–12k
tokens). Total input is therefore ≈8k–15.3k tokens. 16k covers the largest, with little room
to spare.

**Why 8192 out, at medium.**

1. **The report alone is close to the HA defaults.** Stored reports are 9,405–11,450
   characters (≈2.4k–2.9k tokens). HA's Anthropic and OpenAI integrations default to a
   3000-token output limit, so 3000 is at or just above a normal report *before* any
   thinking.
2. **Thinking counts toward the same limit.** Anthropic: "Thinking tokens count toward the
   `max_tokens` limit", even when the thinking isn't returned.
3. **Climate Advisor already sized medium this way.** Its medium thinking budget is 4096
   tokens (`AI_REASONING_BUDGET_TOKENS`), and its old investigator default was 8192 with the
   note "must exceed MEDIUM reasoning budget (4096) + output buffer". ≈2.9k report + 4.1k
   thinking ≈ 7.0k, which fits under 8192 with ≈1.2k to spare. 4096 is not enough at medium.
4. **Adaptive-thinking models give no fixed number.** Anthropic calls effort "a behavioral
   signal, not a strict token budget". 8192 is the fixed-budget arithmetic above, not a
   guarantee; the baseline measures it.
5. **Cost.** Anthropic bills the tokens generated, not the limit, so a higher limit only costs
   more if the model actually writes more. Other providers' billing wasn't checked.

With medium as the highest level, nothing larger than 8192 is recommended.

**Reasoning on the provider.** On AI Task the reasoning level is a provider setting, not a
Climate Advisor one: Anthropic `thinking_effort` = medium on adaptive-thinking models (or a
thinking budget of 4096 or less on fixed-budget models); OpenAI `reasoning_effort` = medium.

### Provider limits Climate Advisor can read

**What the occupant experiences.** For the common providers Climate Advisor can tell, before
a run, that a limit is too small and name the setting to raise. For the others it says it
can't check and runs anyway.

From the HA 2026.10.4 source. These keys are **internal** to each integration, so a missing
or renamed key is treated as "unknown", never as an error.

| Provider (`domain`) | Setting read | Default for AI Task when absent | Compared against | At the default |
|---|---|---|---|---|
| Anthropic (`anthropic`) | subentry option `max_tokens` | **3000**. *Recommended model settings* mode saves the subentry without it | output, 8192 | ⚠️ warn |
| OpenAI (`openai_conversation`) | `max_tokens` | **3000**; raises an error at the limit | output, 8192 | ⚠️ warn |
| Google (`google_generative_ai_conversation`) | `max_tokens` | **19,400** (`RECOMMENDED_AI_TASK_MAX_TOKENS`) | output, 8192 | ✅ OK |
| Ollama (`ollama`) | `num_ctx` (holds input and output; no output cap is set) | **8192** (minimum 2048) | combined, 24576 | ⚠️ warn |
| OpenRouter (`open_router`) | none: the integration has no output-limit option | — | — | unknown, "can't check" |
| Nabu Casa Cloud, custom integrations | integration-specific | — | — | unknown, "can't check" |

No provider stores its model's context window in HA, so the 16k input requirement is
documented only; for Ollama it is part of the combined check.

**How the reader finds them** (`ai_limits.async_read_limits(hass, config)`): resolve the
entity (`ai_task_entity`, or HA's preferred entity from the `ai_task` preferences'
`gen_data_entity_id`, read defensively), find it in the entity registry, take its
`config_entry_id` and `config_subentry_id`, then read
`hass.config_entries.async_get_entry(id).subentries[sub_id].data`. Built-in Claude reports one
output check, for `ai_max_tokens`. The result shape is the
[`GET ai_limits` response](rest-api.md#get-apiclimate_advisorai_limits-issue-1336).

### Limit checks: warn, never block

**What the occupant experiences.** If a limit looks too small they are told which setting to
raise, in which integration, and can still go ahead. Nothing is ever refused because of a
limit (owner decision).

One reader, called at three moments:

| When | Where | What the occupant sees |
|---|---|---|
| **(a) On save** of the AI settings step | config flow: after `_commit_section` and the copy to other zones, `ai_limits.async_read_limits`; if `ok == false`, a follow-up step `ai_limits_warning` (no fields) | The failed checks (setting, integration, value, required). Saving has already succeeded; submitting the step finishes |
| **(b) Before each run** | dashboard: `GET /api/climate_advisor/ai_limits` before `apiFetchStream('ai_investigate', ...)` | `ok == false`: a warning listing each failed check, with *Run anyway* / *Cancel*. `readable == false`: a one-line note ("can't check this provider's limits; it needs a 16k-token context and an 8k-token output limit") and the run proceeds |
| **(c) After the run** | SSE `error` event `error_code`, or `done` event `cut_short` | `ai_output_limit` / `ai_context_limit`: the setting to raise. `ai_task_no_entity`: "choose an AI Task entity, or set a default under Settings > System > AI". `ai_provider_error`: a generic failure. `cut_short` (`ai_report_cut_short`): the report came back missing required sections; the notice names the setting |

`cut_short` is true when `parse_investigation_response` shows any required section empty
while `full_text` is non-empty, or when the built-in client reports `truncated`. Notices are
16-language strings (frontend `ai` i18n area). Contracts:
[claude-api-spec.md](claude-api-spec.md#ai-task-transport-issue-1336),
[ai-skills-spec.md](ai-skills-spec.md#cut-short-and-error-codes-issue-1336),
[rest-api.md](rest-api.md#ai-investigate-stream-fields-issue-1336).

### What AI Task does not give (accepted losses)

**What the occupant experiences.** The report appears all at once when it is done, there is
no spending figure or monthly cap in Climate Advisor, and a report that runs out of room is
replaced by the non-AI fallback with a notice, rather than kept half-written. The owner
accepted all three (2026-11-20).

| Lost on AI Task | Why | What replaces it |
|---|---|---|
| **Live streaming** (status lines, thinking view, sections writing themselves) | `async_generate_data` returns one final result; only the first caller that opens a chat log may attach a delta listener | Status lines still stream; the text arrives as one `text` event, then `stop` |
| **Cost tracking and the monthly budget** | The result carries no token usage (providers write it only to the conversation trace) | Spending limits move to the provider account (for Anthropic, the console usage page). Daily request-count limits stay in Climate Advisor |
| **The partial-report flag** (`truncated`, keep a cut-off report) | No stop reason is returned, and HA's Anthropic and OpenAI integrations raise an error at the output limit, so nothing partial comes back | The requirement, the pre-run warning, `error_code` `ai_output_limit` and the `cut_short` notice |

Built-in Claude keeps all three while it remains.

### Reasoning: medium at most

**What the occupant experiences.** The reasoning choice offers Low and Medium. An install
that had High gets Medium after the upgrade, which keeps a normal report inside an 8192-token
output limit.

- `AI_REASONING_OPTIONS = [low, medium]` (owner decision). `AI_REASONING_HIGH` stays defined
  only so migration v21 can recognise and convert old stored values.
- The built-in client's extended-thinking branch (`thinking.type = enabled` with a
  16384-token budget at `high`) has no caller any more. Built-in Claude at low/medium is
  unchanged: no thinking control on legacy-shape models, adaptive thinking with
  `output_config.effort` on adaptive-shape models
  ([Per-Model Capability Table](claude-api-spec.md#per-model-capability-table-issue-572)).
- On AI Task the level is the provider's own setting (see
  [Provider requirements](#provider-requirements-issue-1336)).

---

## Scope

**Owns:**
- The AI transport: Anthropic API communication for built-in Claude and the AI Task call for `ha_ai_task`, both inside `ClaudeAPIClient` in `claude_api.py` (Issue #1336)
- The provider limit reader (`ai_limits.py`, Issue #1336) and the install-wide AI settings helper (`ai_settings_sync.py`)
- Circuit breaker state machine, rate-limit counters, monthly budget accumulator, and retry logic
- Cost estimation per model and per request (built-in Claude only; AI Task records 0)
- Persistence of stats (counters, monthly cost) across HA restarts
- AI skill registry: registration, lookup, and execution pipeline (`AISkillRegistry` in `ai_skills.py`)
- Skill definitions: `AISkillDefinition` blueprints including context builders, response parsers, and fallbacks
- Context assembly for the merged `"investigator"` skill (`ai_skills_investigator.py` orchestrates; individual context providers live in `ai_skills_context.py`)
- Cross-validation logic (HVAC contradiction check, comfort band flag) before each Claude call
- The deterministic fallback path when Claude is unavailable or returns an error

**Explicitly does NOT own:**
- Sensor entity for `sensor.climate_advisor_ai_status` — owned by `sensor.py` (entity ids depend on the zone title and install language; look up by unique id — [09-DEBUGGING-GUIDE.md](09-DEBUGGING-GUIDE.md#1-ha-sensor-entities-recommended-first))
- Report history storage (`get_investigation_report_history()`, `store_investigation_report()`) — owned by `coordinator.py`. The AI Activity Report skill/service and its separate `get_ai_report_history()`/`_ai_report_history` storage were retired entirely in Issue #578 (superseded by the deterministic, non-AI Activity Record).
- Thermal model computation — owned by `learning.py`
- Daily record persistence — owned by `coordinator.py` + `learning.py`
- REST API endpoints — owned by `api.py`

---

## Responsibilities

- Route each request by `ai_provider`: `ha_ai_task` calls `ai_task.async_generate_data` on `ai_task_entity` (or HA's preferred entity), `builtin_claude` calls the Anthropic API (Issue #1336)
- Built-in Claude only: authenticate with the Anthropic API using the HA config-entry API key; recreate the client when the key changes via `update_config()`
- Classify every AI Task failure into a stable `error_code` and read provider limits for the three warnings (Issue #1336)
- Enforce a circuit breaker: count consecutive failures, open the breaker on threshold, block calls while open, probe with one request in half-open state
- Enforce daily rate limits independently for auto-triggered and manual-triggered requests
- Enforce a separate investigator rate limit gated also on `CONF_AI_INVESTIGATOR_ENABLED` — this now gates only the on-demand/focus-driven call path (`ClimateAdvisorInvestigateView`), not skill registration itself, so the silent/narration mode stays available to anyone with AI enabled (Issue #563)
- Enforce a monthly cost cap; accumulate `estimated_cost` on every successful call; reset at calendar month boundary
- Retry failed API calls with exponential backoff (`AI_MAX_RETRIES` attempts); skip backoff after the final attempt
- Estimate cost per request from a model-prefix lookup table (`_MODEL_COSTS`)
- Built-in Claude only: shape each request from `AI_MODEL_CAPABILITIES` (adaptive thinking + `output_config.effort` on adaptive models). The `high` extended-thinking path was removed with the reasoning options (Issue #1336)
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
| `claude_api.py` | `ClaudeAPIClient`: the AI transport (AI Task call, or Anthropic API for deprecated built-in Claude), circuit breaker, rate limits, budget, retry, cost estimation, persistence. One shared instance per install in `hass.data[f"{DOMAIN}_ai"]` (Issue #1336) |
| `ai_limits.py` | `async_read_limits(hass, config)`: reads the chosen provider's output limit / `num_ctx` and returns the `ai_limits` checks (Issue #1336) |
| `ai_settings_sync.py` | Install-wide AI values: `AI_SHARED_KEYS`, `provider_of`, `is_ai_configured`, `pick_install_ai_values`, `unified_entry_data`, `async_copy_to_other_entries` (Issue #1336) |
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

`_BudgetTracker` maintains a running `monthly_cost` and a `budget_month` marker. **Built-in Claude only** (Issue #1336): on AI Task no cost is known, so `estimated_cost` is 0 and the budget check is skipped; spending limits live in the provider account.

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

### Extended Thinking (removed, Issue #1336)

Built-in Claude used to inject a fixed-budget thinking block (16384 tokens) and force `temperature=1` when `reasoning_effort == AI_REASONING_HIGH`. With reasoning limited to low and medium, and a stored `high` migrated to `medium`, that path has no caller. Adaptive-shape models still get `thinking = {"type": "adaptive"}` and `output_config.effort` at low and medium, and their `temperature` handling follows `AI_MODEL_CAPABILITIES` ([Per-Model Capability Table](claude-api-spec.md#per-model-capability-table-issue-572)).

### Authentication

Built-in Claude only; the AI Task path needs no key (the provider's own HA integration holds it). The API key is read from `config.get(CONF_AI_API_KEY, "")` at client construction. The key is static — no token refresh. When `update_config()` detects a key change (by comparing old vs. new values), it tears down and recreates the `AsyncAnthropic` client. The key is never written to request history, log statements, or API responses.

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
| `ClaudeAPIClient.async_request()` / `async_request_streaming()` | `claude_api.py` | `ai_skills.py` (`async_execute` / `async_execute_streaming`) | Single gate for every AI call, either provider |
| `ClaudeAPIClient.update_config()` | `claude_api.py` | `coordinator.py` (each zone's setup or reload, Issue #1336) | Refresh the shared client from the zone's entry data (identical on every entry); recreates the Anthropic client on key change |
| `ai_limits.async_read_limits()` | `ai_limits.py` | `api.py` (`GET ai_limits`), `config_flow.py` (`ai_limits_warning` step) | One limit reader for the save-time and pre-run warnings (Issue #1336) |
| `ai_settings_sync.async_copy_to_other_entries()` | `ai_settings_sync.py` | `config_flow.py` (AI settings save) | Copy the saved AI values to every other zone (Issue #1336) |
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

10. **No high reasoning path (Issue #1336).** `AI_REASONING_OPTIONS` is `[low, medium]`; no request sends the 16384-token extended-thinking budget. (This replaced "extended thinking forces `temperature=1`", which applied only at `high`.)

11. **Limits only warn.** No limit check refuses a save or a run; `ok == false` only ever produces a warning with *Run anyway* (Issue #1336, owner decision).

12. **AI settings are identical on every zone's entry.** Every write of an `AI_SHARED_KEYS` value goes through the AI step's save (copied to every entry) or migration v21; there is one shared client per install (Issue #1336).

13. **The AI Task path never writes to the provider.** It calls `ai_task.async_generate_data` and reads the chosen entity's registry entry and subentry options; nothing else ([boundary exception §3](HA-BOUNDARY-EXCEPTIONS.md#3-ai-task-generate-data-and-the-limit-lookup-issue-1336)).

---

## Disclosure Path

← Tier 1: [00-PROJECT-INSTRUCTIONS.md](00-PROJECT-INSTRUCTIONS.md)
← Tier 2 parent: [02-ARCHITECTURE-REFERENCE.md](02-ARCHITECTURE-REFERENCE.md)
→ Tier 3: [Claude API Client spec](claude-api-spec.md) · [AI Skills Framework spec](ai-skills-spec.md)
→ Guides: [AI provider migration](ai-provider-migration.md) · [Built-in Claude deprecation](builtin-claude-deprecated.md) · [HA Boundary Exceptions §3](HA-BOUNDARY-EXCEPTIONS.md#3-ai-task-generate-data-and-the-limit-lookup-issue-1336)
↔ Siblings: [Learning Engine Design](05-LEARNING-ENGINE-DESIGN.md) · [Computation Reference](08-COMPUTATION-REFERENCE.md) · [State Persistence](state-persistence.md)
