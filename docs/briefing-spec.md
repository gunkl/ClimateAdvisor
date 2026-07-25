<!-- Nav: → [Project Instructions](00-PROJECT-INSTRUCTIONS.md) | → [Briefing Examples](04-BRIEFING-EXAMPLES.md) -->

# Briefing Spec

## Status

_Tier 3 Territory Spec — stub. Sections marked (TBD) need authoring._

## Scope

Covers `briefing.py`: how the morning briefing context is assembled from coordinator data,
how today/tomorrow dates are computed, and how the output text is structured and delivered.

The briefing is the primary user interface for Climate Advisor. It fires at `briefing_time`
(default 06:00, configurable) and sends a notification summarizing the day's plan, any
learning suggestions, and any required human actions.

## Anchors

| Question | Location |
|---|---|
| How are "today" and "tomorrow" dates defined? | [§ Date Computation](#date-computation) |
| What data does the briefing consume from the coordinator? | _(TBD)_ |
| What timezone are timestamps displayed in? | _(TBD)_ |
| How are learning suggestions ranked and filtered? | _(TBD)_ |
| What happens when forecast data is unavailable? | _(TBD)_ |
| How does the grace period state appear in briefing output? | _(TBD)_ |
| What does each day-type briefing look like? | [Briefing Examples](04-BRIEFING-EXAMPLES.md) |

## Date Computation

The briefing fires at `briefing_time` (default 06:00), which typically precedes `wake_time`
(default 06:30) and the daily classification event. The briefing uses:

```python
today_date = dt_util.now().date()  # calendar today
tomorrow_date = today_date + timedelta(days=1)
```

This is a **calendar-based computation**, not anchored to the automation's "day started"
state (set at wake_time). The briefing's concept of "today" and "tomorrow" is always the
current and next calendar day at the moment of execution, regardless of whether the morning
classification has fired.

**Forecast data**: the briefing receives pre-processed `today_high`, `today_low`,
`tomorrow_high`, `tomorrow_low` values from `_get_forecast()` (via the coordinator's
`data` dict). It does not perform its own forecast date matching. See
[Forecast Pipeline Spec](forecast-pipeline-spec.md) for how these values are derived.

## Known Gaps (for Scribe)

- Input/output schema not yet documented
- Grace period injection logic not specified
- Learning suggestion ranking order not specified
- Timezone display format for timestamps not specified
- Error handling when coordinator data is stale or unavailable not specified
