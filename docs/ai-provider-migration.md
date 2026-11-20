<!-- Nav: ← [AI Integration Brief](ai-integration.md) | ↔ [Built-in Claude deprecation](builtin-claude-deprecated.md) · [Claude API Client spec](claude-api-spec.md) -->

# AI Provider Migration — Owner's Live-House Test (Issue #1336)

**What the occupant experiences.** The owner's own install is the first to move from built-in
Claude to Home Assistant's AI Task. After the move, the AI Investigator gives the same
sectioned report in the same language, from the same Claude model, through HA's Anthropic
integration. Climate Advisor no longer holds the key. This checklist proves that on the real
house, and measures the token counts the requirements were estimated from.

Run it after the owner deploys the release with Issue #1336 (0.9.50). The owner decides
when to deploy; nothing here needs calendar time beyond the two investigations per step.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Why measure before switching? | The 16k-input and 8192-output requirements are estimates from character counts (chars ÷ 4). Built-in Claude logs real `input_tokens` / `output_tokens` at DEBUG; AI Task returns none. Step 1 is the only chance to measure them. | [Step 1](#1-baseline-on-built-in-claude-medium-reasoning) |
| What is the one setting most likely to go wrong? | HA's Anthropic AI Task subentry in *Recommended model settings* mode saves no `max_tokens`, so it runs at 3000. Untick it and set 8192 or more. | [Step 3](#3-set-the-subentrys-limits) |
| How is the warning path tested? | Set max tokens to 3000, check the save-time and pre-run warnings appear, press *Run anyway*, check the cut-short notice or the fallback, then restore 8192. | [Step 5](#5-negative-check-a-too-small-limit) |
| Where do the results go? | A comment on Issue #1336, after the owner's OK to post. | [Step 7](#7-record-the-results) |

## Checklist

### 1. Baseline on built-in Claude, medium reasoning

- [ ] Confirm the zone's AI settings show built-in Claude with reasoning **medium** (a stored
      `high` was migrated to `medium`).
- [ ] Turn on DEBUG logging for `custom_components.climate_advisor.claude_api`.
- [ ] Run one **24 h** investigation and one **7-day** investigation.
- [ ] Record each run's logged `input_tokens` and `output_tokens` ("response finished" DEBUG
      lines). These are the first measured counts.
- [ ] Compare them with the estimates: input ≈8k–15.3k (requirement 16k), report ≈2.4k–2.9k
      plus thinking ≈4.1k at medium ≈7.0k (requirement 8192). If output plus thinking came
      close to 8192, note it: step 3 then uses a higher max tokens.
- [ ] Keep the two reports (they are stored in the zone's report history) for step 6.

### 2. Add HA's Anthropic integration and an AI Task

- [ ] **Settings > Devices & services > Add integration > Anthropic**, with the owner's API key.
- [ ] Add an **AI Task** subentry to it. Note the new `ai_task.*` entity id.

### 3. Set the subentry's limits

- [ ] **Untick *Recommended model settings*.** In recommended mode the subentry is saved
      without `max_tokens`, so it runs at the 3000 default.
- [ ] Choose the **same Claude model** as today.
- [ ] Set **max tokens to 8192 or more** (more if step 1 came close to 8192).
- [ ] Set **thinking effort to medium**.

### 4. Choose the entity in Climate Advisor

- [ ] In **any** zone's **Configure > AI settings** (they apply to all zones), choose the
      new `ai_task.*` entity as the AI provider.
- [ ] Check that the deprecation note in AI settings and the `builtin_claude_deprecated`
      Repairs issue clear (after the zone reloads).
- [ ] Check that no limit warning shows on save (`ai_limits_warning` step) or before a run.
- [ ] Check that every zone shows the same AI settings and the same AI status.

### 5. Negative check: a too-small limit

- [ ] Set the subentry's max tokens to **3000**.
- [ ] Save AI settings again: the save-time warning lists `max_tokens` 3000 against 8192 for
      the Anthropic integration. Saving still succeeds.
- [ ] Start an investigation: the pre-run warning appears with *Run anyway* / *Cancel*.
- [ ] Press **Run anyway**: the run ends in the "cut short" notice or the non-AI fallback
      with the output-limit notice (`error_code` `ai_output_limit`).
- [ ] Restore max tokens to **8192 or more**.

### 6. Repeat and compare

- [ ] Run the **24 h** and **7-day** investigations again, in **English**.
- [ ] Switch the HA language to one non-English language and run them again.
- [ ] Compare with the step 1 reports: every section present, the occupant-facing sections in
      the chosen language, citations still present.
- [ ] Read tokens and cost from the Anthropic console's usage page: it is now the only record.

### 7. Record the results

- [ ] Write up the measured counts (step 1), any change to the 8192 / 16k figures, the
      negative check and the comparison.
- [ ] Post them as a comment on Issue #1336 **only after the owner's OK to post**.

## What this checklist does not cover

- **Other providers** (OpenAI, Google, Ollama, OpenRouter) are covered by the unit and
  real-HA tests with a fake AI Task entity, not by the live house.
- **Removing built-in Claude** is a later release the owner has not named yet; see
  [Built-in Claude deprecation](builtin-claude-deprecated.md).

## Disclosure Path

← Tier 2 parent: [AI Integration Brief](ai-integration.md)
↔ [Built-in Claude deprecation](builtin-claude-deprecated.md) · [Claude API Client spec](claude-api-spec.md) · [REST API: ai_limits](rest-api.md#get-apiclimate_advisorai_limits-issue-1336)
