<!-- Nav: ← [AI Integration Brief](ai-integration.md) | ↔ [AI provider migration](ai-provider-migration.md) -->

# Deprecated: Built-in Claude (Issue #1336)

**What the occupant experiences.** If your AI Investigator uses Climate Advisor's built-in
Claude (an Anthropic API key pasted into Climate Advisor's AI settings), it keeps working.
You see a deprecation note in AI settings and one notice in **Settings > Repairs**. Both tell
you to move to Home Assistant's own Anthropic integration. After you move, you get the same
Claude model and the same report, and Climate Advisor no longer holds your key.

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| Does built-in Claude stop working on upgrade? | No. Migration v21 keeps every install that had a key on `builtin_claude`. It is deprecated, not removed. | [Status](#status) |
| When is it removed? | Not decided. The owner will name the removal release later. | [Status](#status) |
| How do I move? | Add HA's Anthropic integration and an AI Task, untick *Recommended model settings*, set max tokens to 8192 and thinking effort to medium, then choose the AI Task entity in Climate Advisor's AI settings. | [How to move](#how-to-move) |
| What do I lose by moving? | Live streaming of the report, Climate Advisor's cost figure and monthly budget, and the "partial report" flag. | [What changes](#what-changes-when-you-move) |

## Status

- **Deprecated in 0.9.51** (Issue #1336). The provider choice `ai_provider` defaults to
  `ha_ai_task` for new installs. An existing install with a key stays on `builtin_claude`
  until the occupant switches.
- **Notices:** the AI settings step's description while `builtin_claude` is selected, and the
  install-wide Repairs issue `builtin_claude_deprecated`, raised at setup when any configured
  zone uses built-in Claude and deleted otherwise. Both are in every shipped language.
- **Still supported until removal:** the API key, model choice, reasoning (low or medium; a
  stored `high` became `medium`), max tokens, temperature, the monthly budget and cost
  figures, and live streaming. These fields are labelled "Built-in Claude only" in AI
  settings.
- **Removal release:** to be decided by the owner. At removal, an install still on built-in
  Claude becomes `ha_ai_task` with no entity chosen: AI stays configured but idle (or uses
  HA's preferred AI Task entity if one is set), and a Repairs notice says to choose an AI Task
  entity. The `anthropic` package requirement is dropped then.
- **Boundary:** the direct Anthropic cloud call stays listed in `claude.md` until removal.

## How to move

1. In Home Assistant, **Settings > Devices & services > Add integration > Anthropic**, and
   enter your API key.
2. Add an **AI Task** to it.
3. **Untick "Recommended model settings".** That mode saves no max tokens, so it runs at the
   default of **3000**, which is too small for a full report. Then:
   - choose the same Claude model you use today;
   - set **max tokens to 8192** or more;
   - set **thinking effort to medium**.
4. In Climate Advisor, **Configure > AI settings** in any zone (the settings apply to all
   zones), choose the new `ai_task.*` entity.
5. Reload the zone (Repairs shows "reload needed"). The deprecation note and the Repairs
   notice clear.

Your Anthropic account stays the same; usage and cost now show only on the Anthropic
console's usage page. The owner's full test checklist is in
[AI provider migration](ai-provider-migration.md).

## What changes when you move

| | Built-in Claude | Anthropic through HA's AI Task |
|---|---|---|
| Where the key lives | Climate Advisor's AI settings | HA's Anthropic integration |
| The report | Writes itself live | Appears all at once when done |
| Cost and monthly budget in Climate Advisor | Yes | No (use the Anthropic console) |
| A report that runs out of room | Kept and marked cut short | Anthropic raises an error: you get the non-AI fallback and a notice naming max tokens |
| Daily request limits, circuit breaker, fallback report | Yes | Yes, unchanged |
| Reasoning | Climate Advisor's setting (low or medium) | The integration's thinking effort (set medium) |
| Limit check before a run | `ai_max_tokens` | The subentry's `max_tokens` |

Requirements and the reasons behind them: [AI Integration: provider requirements](ai-integration.md#provider-requirements-issue-1336).

## Disclosure Path

← Tier 2 parent: [AI Integration Brief](ai-integration.md)
↔ [AI provider migration](ai-provider-migration.md) · [HA Boundary Exceptions](HA-BOUNDARY-EXCEPTIONS.md#3-ai-task-generate-data-and-the-limit-lookup-issue-1336)
