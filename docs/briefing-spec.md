<!-- Nav: → [Project Instructions](00-PROJECT-INSTRUCTIONS.md) | → [Briefing Examples](04-BRIEFING-EXAMPLES.md) -->

# Briefing Spec

## Status

_Tier 3 Territory Spec — authored as part of Issue #518's Documentation Convergence pass._

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
| What data does the briefing consume from the coordinator? | [§ Input Schema](#input-schema) |
| What timezone are timestamps displayed in? | [§ Timezone Display](#timezone-display) |
| How are learning suggestions ranked and filtered? | [§ Learning Suggestion Filtering](#learning-suggestion-filtering) |
| What happens when forecast data is unavailable? | [§ Stale/Missing Data Handling](#stalemissing-data-handling) |
| How does the grace period state appear in briefing output? | [§ Grace Period Injection](#grace-period-injection) |
| How does the header table stay consistent with the conversational body? | [§ Single Source of Truth for Warm-Day Timing (Issue #518)](#single-source-of-truth-for-warm-day-timing-issue-518) |
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

## Input Schema

`ClimateAdvisorCoordinator._build_briefing_text()` (`coordinator.py`) assembles every argument
`generate_briefing()` (`briefing.py`) takes and returns `(briefing_full, briefing_short)` —
the second call passes `verbosity="tldr_only"`. Nothing else calls `generate_briefing()`
directly. Key inputs, all read from `coordinator.config` / `coordinator.data` at call time
(never fetched fresh by `briefing.py` itself — it has no HA dependency, see module docstring):

| Argument | Source |
|---|---|
| `classification` | `DayClassification` from `classify_day()`, cached as `self._current_classification` |
| `comfort_heat`/`comfort_cool`/`setback_heat`/`setback_cool` | `self.config` |
| `wake_time`/`sleep_time` | `self.config["wake_time"/"sleep_time"]`, parsed via `_parse_time()` |
| `learning_suggestions` | `self.learning.generate_suggestions()` |
| `debounce_seconds`/`manual_grace_seconds`/`automation_grace_seconds` | `self.config`, falling back to `DEFAULT_*` constants |
| `grace_active`/`grace_source` | `self.automation_engine._grace_active` / `._last_resume_source` |
| `bedtime_setback_heat`/`bedtime_setback_cool` | `compute_bedtime_setback()` (`automation.py`), only computed for the matching `hvac_mode` |
| `adaptive_thermal_active` | `thermal_model.get("confidence", "none") != "none"` from `self.learning.get_thermal_model()` |
| `predicted_indoor_future`/`predicted_outdoor_future` | `self._last_predicted_indoor` / `_build_future_forecast_outdoor()` |
| `occupancy_mode` | `self._occupancy_mode` |

`generate_briefing()` is pure logic with no I/O — it never re-derives anything from `hass`,
so any staleness in the values above (e.g. a classification computed hours ago) is a
coordinator-layer concern, not a briefing-layer one.

## Single Source of Truth for Warm-Day Timing (Issue #518)

Before Issue #518, the header TLDR table (`_generate_tldr_table()`) read `c.window_close_time`
(the classifier's static `WARM_WINDOW_CLOSE_HOUR` constant) while the conversational body
(`_warm_day_plan()`, via `_derive_warm_day_events()`) independently derived a close time from
the live ODE-predicted indoor/outdoor curves (`nat_vent_cutoff`) — the two could disagree, and
the body's separate AC-start sentence could also contradict the header's `HVAC Mode: Off` by
promising a fixed AC clock-time with no awareness of window state. Fixed by computing
`_derive_warm_day_events()` **once** in `generate_briefing()` and passing the same `warm_events`
dict into both `_generate_tldr_table()` and `_warm_day_plan()` — the header now prefers
`warm_events["nat_vent_cutoff"]` when available, falling back to the classifier constant only
when no forecast curve exists. The AC-safety-net fact is now stated exactly once (in the
forecast-peak sentence, conditioned on windows being closed) rather than duplicated by a second,
window-unaware sentence — see [§6e in 08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md)
for the real automation-layer guard (`apply_classification()`'s `DEFER_PAUSED` branch) this
wording now matches. The "reopen windows... I'll turn off the AC" sentence only claims the
AC-off action when `ceiling_breach_time < recovery_time` (i.e. the AC could plausibly have
engaged first) — otherwise it states the reopen without an AC claim.

## Coherence Validation (Issue #518)

`tools/briefing_review.py` renders `generate_briefing()` across a `day_type` x `hvac_mode` x
setback-active scenario matrix and runs deterministic assertions: header/body window-close
times must agree, the AC-safety-net sentence must never promise a fixed clock time or use
"no action needed" boilerplate, the adaptive-setback footer must never appear when the header
says "No setback", and "I'll turn off the AC" must never appear without a preceding
forecast/ceiling-breach sentence. Run it with `python tools/briefing_review.py -v` to see the
full rendered text for every scenario. This is fully deterministic — no LLM is in the runtime
path (see the module docstring in `briefing.py`). When authoring a *new* scenario for the
matrix, review the rendered text with an agent for user-outcome soundness before adding it —
that review is a development-time step, not something the script does automatically.

## Grace Period Injection

`_grace_period_section()` returns an empty list (nothing rendered) unless `grace_active=True`
**and** `grace_source` is set. `grace_source == "manual"` renders the "hands-off window" framing
(`manual_grace_seconds`); anything else renders the "settling period" framing
(`automation_grace_seconds`). The literal phrase "grace period" is never used in body text
(voice-rule: no jargon) — only "hands-off window" / "settling period".

## Learning Suggestion Filtering

`Learning.generate_suggestions()` (`learning.py`) requires at least `MIN_DATA_POINTS_FOR_SUGGESTION`
records; below that it returns `[]` unconditionally. Suggestions are built pattern-by-pattern
(insertion order = the order patterns are checked in the function — there is no explicit score
or ranking pass) and filtered against two suppression sets: `dismissed_suggestions` (persisted
state) and suggestions in `settings_history` within the last 30 days that were either accepted
or marked `verdict == "incorrect"` in feedback. `briefing.py` renders whatever list it's given
with no further filtering or reordering of its own.

## Timezone Display

"Today"/"tomorrow" dates (see § Date Computation above) use `dt_util.now().date()` — HA's
configured local timezone. Body-text clock times (window open/close, AC-forecast peak,
reopen) are derived from `predicted_indoor_future`/`predicted_outdoor_future` timestamps,
which are already local-time-aware by the time they reach `briefing.py` (produced upstream by
`coordinator.py`'s forecast pipeline) — `briefing.py` only calls `.strftime(_FMT_HOUR)` on
them, performing no timezone conversion itself.

## Stale/Missing Data Handling

`_async_send_briefing()` (`coordinator.py`) returns early without generating a briefing if
`await self._get_forecast()` returns falsy — no partial/stale briefing is ever sent when the
day's forecast is unavailable. Within `generate_briefing()` itself, every optional input
degrades gracefully rather than raising: missing `predicted_indoor_future`/`predicted_outdoor_future`
falls back to the classifier's static window/close-time constants (see § Single Source of Truth
above); `bedtime_setback_heat`/`bedtime_setback_cool` of `None` falls back to
`comfort_{heat,cool} ± DEFAULT_SETBACK_DEPTH_F`; `learning_suggestions=None` simply omits that
section. There is no explicit "data is stale" messaging in the briefing itself — staleness is
prevented upstream by the coordinator's early-return, not communicated to the user.
