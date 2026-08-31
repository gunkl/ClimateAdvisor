<!-- Nav: ← [docs/00-PROJECT-INSTRUCTIONS.md] | → [scheduler.py#L1 | coordinator.py#L3527 | automation.py#L3137 | config_flow.py#L1154 | thermal_lead_time.py#L21] | ↔ [docs/08-COMPUTATION-REFERENCE.md] -->

# Time-of-Use (TOU) Scheduler — Territory Spec (Tier 3)

## Anchors

| Question | Short answer | → Full answer |
|---|---|---|
| What does a `cost_period` schedule contain, and what does it deliberately NOT contain? | `id`, `name`, `days`, `start`, `end`, `cost_tag` (`"high"`/`"low"`). No temperature field of any kind. | [Scope](#scope), [Data Model](#data-model) |
| How is midnight/day-of-week resolved for a schedule spanning midnight? | Two explicit calendar-day membership tests (today's weekday for the pre-midnight portion, yesterday's for the post-midnight portion) — never a single numeric hour shift. | [Midnight & Day-of-Week Resolution](#midnight--day-of-week-resolution) |
| What temperature does pre-conditioning actually bank toward? | Whatever `resolve_comfort_heat()`/`resolve_comfort_cool()` (nat_vent_gate.py) already resolves for that moment — plain `comfort_heat`/`comfort_cool`, or the sleep-window value if the lead-time window overlaps the sleep schedule. No new config field. | [Banking Target Resolution](#banking-target-resolution) |
| Why does the "coast" phase after pre-conditioning need no dedicated code? | `_apply_comfort_band()` issues a single-setpoint *threshold* command; a real thermostat only acts once indoor crosses it. Confirmed by a prerequisite test before any implementation began — not assumed. | [Coast Phase — Confirmed, Not Assumed](#coast-phase--confirmed-not-assumed) |
| How is the pre-conditioning lead time computed? | `thermal_lead_time.compute_lead_minutes_from_rate()` — one formula shared by 4 call sites (this feature, adaptive pre-heat, ODE ceiling guard, warm-day briefing), each with its own bounds/fallback. | [Lead-Time Computation](#lead-time-computation) |
| Where does the chart's Target Band show pre-conditioning, and how does it stay in sync with what's actually commanded? | `_compute_target_band_schedule()`'s `tou_precondition_window` parameter — an additive override applied after the normal band, same shape as the existing `pre_cool_target` mechanism. Both call sites (chart builder, ODE curve builder) always receive the same resolved window. | [Chart Coverage](#chart-coverage) |
| Does the chart show the actual system target (not just the comfort band range), and does it know about TOU/nat-vent? | Yes — one bold "Target" line, historical half from `chart_log`'s `setpoint`/`nat_vent_target` fields, forward half from `_compute_effective_target_forward()`'s 3-tier TOU→nat-vent→band-edge derivation. Supersedes the old dead `predicted_setpoint`/`historical_setpoint` fields and the thin `defense_lines` overlay. | [Unified Target Line](#unified-target-line-issue-786-follow-up-phase-3) |

## Scope

- **Files:**
  - `custom_components/climate_advisor/scheduler.py` — schedule storage shape, resolution, banking-phase decision (the whole module)
  - `custom_components/climate_advisor/thermal_lead_time.py` — shared lead-time-from-rate helper (the whole module)
  - `custom_components/climate_advisor/coordinator.py` — `_resolve_tou_schedule_state()` (L3527), `_apply_tou_schedule()` (L3561), `_tou_precondition_window_tuple()` (L3572), `_format_tou_ends()` (L7900), the `_compute_target_band_schedule()`/`_build_predicted_indoor_future()` `tou_precondition_window` parameter, the `_compute_automation_status()` TOU branch
  - `custom_components/climate_advisor/automation.py` — `apply_tou_precondition()` (L3137), `_set_temperature_for_mode()`'s `target_override` parameter (L3100)
  - `custom_components/climate_advisor/config_flow.py` — `async_step_scheduler()` (L1154), `async_step_scheduler_edit()` (L1179)
  - `custom_components/climate_advisor/nat_vent_gate.py` — `resolve_comfort_cool()` (the cool-side counterpart to the pre-existing `resolve_comfort_heat()`), reused (not reimplemented) by `scheduler.py`

What this spec does NOT cover:
- The overnight weather-trend-driven pre-cool mechanism (`compute_pre_cool_target()`, `resolve_pre_cool_modifier()`, `automation.py`) — a structurally distinct, older mechanism kept architecturally separate from TOU banking (two different reasons to pre-cool, not one).
- A `comfort_zone` schedule type (user-defined arbitrary comfort-band windows) — part of the original feature request but explicitly deferred to a future issue; this spec's `Schedule` shape supports only `cost_period`.
- The ODE ceiling guard (`ode_ceiling_guard.py`) and adaptive pre-heat (`automation.py::_schedule_pre_condition()`) beyond their shared use of `thermal_lead_time.compute_lead_minutes_from_rate()`.

## Origin

GitHub issue #786, following a request on the project roadmap issue (#11) from a user (DeppressedCabbage) who manually pre-cools/pre-heats a few degrees before a scheduled high electricity-rate window. The design generalizes this to arbitrary day-of-week/time windows with a design simplification made mid-implementation: rather than asking the user to configure a target temperature to bank to, the system reuses the home's own existing comfort-band edge and its already-learned thermal response rate — mirroring the project's existing overnight whole-house-fan "bank cool" pattern (`sleep_heat`/`sleep_cool`), generalized to any scheduled window instead of only the nightly one.

## Data Model

`Schedule` (frozen dataclass, `scheduler.py`):

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | uuid4 hex, assigned at creation by `config_flow.py` |
| `name` | `str` | user-facing label |
| `days` | `tuple[str, ...]` | 3-letter weekday abbreviations (`WEEKDAY_ABBREVS = ("mon","tue","wed","thu","fri","sat","sun")`), or `("all",)` (the `ALL_DAYS` sentinel — data-model/`scheduler.py` only as of the Issue #786 UI-polish follow-up; the config-flow day selector no longer offers it, since checking all 7 weekday boxes is functionally identical and the sentinel was redundant UI surface. `scheduler.py`'s `ALL_DAYS`/`_days_match()` handling and `config_flow.py`'s `_format_schedule_summary()` "All days" display branch are both kept for backward compat with already-saved `days=["all"]` schedules) |
| `start` | `str` | `"HH:MM"` or `"HH:MM:SS"` (HA's `TimeSelector` returns the latter) — local wall-clock, civil time |
| `end` | `str` | same shape as `start` |
| `cost_tag` | `str` | `COST_TAG_HIGH` (`"high"`) or `COST_TAG_LOW` (`"low"`) at the data-model level. As of the Issue #786 UI-polish follow-up, the config-flow edit form no longer exposes a cost_tag selector — every schedule created/edited via the UI is hardcoded to `COST_TAG_HIGH` (it's the only value with any live behavioral consumer: `resolve_tou_phase()` only ever checks for `cost_tag == COST_TAG_HIGH`). `scheduler.py` still parses any `cost_tag` value permissively, and `COST_TAG_LOW` is kept as dead-but-harmless for backward compat with any pre-existing "low" schedule |

No temperature field exists anywhere in this shape — this is deliberate, not an oversight (see [Origin](#origin)).

**Storage**: `config_entry.data["schedules"]`, a plain list of `Schedule`-shaped dicts. No new file — per `docs/HA-BOUNDARY-EXCEPTIONS.md`, the only approved non-config-entry data file is the learning engine's, and schedule data doesn't meet that bar. `CONFIG_METADATA` (`const.py`) is a flat per-field scalar dict with no list-of-records precedent, so `"schedules"` deliberately bypasses it rather than being forced into that shape.

**Cap**: `MAX_SCHEDULES = 5`, enforced by `config_flow.py` (the "+ Add" option is omitted from the scheduler list step once 5 exist, and `async_step_scheduler_edit()` defensively ignores an add attempted past the cap). `scheduler.py`'s resolver functions do **not** enforce this themselves — an arbitrary-length list is accepted, keeping the boundary a config-flow-layer concern only (see `tests/test_scheduler.py::TestFiveScheduleCapIsAConfigFlowConcern`).

## Pre-conditions

1. `now` passed to `is_schedule_active_at()`/`resolve_active_schedules()`/`resolve_tou_phase()` must be a `dt_util`-local-aware `datetime` — these functions do plain wall-clock arithmetic (`.hour`, `.minute`, `.weekday()`) and have no way to detect a naive or UTC-unconverted timestamp.
2. `resolve_tou_phase()` requires `current_indoor_temp` (from `coordinator._get_indoor_temp()`) and `hvac_mode` (`classification.hvac_mode`) — both `None`/`"off"` short-circuit to `TOUPhase.NONE` immediately.

## Post-conditions

1. `resolve_active_schedules()` always returns a `ScheduleResolution` — `cost_tag=None` (implicit "normal") when no schedule covers `now`; the schedule's own `cost_tag` (first-listed wins on overlap) otherwise.
2. `resolve_tou_phase()` always returns a `TOUPhaseResolution`. Its `target`/`mode`/`schedule_id`/`schedule_start`/`precondition_start` fields are populated whenever a qualifying upcoming `cost_tag="high"` schedule was found within the lookahead (`_LOOKAHEAD = 240 minutes`, matching the maximum possible lead time), **regardless of `phase`** — `phase` only answers "should automation act right now." A chart-rendering caller needs the full window shape for future timestamps that aren't "now," not just the current instant's act-or-not answer (see `tests/test_scheduler.py::test_window_fields_populated_even_when_phase_is_none`).
3. When `apply_tou_precondition()` runs, `_override_confirm_pending` is `False`, and `decide_scheduled_band_gate()` returns `PROCEED` (see [Status Card](#status-card) for the full gate), the thermostat is commanded to `target` in `mode` (via `_set_temperature_for_mode(classification, target_override=target)`) and a `tou_precondition_applied` event fires (deduped on `(schedule_id, mode, round(target, 2))`, same shape as `comfort_band_applied`'s dedup). When the gate instead returns `DEFER_OCCUPANCY`, `_set_temperature_for_mode()`'s own Issue #85 safety net redirects to setback (`handle_occupancy_away()`/`handle_occupancy_vacation()`) — `target` is **not** commanded in that case, and the event is suppressed (see the Occupancy row in [Error Conditions](#error-conditions)).

## Invariants

1. **No second band-computation engine.** `_compute_target_band_schedule()` remains the single source of truth for the chart Target Band; TOU pre-conditioning's chart representation is an additive override parameter (`tou_precondition_window`) on that same function, applied after whichever branch computed the base band — never a parallel implementation.
2. **The Issue #85 occupancy safety net always applies.** `apply_tou_precondition()` routes exclusively through `_set_temperature_for_mode()`, never `_set_temperature()` directly — away/vacation redirects to setback regardless of an active pre-conditioning window, and the `tou_precondition_applied` event is suppressed in that case so it never misrepresents what was actually commanded.
3. **TOU and weather-trend pre-cool stay architecturally separate.** `resolve_tou_phase()`/`apply_tou_precondition()` never call or are called by `resolve_pre_cool_modifier()`/`compute_pre_cool_target()`/`_schedule_pre_condition()` — two independently-gated reasons to pre-cool/pre-heat, not one mechanism serving two purposes.
4. **The lead-time formula has exactly one implementation.** `thermal_lead_time.compute_lead_minutes_from_rate()` is the sole implementation of `delta_t / rate * 60 * safety, clamped, with a fallback` — used by this feature, `automation.py::_schedule_pre_condition()`, `ode_ceiling_guard.py`, and `briefing.py::_derive_warm_day_events()`. Each caller keeps its own bounds/safety-multiplier/fallback constants (these genuinely differ per use); only the shape is shared.
5. **A not-yet-confirmed override is checked separately from `decide_scheduled_band_gate()`, before it.** The gate's `manual_override_active` input is `_manual_override_active`, which only becomes `True` once CA's ~10-minute confirm window (`DEFAULT_OVERRIDE_CONFIRM_SECONDS`) elapses; during that window `_override_confirm_pending=True` while `_manual_override_active` is still `False`, so the gate alone cannot see it. `apply_tou_precondition()` checks `_override_confirm_pending` explicitly, first, before calling the gate at all — mirroring `apply_classification()`'s own separate check at the same two flags (`automation.py:2423`/`2458`), since TOU pre-conditioning runs every 30-minute cycle (unlike `handle_bedtime()`/`handle_morning_wakeup()`/`handle_pre_cool()`, which fire once daily at a fixed time and don't carry this same-cycle race window in practice).
6. **The normal comfort-band setpoint sanity check is exempted for exactly one call site, not weakened generally.** `_set_temperature()`'s `SETPOINT INCONSISTENCY` check (cool-mode target below `comfort_heat`, or heat-mode target above `comfort_cool` — a real bug signal for ordinary comfort-band writes) would otherwise false-positive on TOU's own intentional design: banking a cool-mode target down to a sleep-window `sleep_heat` value below `comfort_heat` is not a bug, it's the feature working as designed. `_set_temperature_for_mode()`/`_set_temperature()` gained a `skip_setpoint_sanity_check: bool = False` parameter; only `apply_tou_precondition()`'s call passes `True`. Every other existing caller (`_apply_comfort_band()` and all its other call sites) keeps the check fully active — this is a single-call-site exemption, not a relaxed bound (see `tests/test_tou_precondition.py::test_normal_comfort_band_write_below_comfort_heat_still_flags_incident` for the non-regression proof).

## Midnight & Day-of-Week Resolution

`_compute_target_band_schedule()` (`coordinator.py`) already solves midnight wraparound for a single wake/sleep pair via `h_n = h + 24 if (night_owl and h < wake_h) else h`. This idiom alone is **insufficient** once day-of-week is added: shifting the hour by +24 silently attributes the post-midnight portion of a window to *whatever calendar day `now` actually is* — wrong for a schedule like "Friday 11pm–1am" evaluated at 12:30am Saturday, where the post-midnight portion is still Friday's schedule.

`is_schedule_active_at()` (`scheduler.py:139`) instead tests two explicit calendar days when `end_h <= start_h` (spans midnight):

```
today = weekday(now)
yesterday = weekday(now - 1 day)
active = (today in schedule.days and start_h <= h < 24)
      or (yesterday in schedule.days and 0 <= h < end_h)
```

All hour comparisons use local wall-clock `now.hour + now.minute/60.0` — civil time, matching `_compute_target_band_schedule()`'s own convention, not the UTC-delta elapsed-duration convention `_interpolate_hourly_outdoor_temp()` uses for a different class of problem (schedule boundaries are civil-time definitions, not elapsed durations).

`_parse_hhmm()` accepts both `"HH:MM"` and `"HH:MM:SS"` (HA's `TimeSelector` returns the latter) — any seconds component is ignored.

## Banking Target Resolution

Direction follows the day's own anticipated HVAC need — mirroring the original request (pre-*cool* before a high-cost window on a cooling day, pre-*heat* before one on a heating day), not an arbitrary/opposite choice:

| `hvac_mode` | Banks toward | Resolved via | Driven by rate |
|---|---|---|---|
| `"cool"` | comfort-band **floor** | `resolve_comfort_heat(comfort_heat_raw, sleep_heat, in_sleep_window)` (`nat_vent_gate.py`) | `thermal_model["k_active_cool"]` |
| `"heat"` | comfort-band **ceiling** | `resolve_comfort_cool(comfort_cool_raw, sleep_cool, in_sleep_window)` (`nat_vent_gate.py`, added for this feature — symmetric counterpart to the pre-existing `resolve_comfort_heat()`) | `thermal_model["k_active_heat"]` |
| anything else (`"off"`, etc.) | — | `TOUPhase.NONE`, no direction to bank | — |

`in_sleep_window` is evaluated at `schedule_start` (the schedule's own start instant, via `automation.py::_in_sleep_window()`), not at "now" — the banking target itself never depends on the current time-of-day, only on whether the *destination* window is inside the sleep schedule. **This means the target is never a new number** — it is always exactly whichever value `resolve_comfort_heat()`/`resolve_comfort_cool()` would already return for that moment; a schedule confirmed with David: "match sleep_heat/sleep_cool when overlapping the sleep window" rather than always using the plain comfort value.

## Coast Phase — Confirmed, Not Assumed

Before any pre-conditioning "stop" logic was written, a prerequisite test (`tests/test_tou_precondition.py::TestApplyComfortBandEdgeOnlyIntervention`) confirmed directly against production code: `_apply_comfort_band()` issues **one** `set_temperature` service call per cycle, with the day's active edge value — identical regardless of whether indoor sits at that edge already or far from it (banked at the opposite edge). This is a **threshold** command, not a proactive drive-to-target: a real thermostat in `cool` mode with `temperature=76°F` stays off while indoor is below 76°F; it does not actively heat back up to reach it.

Consequence: once a schedule's own `start` time arrives, `resolve_tou_phase()` reports `TOUPhase.NONE` and `apply_tou_precondition()` simply stops being called. The very next classification cycle's normal `apply_classification()` → `_apply_comfort_band()` call re-arms exactly the day's plain edge (e.g. `comfort_cool` on a cooling day) — which, given the banked starting position, is naturally idle until indoor actually reaches it. **No dedicated "coast" state, suppression guard, or new code exists anywhere in this feature for this behavior** — it falls out of the pre-existing threshold-command semantics for free. This is directly confirmed end-to-end by the golden scenario (see [Golden Scenario Coverage](#golden-scenario-coverage)).

"Comfort always wins" (confirmed decision, not a design choice requiring code): if the banked thermal mass runs out before the scheduled window ends, `_apply_comfort_band()`'s normal edge-threshold command is exactly what corrects it — the same mechanism that corrects a comfort breach on any other day. No new escalation path exists.

## Lead-Time Computation

`thermal_lead_time.compute_lead_minutes_from_rate(delta_t, rate, *, min_minutes, max_minutes, safety_multiplier, fallback_minutes)` (`thermal_lead_time.py:21`) — the one shared implementation of a formula independently duplicated 3 times before this feature existed:

| Caller | `min`/`max` | `safety_multiplier` | `fallback_minutes` |
|---|---|---|---|
| `scheduler.py::resolve_tou_phase()` | 30 / 240 | 1.3 | 120 (`_TOU_LEAD_MIN_FALLBACK`) |
| `automation.py::_schedule_pre_condition()` (adaptive pre-heat) | config `CONF_MIN_PREHEAT_MINUTES`/`CONF_MAX_PREHEAT_MINUTES` | config `CONF_PREHEAT_SAFETY_MARGIN` | clamped `default_preheat_minutes` |
| `ode_ceiling_guard.py` (ceiling-guard escalation) | `_LEAD_MIN_FLOOR`/`_LEAD_MIN_CEIL` (30/240) | `_LEAD_MIN_LOOKAHEAD_MULTIPLIER` (1.3) | `_CEILING_PRECOOL_FALLBACK_MIN` (120) |
| `briefing.py::_derive_warm_day_events()` (warm-day pre-cool lead) | 30 / 240 | 1.3 | `const.CEILING_PRECOOL_FALLBACK_MIN` (120) — previously a locally-redefined duplicate constant, now imported directly |

`resolve_tou_phase()`'s own lookahead window (`_LOOKAHEAD = timedelta(minutes=240)`, matching `_TOU_LEAD_MIN_CEIL`) bounds how far ahead `_next_start_within()` searches for a qualifying schedule — a schedule starting further than 4 hours away is not yet "found" (and its window fields stay unpopulated) until a later 30-minute cycle brings it within range. This is intentional, not a gap: the longest possible computed lead time is 240 minutes, so nothing further out could ever need pre-conditioning yet.

## Chart Coverage

`_compute_target_band_schedule()` gained one new optional parameter: `tou_precondition_window: tuple[datetime, datetime, float, str] | None` — `(window_start, window_end, target, mode)`. Applied **after** whichever of the 5 existing branches computed the base `lower`/`upper` for a timestamp:

```python
if tou_precondition_window is not None:
    window_start, window_end, target, mode = tou_precondition_window
    if window_start <= ts < window_end:
        if mode == "cool":
            lower = target
        elif mode == "heat":
            upper = target
```

`coordinator._tou_precondition_window_tuple()` builds this tuple from the cached `self._tou_phase_resolution` (`(precondition_start, schedule_start, target, mode)`), returning `None` when no upcoming `high` schedule was resolved. Both call sites that build the chart Target Band and the ODE prediction curve (`get_chart_data()`'s own `_compute_target_band_schedule()` call, and `_build_predicted_indoor_future()`'s internal one when it isn't handed a pre-computed `band_schedule`) always receive the same tuple, so they can never disagree — the mandatory Chart Coverage rule (`CLAUDE.md`).

**Resolution timing**: `coordinator._resolve_tou_schedule_state()` (pure, no HVAC writes) runs *before* the ODE-prediction executor call in `_async_update_data_impl()` (and again before the equivalent call in the briefing-generation path), so both consumers see the current cycle's fresh resolution rather than the previous cycle's stale one. Acting on a `PRECONDITIONING` result (`coordinator._apply_tou_schedule()`, which calls `automation_engine.apply_tou_precondition()`) happens later in the same cycle, after `apply_classification()` — so the pre-conditioning setpoint is the final word for the thermostat that cycle, not the day's normal comfort-band edge.

### Unified "Target" chart line supersedes a separate TOU-window annotation

An earlier design direction for surfacing TOU on the chart considered a dashed vertical-line
annotation marking the schedule's start/end (reusing the existing `caAnnotations` plugin).
That direction was superseded: instead, one bold, solid "Target" line renders the actual
system target at any point in time regardless of source — plain comfort-band operation, TOU
banking, or nat-vent thermostatic cycling — and the level shift itself is the visual signal
that TOU pre-conditioning (or nat-vent cycling) is active. No separate annotation is needed;
see the "Unified Target Line" section below for the full three-tier derivation.

## Unified Target Line (Issue #786 follow-up, Phase 3)

The dashboard chart's Target line answers "what is the system actually targeting right now"
— distinct from the shaded Target Band (the comfort *range*), this is the single value the
thermostat is being driven toward. It has a historical (past) half and a forward (future)
half, both built entirely from data already resolved elsewhere in this cycle — no new
resolution logic anywhere in this chain.

### Historical half — `coordinator._extract_historical_effective_target()`

Per persisted `chart_log` cycle, in priority order:

1. The real `setpoint` field (compressor-commanded — read from the live thermostat's
   `target_temperature` attribute at write time, genuinely source-agnostic since it doesn't
   care *why* the thermostat is set there).
2. Else `nat_vent_target` (Phase 3a) when `nat_vent_active` was true that cycle — the real
   thermostatic value the WHF fan was cycling around
   (`nat_vent_cycling.compute_nat_vent_target()`, see below), not a band-edge approximation.
3. Else `None` — genuinely undefined (thermostat off, no nat-vent).

All 4 `chart_log.append()` call sites (the 30-min poll plus the 3 event-driven sites —
`classification_change`/`override`/`hvac_action_change`) now populate both `setpoint` and
`nat_vent_target` every cycle (`coordinator._read_chart_setpoint()` /
`coordinator._nat_vent_target_now()`), closing the gap where the 3 event-driven sites
previously always wrote `setpoint=None`.

### Forward half — `coordinator._compute_effective_target_forward()`

Per future forecast-hour timestamp, in priority order:

1. **TOU banking target** — while the timestamp falls inside the resolved
   `[precondition_start, schedule_start)` window (`coordinator._tou_precondition_window_tuple()`,
   the same tuple the Target Band's own TOU override branch above already consumes).
2. **Nat-vent thermostatic cycling target** — while `get_chart_data()`'s pre-existing
   `predicted_activity[].fan_active` proxy says nat-vent is predicted active for that
   timestamp. Fed through the same shared `nat_vent_cycling.compute_nat_vent_target()`
   helper as the historical half and the live decision path, using *that timestamp's own*
   `target_band.lower`/`.upper` (already sleep/wake/TOU-ramp-aware) as the day/sleep
   floor-and-ceiling inputs. `fan_active` is a known, pre-existing, already-accepted
   approximation (`outdoor < indoor and outdoor < band_upper + delta and indoor >
   band_upper` — not a call into the real `decide_nat_vent_gate()`/FSM); this derivation
   inherits that approximation rather than correcting it, and degrades gracefully to tier 3
   (never crashes, never fabricates an out-of-band value) whenever the proxy's inputs are
   incomplete.
3. **Plain active comfort-band edge** — `target_band.lower` for `hvac_mode == "heat"`,
   `.upper` for `"cool"`, `None` otherwise. Same derivation `_derive_predicted_setpoint()`
   used before this fix; now only the fallback tier instead of the whole answer, closing
   that function's TOU-blindness and nat-vent-blindness (both confirmed gaps — the old
   field was never rendered by the frontend, so neither gap was previously user-visible).

### `nat_vent_target` DRY consolidation

`(comfort_heat + comfort_cool) / 2` by day, `sleep_heat + hysteresis` overnight — previously
duplicated independently in `automation.py`'s live `nat_vent_temperature_check()` decision
path, `nat_vent_cycling.decide_nat_vent_cycling()`'s pure reimplementation, and (found during
this consolidation, not previously tracked) `coordinator.compute_nat_vent_cycling_band()`'s
dashboard-status helper. Consolidated into one shared
`nat_vent_cycling.compute_nat_vent_target()`, mirroring how Issue #786 consolidated the
3x-duplicated lead-time formula into `thermal_lead_time.py`. All three pre-existing call
sites, plus `coordinator._nat_vent_target_now()` (the chart line's own per-cycle read), now
call the one shared function.

### Frontend

`frontend/index.html` renders one dataset, `'Target'` — `borderWidth: 4`, solid (not
dashed), amber/gold (`CHART_COLORS.target`), distinctly bolder than every other line on the
chart. Built via the same historical/future merge pattern already established for
`mergedPredIndoor`: `chartData.effective_target_history` (past, `x <= now`) concatenated with
`chartData.effective_target_forecast` (future, `x > now`), sorted by timestamp. This retires
the old thin `defense_lines`-based "Heat Setpoint"/"Cool Setpoint" overlay — a second,
always-both-band-edges rendering of the same shaded Target Band with no independent
information — which the code's own prior comment already flagged as superseded
("kept for backward-compat (no longer drives display)"). The backend `defense_lines` field
and `_compute_defense_lines()` function are unchanged (other tests pin their contract); only
the frontend's use of them for rendering was removed. The `overlay-setpoint` checkbox
(relabeled "Target") now toggles the new line instead.

## Status Card

`_compute_automation_status()` gained a branch (positioned after the grace-active check, before the occupancy-mode fallbacks — a schedule is a mechanism *reason*, the one card this belongs on per the Status Card Ontology): when `self._tou_phase_resolution.phase == TOUPhase.PRECONDITIONING`, returns `"pre-cooling — TOU high-cost period (ends H:MM AM)"` or `"pre-heating — ..."`, using the same compact "short label — duration (ends HH:MM)" convention `_format_grace_remaining()` established for the Fan (WHF) card. A higher-priority mechanism reason (grace period, door/window pause, nat-vent) still wins if simultaneously true — the Status card shows only one reason at a time.

This is display precedence, not behavioral precedence, and the two are governed by separate code paths: `_compute_automation_status()`'s branch order decides only what text appears on the Status card; it never touches the thermostat. Before Fix 1 (Issue #786), that made this section misleading by omission — its wording read as if the Status card's reason-ordering also decided which mechanism actually controlled the HVAC command, but `apply_tou_precondition()` at the time only checked door/window state and could still write a TOU setpoint underneath an active manual override or nat-vent session, silently contradicting whatever the card displayed.

As of Fix 1, `apply_tou_precondition()` calls the same shared `decide_scheduled_band_gate()` used by `handle_bedtime()`/`handle_morning_wakeup()`/`handle_pre_cool()`, and defers (issues no HVAC command at all) when the gate returns `DEFER_OVERRIDE`, `DEFER_PAUSED`, or `DEFER_NAT_VENT` — see [Error Conditions](#error-conditions). So the override/paused/nat-vent precedence described above now holds at both layers: what the Status card *displays* and what the thermostat actually *does* agree for these three cases. This isn't by construction or coincidence — `_compute_automation_status()` and `apply_tou_precondition()` are two independent call sites that each consult the same underlying engine state (`_manual_override_active`, `_paused_by_door`, `_natural_vent_active`/`_whf_owns_hvac()`); they simply now agree because both read the same ground truth rather than one of them approximating it. `tools/simulations/pending/issue_786_tou_precondition_defers_to_manual_override.json` is the golden-track scenario proving the behavioral (write-suppression) side of this — it asserts zero `tou_precondition_applied` events and that the thermostat holds the user's override rather than the banking target while a manual override is active.

No new API field was needed: `ATTR_AUTOMATION_STATUS` already flows `_compute_automation_status()`'s return value into `coordinator.data` and from there into `api.py`'s status response — modifying the shared function was sufficient.

## Error Conditions

| Condition | Handling |
|---|---|
| `days=[]` submitted in the config-flow edit form | Form-level error (`errors["days"] = "schedule_days_required"`), not persisted — the resolver itself also never treats an empty list as a wildcard (`is_schedule_active_at()` returns `False` unconditionally for `days=()`) |
| A schedule window falls entirely inside a DST-skipped local hour | `is_schedule_active_at()`/`resolve_tou_phase()` do plain wall-clock arithmetic on whatever `datetime` they're given — they cannot detect a "nonexistent" local instant. Avoiding construction of one in the first place is `dt_util`'s responsibility upstream; these functions are only guaranteed not to raise (see `tests/test_scheduler.py::test_dst_spring_forward_nonexistent_hour_does_not_crash`) |
| A confirmed manual override, a paused door/window, or an active nat-vent/WHF session during a pre-conditioning window | `apply_tou_precondition()` skips (no HVAC command, no `tou_precondition_applied` event, INFO log) — as of Issue #786 Fix 1, this routes through the same shared `decide_scheduled_band_gate()` call `handle_bedtime()`/`handle_morning_wakeup()`/`handle_pre_cool()` already use, deferring on `DEFER_OVERRIDE`/`DEFER_PAUSED`/`DEFER_NAT_VENT`. The door/window case still deliberately does **not** trigger a second, duplicate pause-for-door-window notification flow; `apply_classification()`'s own `_apply_comfort_band()` call earlier in the same cycle already owns that. Previously (pre-Fix 1) this row covered only the door/window case via a bespoke check that never looked at override or nat-vent state — see [Status Card](#status-card) for the full before/after |
| An override is detected but not yet confirmed (`_override_confirm_pending=True`, within CA's ~10-minute confirm window) during a pre-conditioning window | `apply_tou_precondition()` skips (no HVAC command, no event, INFO log) — checked separately, before `decide_scheduled_band_gate()`, since `_manual_override_active` is still `False` during this window and the gate alone cannot see it. See [Invariants](#invariants) item 5. Empirically confirmed: `tests/test_tou_precondition.py::test_skips_when_override_confirm_pending` |
| Occupancy is away/vacation during a pre-conditioning window | `_set_temperature_for_mode()`'s Issue #85 safety net redirects to setback; `apply_tou_precondition()` suppresses the `tou_precondition_applied` event in this case (it would otherwise misrepresent what was actually commanded) |
| A 6th schedule reaches `resolve_active_schedules()`/`resolve_tou_phase()` (should be unreachable — config-flow enforces the cap) | The resolver functions accept an arbitrary-length list without truncating — the cap is exclusively a config-flow concern, kept out of the resolver by design |

## Golden Scenario Coverage

`tools/simulations/pending/issue_786_tou_precondition_banks_then_coasts.json` (not yet promoted to `golden/` — awaiting David's review per the project's Golden Simulation Test Policy) exercises the full stack via `use_coordinator: true`: a `cost_tag="high"` schedule 16:15–21:00, asserting (a) the plain comfort band holds before the lead window opens, (b) `tou_precondition_applied` fires with the correct target/mode once the fallback 120-minute lead window opens, and (c) the plain comfort band is re-armed the instant the schedule's own start time is reached — direct end-to-end proof of the [Coast Phase](#coast-phase--confirmed-not-assumed) finding. Verified to actually fail (not vacuously pass) when the feature's action call site is disabled, per the project's Three-Exercise Protocol.

Required a small, purely-additive harness change (`tools/sim_harness/run_production.py`'s `"classification"` event handler now also calls `coordinator._resolve_tou_schedule_state()`/`_apply_tou_schedule()` when a real coordinator is present — mirroring the exact production sequence) and two new `outcomes.py` additions (a `tou_precondition_applied` → `None` mapping, handled only via a new `check_assertion` custom type, exactly the precedent `comfort_band_applied` already established — see `outcomes.py` for the full §8 justification).

## Code Reference

- [`Schedule`, `resolve_active_schedules()`, `resolve_tou_phase()`, `is_schedule_active_at()`](../custom_components/climate_advisor/scheduler.py) — the whole module
- [`compute_lead_minutes_from_rate()`](../custom_components/climate_advisor/thermal_lead_time.py#L21)
- [`resolve_comfort_cool()`](../custom_components/climate_advisor/nat_vent_gate.py) — cool-side counterpart to `resolve_comfort_heat()`
- [`_resolve_tou_schedule_state()`, `_apply_tou_schedule()`, `_tou_precondition_window_tuple()`](../custom_components/climate_advisor/coordinator.py#L3527)
- [`apply_tou_precondition()`](../custom_components/climate_advisor/automation.py#L3137)
- [`async_step_scheduler()`, `async_step_scheduler_edit()`](../custom_components/climate_advisor/config_flow.py#L1154)
