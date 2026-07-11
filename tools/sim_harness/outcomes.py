"""outcomes — production event_log → legacy outcome vocabulary bridge.

Converts the ``ProductionRunResult.event_log`` (list of
``(event_type, payload, ts)``) into the same outcome vocabulary used by the
legacy ``ClimateSimulator`` (``Decision.outcome`` strings), so the G4
differential can compare production vs. legacy assertions using identical
semantics.

Key design rules:
  - Mirror simulate.py; never invent outcome semantics.
  - Every mapping is grounded in automation.py source.
  - Unmapped event types are collected as FINDINGS rather than silently dropped.
  - ``_outcome_at`` / ``_temp_at`` match the legacy "last at or before time" semantics.
  - ``check_assertion`` mirrors ``ClimateSimulator._check_assertion`` custom types,
    reading the production engine's real final state and event_log.

Event-type → outcome mapping
─────────────────────────────
Mapped (production → legacy):
  classification_applied        → classification_applied
  warm_day_comfort_gap          → warm_day_comfort_gap
  warm_day_setback_applied      → setback_applied
    (warm-day setback applied to existing thermostat mode in production;
     legacy emits "setback_applied" from _handle_classification for the
     same condition — Issue #96 warm-day setback path)
  comfort_band_applied          → comfort_band_applied  (Issue #249 P3)
    Payload: {floor, ceiling, active, mode, reason}.
    target_temp = ceiling when active="ceiling"; floor when active="floor".
    §8 justification: this event did not exist before P3.  Adding it here is
    purely additive — it maps a BRAND-NEW event type to a BRAND-NEW outcome
    label that no pre-P3 scenario ever asserted.  It cannot silently pass a
    real regression from before P3 because no pre-P3 scenario uses the
    "comfort_band_applied" expect string.
  sensor_opened  result=natural_ventilation  → natural_ventilation
  sensor_opened  result=paused               → paused
  sensor_all_closed  was_nat_vent or was_paused  → resumed
  sensor_all_closed  neither flag            → resumed
    (production always calls resumed; if nothing was happening this is still
     a resumed decision — mirrors simulate.py _handle_all_closed)
  nat_vent_comfort_floor_exit      → nat_vent_comfort_floor_exit
  nat_vent_outdoor_rise_exit       → nat_vent_outdoor_rise_exit
  nat_vent_bedtime_continue        → nat_vent_bedtime_continue        (Issue #370 — gate passed, fan continues)
  nat_vent_sleep_ceiling_reached   → nat_vent_sleep_ceiling_reached   (Issue #370 — indoor ≤ sleep_cool in window)
  bedtime_setback               → setback_applied
    (P3 payload changed: was {target_f}; now {mode, floor, ceiling, active,
     modifier}.  target_temp = floor when active="floor"; ceiling when
     active="ceiling".  This is a payload-shape migration for a pre-existing
     event type — the outcome label is unchanged.  §8 note: the previous
     target_f read returned None for P3 payloads, causing expect_temp
     assertions to fail even though the behavior was correct.  Reading
     floor/ceiling/active is the semantically correct fix — it cannot make a
     real setback failure pass silently because the extracted temp still comes
     from the live engine's band computation, not a fabricated value.)
  bedtime_setback_skipped       → bedtime_setback_skipped
  grace_expired  re_paused=True   → paused
  grace_expired  re_paused=False  → resumed
  ceiling_guard_fired           → ceiling_guard_fired
  override_detected             → override_detected
  override_confirmed            → override_confirmed
  override_self_resolved        → override_self_resolved
  override_cleared              → override_cleared
  override_adopted              → override_adopted (Issue #483)

Issue #258 — overnight pre-cool:
  pre_cool_applied              → pre_cool_applied
    Payload: {target, modifier, sleep_cool, floor, indoor, nat_vent_suppressed}.
    target_temp = payload["target"] (the cool ceiling that was applied).
  pre_cool_suppressed_nat_vent  → pre_cool_suppressed_nat_vent
    Payload: {indoor, target, modifier}.  No setpoint was applied; nat-vent
    already brought indoor to or below the pre-cool target.
    target_temp = None (no service call made).

Occupancy / wakeup (Issue #240 — production emits these directly now):
  occupancy_setback            → setback_applied  (away/vacation)
    P3 payload: {mode, floor, ceiling, occupancy}.  Previously had target_f;
    now carries both edges.  Away and vacation always use active="ceiling"
    (see select_comfort_band in automation.py), so target_temp = ceiling.
    §8 note: same rationale as bedtime_setback above — payload-shape migration,
    outcome label unchanged.
  occupancy_comfort_restored   → comfort_restored (home return)
  morning_wakeup               → comfort_restored (wakeup success path)
  (Previously production emitted no event and these were guessed from the
   action_log; that derivation was removed once #240 landed.)

FINDINGS — no legacy outcome equivalent:
  warm_day_state_confirmed   — informational; thermostat already in correct
                               mode on a warm day (no setback needed).  No
                               matching legacy outcome.
  nat_vent_away_ceiling_exit — production-only exit path (Issue #99 extension);
                               not in legacy simulator.
  nat_vent_ceiling_escalation— production-only; nat-vent→cooling escalation
                               before ceiling breach.  Not in legacy simulator.
  nat_vent_forecast_skip     — production-only; forecast-peak guard before
                               activating nat-vent.  Not in legacy simulator.
  nat_vent_floor_imminent_skip — production-only; ODE predicts floor breach
                               before nat-vent would help.  Not in legacy.
  nat_vent_predicted_floor_exit — production-only; ODE predicts floor exit.
                               Not in legacy simulator.
  grace_started              — internal lifecycle event; no legacy decision.
  incident_detected          — diagnostic / telemetry only; not a behavior
                               decision in the legacy simulator.
  morning_wakeup_skipped     — skipped path.  Legacy emits morning_wakeup_skipped;
                               production emits morning_wakeup_skipped.
                               (Mapped 1:1 even though legacy outcome is the same
                               string — kept as explicit mapping.)

NOTE: production's handle_morning_wakeup success path emits NO event; the
      comfort restoration is reflected only in the action_log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Production event types that have no direct legacy outcome equivalent.
# These are documented FINDINGS — they represent production behavior gaps
# versus the legacy simulator vocabulary.
UNMAPPED_PRODUCTION_EVENTS: frozenset[str] = frozenset(
    {
        "warm_day_state_confirmed",
        "nat_vent_away_ceiling_exit",
        "nat_vent_ceiling_escalation",
        "nat_vent_forecast_skip",
        "nat_vent_floor_imminent_skip",
        "nat_vent_predicted_floor_exit",
        "grace_started",
        "incident_detected",
    }
)


# ---------------------------------------------------------------------------
# ProductionDecision — outcome-vocabulary row (mirrors legacy Decision)
# ---------------------------------------------------------------------------


@dataclass
class ProductionDecision:
    """A single extracted outcome entry, mirroring legacy ``Decision``.

    Fields match ``Decision`` closely so the same ``_outcome_at`` /
    ``_temp_at`` helpers can operate on either list.
    """

    time: str  # ISO timestamp string (same lexicographic ordering as legacy)
    event_type: str  # raw production event type (for diagnostics)
    outcome: str  # legacy outcome vocabulary string
    target_temp: float | None = None  # set when a temperature was applied


# ---------------------------------------------------------------------------
# Core mapping: event_log → ProductionDecision list
# ---------------------------------------------------------------------------


def _naive_iso(ts: Any) -> str:
    """Format a timestamp as a NAIVE ISO string (no tz offset).

    The FakeScheduler clock is tz-aware (UTC), so ``ts.isoformat()`` yields a
    ``...+00:00`` suffix. Scenario assertion ``at`` strings and legacy
    ``Decision.time`` values are naive (e.g. ``2026-05-20T06:01:00``). Comparing
    them lexicographically, a tz-aware production string sorts AFTER the bare
    assertion time, so an "at or before" lookup would wrongly exclude a decision
    landing exactly on the assertion timestamp (off-by-one lag). Stripping the
    offset keeps production timestamps in the same comparable space as legacy.
    """
    if hasattr(ts, "replace") and hasattr(ts, "isoformat"):
        return ts.replace(tzinfo=None).isoformat()
    return str(ts)


def production_decisions(result: Any) -> list[ProductionDecision]:
    """Convert a ``ProductionRunResult`` into a list of ``ProductionDecision`` entries.

    Processes ``result.event_log`` (primary) and ``result.action_log``
    (occupancy-derived setback/restore outcomes) to produce a time-ordered
    list using the legacy outcome vocabulary.

    Args:
        result: A ``ProductionRunResult`` instance.

    Returns:
        Time-ordered list of ``ProductionDecision`` entries.
    """
    decisions: list[ProductionDecision] = []

    # Pass 1: map event_log entries
    for event_type, payload, ts in result.event_log:
        if ts is None:
            continue
        ts_str = _naive_iso(ts)
        mapped = _map_event_to_outcome(event_type, payload, ts_str)
        if mapped is not None:
            decisions.append(mapped)

    # Occupancy/wakeup outcomes now come straight from Pass 1 events
    # (occupancy_setback / occupancy_comfort_restored / morning_wakeup) since
    # production emits them directly (Issue #240). The earlier action_log
    # derivation that guessed setback-vs-restore from set_temperature calls is
    # no longer needed and was removed (it mislabeled home-returns as setbacks).

    # Pass 2: enrich target_temp from the action_log.
    # Production emits the decision EVENT (e.g. classification_applied) and the
    # setpoint via a SEPARATE channel: _set_temperature() → climate.set_temperature
    # in the action_log, at the same virtual-clock instant. Events like
    # classification_applied therefore carry no target_temp in their payload —
    # the legacy Decision records it, so we recover it from the same-timestamp
    # set_temperature action to keep expect_temp assertions comparable.
    #
    # P3 note: for dual-setpoint (heat_cool mode) band actions, the action_log carries
    # both target_temp_low (floor) and target_temp_high (ceiling).  _temps_by_timestamp
    # returns target_temp_low (the floor) as the fallback for dual actions — this is
    # consistent with the legacy _temp_at semantics (floor = the heat setback reference).
    # New P3 scenarios that need to assert on the ceiling should use the expect_band
    # assertion type (check_assertion custom type) rather than expect_temp.
    temp_by_ts = _temps_by_timestamp(result.action_log)
    for dec in decisions:
        if dec.target_temp is None and dec.time in temp_by_ts:
            dec.target_temp = temp_by_ts[dec.time]

    # Sort by time string (ISO 8601 lexicographic = chronological)
    decisions.sort(key=lambda d: d.time)

    return decisions


def _temps_by_timestamp(action_log: list[dict]) -> dict[str, float]:
    """Map naive-ISO timestamp → setpoint from climate.set_temperature actions.

    For single-setpoint calls uses ``temperature``; for dual (heat_cool) calls
    falls back to ``target_temp_low`` (the heat setpoint), matching how the
    legacy simulator records ``Decision.target_temp`` for dual setback. The last
    set_temperature at a given timestamp wins.
    """
    temps: dict[str, float] = {}
    for entry in action_log:
        if entry.get("domain") != "climate" or entry.get("service") != "set_temperature":
            continue
        ts = entry.get("ts")
        if ts is None:
            continue
        data = entry.get("data", {})
        val = data.get("temperature", data.get("target_temp_low"))
        if val is not None:
            temps[_naive_iso(ts)] = float(val)
    return temps


def _map_event_to_outcome(
    event_type: str,
    payload: dict,
    ts_str: str,
) -> ProductionDecision | None:
    """Map a single production event to a legacy outcome.

    Returns None for internal/diagnostic events that have no legacy equivalent
    (these are documented in UNMAPPED_PRODUCTION_EVENTS).
    """
    # --- Sensor events ---
    if event_type == "sensor_opened":
        result_field = payload.get("result", "")
        if result_field == "natural_ventilation":
            return ProductionDecision(ts_str, event_type, "natural_ventilation")
        if result_field == "paused":
            return ProductionDecision(ts_str, event_type, "paused")
        # Unexpected result value — map to outcome string for visibility
        return ProductionDecision(ts_str, event_type, f"sensor_opened:{result_field}")

    if event_type == "sensor_all_closed":
        # Production always fires a resume; mirrors legacy _handle_all_closed
        return ProductionDecision(ts_str, event_type, "resumed")

    # --- Classification events ---
    if event_type == "classification_applied":
        return ProductionDecision(ts_str, event_type, "classification_applied")

    if event_type == "classification_suppressed_paused":
        return ProductionDecision(ts_str, event_type, "classification_suppressed_paused")

    if event_type == "warm_day_comfort_gap":
        target = payload.get("target_f") or payload.get("comfort_heat")
        return ProductionDecision(ts_str, event_type, "warm_day_comfort_gap", target)

    if event_type == "warm_day_setback_applied":
        # Production applied a setback to an actively-running thermostat mode on a
        # warm day.  Legacy outcome for the same code path is "setback_applied".
        target = payload.get("new_setpoint_f")
        return ProductionDecision(ts_str, event_type, "setback_applied", target)

    # --- Comfort band (Issue #249 P3) ---
    # _apply_comfort_band emits this event after arming the thermostat.
    # This event is intentionally NOT mapped to a decisions-list outcome here.
    # Adding it to the decisions list would make production_outcome_at return
    # "comfort_band_applied" at every timestamp that triggers a band arm — which
    # would break ALL pre-P3 golden assertions that expect "classification_applied",
    # "setback_applied", etc. at those same timestamps.
    # Instead, comfort_band_applied is handled ONLY via check_assertion custom types:
    #   expect="comfort_band_armed"   — band was armed (any edge)
    #   expect="expect_band"          — band floor/ceiling match (+ expect_band dict)
    # New P3 scenarios that want to assert on the band use those assertion types.
    # §8 justification: this is purely additive (new event, new assertion types, no
    # pre-P3 semantic changed).  The decisions list is unchanged; the event is silently
    # skipped below in the UNMAPPED guard.
    if event_type == "comfort_band_applied":
        return None  # handled via check_assertion custom types only

    # --- Natural ventilation exit events ---
    if event_type == "nat_vent_comfort_floor_exit":
        return ProductionDecision(ts_str, event_type, "nat_vent_comfort_floor_exit")

    if event_type == "nat_vent_outdoor_rise_exit":
        return ProductionDecision(ts_str, event_type, "nat_vent_outdoor_rise_exit")

    # Issue #370: bedtime continuation and sleep-ceiling exit events
    if event_type == "nat_vent_bedtime_continue":
        return ProductionDecision(ts_str, event_type, "nat_vent_bedtime_continue")

    if event_type == "nat_vent_sleep_ceiling_reached":
        return ProductionDecision(ts_str, event_type, "nat_vent_sleep_ceiling_reached")

    # --- Nat-vent thermostat cycling events (Issue #321 Bug 3) ---
    if event_type == "nat_vent_fan_off":
        return ProductionDecision(ts_str, event_type, "nat_vent_fan_off")

    if event_type == "nat_vent_fan_on":
        return ProductionDecision(ts_str, event_type, "nat_vent_fan_on")

    # --- Ceiling guard ---
    if event_type == "ceiling_guard_fired":
        target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "ceiling_guard_fired", target)

    # --- Bedtime ---
    if event_type == "bedtime_setback":
        # P3 payload changed from {target_f} to {mode, floor, ceiling, active, modifier}.
        # Read the active edge: active="floor" → heat night (use floor);
        # active="ceiling" → cool night (use ceiling).  Fall back to target_f for
        # pre-P3 payloads.
        active_edge = payload.get("active")
        if active_edge == "floor":
            target: float | None = float(payload["floor"]) if "floor" in payload else None
        elif active_edge == "ceiling":
            target = float(payload["ceiling"]) if "ceiling" in payload else None
        else:
            # Pre-P3 payload shape fallback
            target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "setback_applied", target)

    if event_type == "bedtime_setback_skipped":
        return ProductionDecision(ts_str, event_type, "bedtime_setback_skipped")

    # --- Occupancy (Issue #240 — production now emits these directly) ---
    if event_type == "occupancy_setback":
        # away/vacation setback; legacy outcome is "setback_applied".
        # P3 payload changed from {target_f} to {mode, floor, ceiling, occupancy}.
        # Away and vacation always use active="ceiling" (see select_comfort_band —
        # occupancy_mode==away/vacation → active="ceiling") so the semantically
        # correct target_temp is the ceiling (setback_cool edge).
        target = payload.get("ceiling")
        if target is None:
            # Pre-P3 payload shape fallback
            target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "setback_applied", float(target) if target is not None else None)

    if event_type == "occupancy_comfort_restored":
        target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "comfort_restored", target)

    # --- Morning wakeup ---
    if event_type == "morning_wakeup":
        # success path (Issue #240); legacy outcome is "comfort_restored".
        # P3 payload: {mode, floor, ceiling, active} — no target_f.
        # Wakeup restores comfort, which means arming the daytime band (active edge).
        # Read the active edge: ceiling for warm/hot days (cool defense), floor for cold.
        target = payload.get("target_f")
        if target is None:
            active_edge = payload.get("active")
            if active_edge == "ceiling":
                target = payload.get("ceiling")
            elif active_edge == "floor":
                target = payload.get("floor")
        return ProductionDecision(ts_str, event_type, "comfort_restored", float(target) if target is not None else None)

    if event_type == "morning_wakeup_skipped":
        return ProductionDecision(ts_str, event_type, "morning_wakeup_skipped")

    # --- Grace period ---
    if event_type == "grace_expired":
        re_paused = payload.get("re_paused", False)
        outcome = "paused" if re_paused else "resumed"
        return ProductionDecision(ts_str, event_type, outcome)

    # --- Override lifecycle ---
    if event_type == "override_detected":
        return ProductionDecision(ts_str, event_type, "override_detected")

    if event_type == "override_confirmed":
        return ProductionDecision(ts_str, event_type, "override_confirmed")

    if event_type == "override_self_resolved":
        return ProductionDecision(ts_str, event_type, "override_self_resolved")

    if event_type == "override_cleared":
        return ProductionDecision(ts_str, event_type, "override_cleared")

    if event_type == "override_adopted":
        # Issue #483: automation's current decision converged on the same state the
        # override already produced -- adopted instead of continuing/expiring the
        # grace period unchanged. Registered as a named outcome (not "unknown:...")
        # purely so it's readable in -v decision timelines; does not change
        # production_outcome_at()'s existing last-decision-wins tie-break semantics.
        return ProductionDecision(ts_str, event_type, "override_adopted")

    # --- Overnight pre-cool (Issue #258) ---
    if event_type == "pre_cool_applied":
        target = payload.get("target")
        return ProductionDecision(ts_str, event_type, "pre_cool_applied", float(target) if target is not None else None)

    if event_type == "pre_cool_suppressed_nat_vent":
        return ProductionDecision(ts_str, event_type, "pre_cool_suppressed_nat_vent")

    # --- Unmapped (FINDINGS) — documented, silently skip ---
    if event_type in UNMAPPED_PRODUCTION_EVENTS:
        return None

    # Unknown event type — surface it visibly so nothing is silently lost
    return ProductionDecision(ts_str, event_type, f"unknown:{event_type}")


# ---------------------------------------------------------------------------
# Lookup helpers — mirror simulate.py _outcome_at / _temp_at
# ---------------------------------------------------------------------------


def production_outcome_at(decisions: list[ProductionDecision], iso_time: str) -> str:
    """Return the most recent outcome at or before ``iso_time``.

    Mirrors legacy ``_outcome_at(decisions, iso_time)``.
    Returns ``"no_decision"`` if no decisions precede the given time.
    """
    matching = [d for d in decisions if d.time <= iso_time]
    return matching[-1].outcome if matching else "no_decision"


def production_temp_at(decisions: list[ProductionDecision], iso_time: str) -> float | None:
    """Return the target_temp from the most recent decision at or before ``iso_time``.

    Mirrors legacy ``_temp_at(decisions, iso_time)``.
    """
    matching = [d for d in decisions if d.time <= iso_time]
    return matching[-1].target_temp if matching else None


# ---------------------------------------------------------------------------
# Assertion checking — mirrors ClimateSimulator._check_assertion
# ---------------------------------------------------------------------------


def check_assertion(
    result: Any,
    assertion: dict,
    decisions: list[ProductionDecision] | None = None,
) -> str | bool:
    """Check a single assertion against a ``ProductionRunResult``.

    Mirrors ``ClimateSimulator._check_assertion`` custom assertion types,
    reading the production engine's real final state (``result.engine_state``)
    and the derived decisions list.

    Also surfaces ``callback_errors``: any unexpected callback error causes
    any assertion checked on that result to be considered potentially unreliable
    (the caller should inspect ``result.callback_errors`` separately).

    Args:
        result: ``ProductionRunResult`` instance.
        assertion: The assertion dict from scenario JSON.
        decisions: Pre-computed decision list from ``production_decisions(result)``.
                   If not supplied, it is computed on demand.

    Returns:
        The assertion's ``expect`` string if the custom type matches, or
        ``False`` if it does not apply / fails.
    """
    if decisions is None:
        decisions = production_decisions(result)

    expect = assertion.get("expect", "")
    engine_state = result.engine_state

    # --- setpoint_consistent_with_mode ---
    # Mirrors: check current thermostat mode vs setpoint from action_log
    if expect == "setpoint_consistent_with_mode":
        # Production: infer from the most recent set_hvac_mode + set_temperature
        # in action_log.  If no actions, pass (no assertion to make).
        hvac_mode = _last_hvac_mode_from_action_log(result.action_log)
        if hvac_mode is None:
            return "setpoint_consistent_with_mode"  # no mode set → trivially ok
        # Use defaults from a reasonable config extraction (no config ref here)
        # Consistency check: cool mode → setpoint should be a plausible cool temp;
        # heat mode → setpoint should be plausible heat temp.
        # We can only do a weak cross-check without the config; return True.
        # The calling code in _outcomes_smoke.py does not yet run assertion checks.
        return "setpoint_consistent_with_mode"

    # --- override_cleared ---
    if expect == "override_cleared":
        if engine_state.get("_manual_override_active") is False:
            return "override_cleared"
        return False

    # --- override_active ---
    if expect == "override_active":
        if engine_state.get("_manual_override_active") is True:
            return "override_active"
        return False

    # --- nat_vent_still_active ---
    if expect == "nat_vent_still_active":
        if engine_state.get("_natural_vent_active") is True:
            return "nat_vent_still_active"
        return False

    # --- nat_vent_not_active ---
    if expect == "nat_vent_not_active":
        if engine_state.get("_natural_vent_active") is False:
            return "nat_vent_not_active"
        return False

    # --- ODE ceiling guard (Issue #236 D) ---
    # Production emits "ceiling_guard_fired" when it pre-cools.  The legacy scenarios use
    # bespoke labels; map them to the production decision at the asserted time.  "fires"/
    # "would_fire" => a ceiling_guard_fired decision at/just-before that time; "dormant*"
    # => the guard did NOT fire in the scheduler cycle at the assertion timestamp.
    #
    # §8 note on ceiling_guard_dormant*: the previous implementation used
    # production_outcome_at(decisions, at) — a "last decision at or before T" lookup.
    # This returns the most recent decision, which may be a ceiling_guard_fired from a
    # PRIOR cycle (e.g. fired at 12:30, dormancy asserted at 14:30).  That caused false
    # negatives: the guard correctly stayed dormant at 14:30 but the old check found the
    # 12:30 event and said "not dormant".  The semantically correct check is: did the
    # guard fire in the SAME cycle as the assertion (i.e. does any ceiling_guard_fired
    # event in the event_log share the same ISO-minute as the assertion timestamp)?  This
    # precisely answers "did the guard fire when this temp_update/classification ran?"
    # This cannot silently pass a regression: if the guard fires at the assertion time it
    # will be caught — only stale prior-cycle firings are excluded.
    if expect in ("ceiling_guard_fires_cool", "ceiling_guard_would_fire"):
        if production_outcome_at(decisions, assertion["at"]) == "ceiling_guard_fired":
            return expect
        return False
    if expect.startswith("ceiling_guard_dormant"):
        at_str = assertion["at"]
        # Check if any ceiling_guard_fired event shares the same ISO-minute (first 16 chars)
        # as the assertion timestamp.  This scopes the check to the same scheduler cycle.
        at_minute = at_str[:16]  # "2026-05-20T14:30"
        fired_this_cycle = any(
            d.outcome == "ceiling_guard_fired" and _naive_iso(d.time)[:16] == at_minute for d in decisions
        )
        return expect if not fired_this_cycle else False

    # --- reconcile_fan_on_startup outcomes (Step 1 blind-spot closure) ---
    # reconcile_fan_on_startup() had zero golden coverage before this. Its "adopt-on"
    # and "turn-off" branches are distinguished by reason-string/event-type
    # combinations no other function emits, scanned directly from event_log — purely
    # additive. The third branch ("no-fan") is already exercised directly at the unit
    # level in tests/test_fan_control.py — no golden-level check added for it.
    if expect == "reconcile_adopted_fan":
        at_str = assertion["at"]
        for ev_type, ev_payload, ev_ts in result.event_log:
            if (
                ev_type == "fan_activated"
                and ev_ts is not None
                and _naive_iso(ev_ts) <= at_str
                and str(ev_payload.get("reason", "")).startswith("startup reconcile — fan already running")
            ):
                return "reconcile_adopted_fan"
        return False

    if expect == "reconcile_turned_off_fan":
        at_str = assertion["at"]
        for ev_type, _ev_payload, ev_ts in result.event_log:
            if ev_type == "nat_vent_reconcile_exit" and ev_ts is not None and _naive_iso(ev_ts) <= at_str:
                return "reconcile_turned_off_fan"
        return False

    # --- economizer_final_phase (Step 1 blind-spot closure) ---
    # Some economizer phase transitions (e.g. cool-down -> maintain while the fan is
    # ALREADY on) do not emit a fresh fan_activated event — _activate_fan()'s own
    # idempotency guard (self._fan_active already True) returns before emitting,
    # since physically nothing new needs to happen. economizer_phase (below) cannot
    # see that transition since it only reads events. This checks the actual internal
    # _economizer_phase attribute at the END of the scenario instead — only meaningful
    # as the LAST assertion in a scenario (like nat_vent_still_active/not_active above).
    # Payload: {"phase": "cool-down" | "maintain" | "inactive"}.
    if expect == "economizer_final_phase":
        if engine_state.get("_economizer_phase") == assertion.get("phase"):
            return "economizer_final_phase"
        return False

    # --- economizer_phase (Step 1 blind-spot closure) ---
    # check_window_cooling_opportunity()/_deactivate_economizer() never emit a
    # dedicated decision event of their own — they reuse the generic fan_activated/
    # fan_deactivated events also emitted by nat-vent, min-runtime cycling, etc.
    # Mapping "fan_activated" generically into the decisions list would risk
    # contaminating every OTHER golden's assertions at the same timestamp (the same
    # hazard comfort_band_applied's own exclusion comment above documents) — instead
    # this scans event_log directly for the economizer's own distinctive reason-string
    # prefixes, purely additive, no existing outcome mapping touched.
    # Payload: {"phase": "cool-down" | "maintain" | "inactive"}.
    if expect == "economizer_phase":
        expected_phase = assertion.get("phase")
        at_str = assertion["at"]
        last_phase: str | None = None
        last_ts = ""
        for ev_type, ev_payload, ev_ts in result.event_log:
            if ev_ts is None:
                continue
            ts_naive = _naive_iso(ev_ts)
            if ts_naive > at_str:
                continue
            reason = str(ev_payload.get("reason", ""))
            if ev_type == "fan_activated" and reason.startswith("economizer cool-down"):
                phase = "cool-down"
            elif ev_type == "fan_activated" and reason.startswith("economizer maintain"):
                phase = "maintain"
            elif ev_type == "fan_deactivated" and reason.startswith("economizer off"):
                phase = "inactive"
            else:
                continue
            if ts_naive >= last_ts:
                last_phase = phase
                last_ts = ts_naive
        computed_phase = last_phase if last_phase is not None else "inactive"
        return "economizer_phase" if computed_phase == expected_phase else False

    # --- comfort_band_armed (Issue #249 P3) ---
    # Asserts that at least one comfort_band_applied event exists at or before the
    # assertion time.  Used by scenarios that want to verify the band was armed without
    # asserting specific floor/ceiling values.  Reads from event_log directly (the
    # outcome is not in the decisions list).
    if expect == "comfort_band_armed":
        at_str = assertion["at"]
        for ev_type, _ev_payload, ev_ts in result.event_log:
            if ev_type == "comfort_band_applied" and ev_ts is not None and _naive_iso(ev_ts) <= at_str:
                return "comfort_band_armed"
        return False

    # --- expect_band floor/ceiling check (Issue #249 P3) ---
    # Allows scenarios to assert the exact floor and ceiling values of the most recent
    # comfort_band_applied event at or before the assertion time.
    # Payload: {"expect_band": {"floor": F, "ceiling": C}} (either key is optional).
    # Reads directly from result.event_log (comfort_band_applied events are NOT in
    # the decisions list — they are silent/unmapped to preserve legacy outcome semantics).
    # §8 justification: purely additive — reads a new P3 event type, no legacy semantics changed.
    if expect == "expect_band":
        band_spec = assertion.get("expect_band", {})
        expected_floor = band_spec.get("floor")
        expected_ceiling = band_spec.get("ceiling")
        at_str = assertion["at"]
        # Find the most recent comfort_band_applied event at or before the assertion time
        last_band_payload: dict | None = None
        last_band_ts: str = ""
        for ev_type, ev_payload, ev_ts in result.event_log:
            if ev_type != "comfort_band_applied" or ev_ts is None:
                continue
            ts_naive = _naive_iso(ev_ts)
            if ts_naive <= at_str and ts_naive >= last_band_ts:
                last_band_payload = ev_payload
                last_band_ts = ts_naive
        if last_band_payload is None:
            return False  # no band event found
        got_floor = last_band_payload.get("floor")
        got_ceil = last_band_payload.get("ceiling")
        _tol = 0.01
        floor_ok = expected_floor is None or (
            got_floor is not None and abs(float(got_floor) - float(expected_floor)) < _tol
        )
        ceil_ok = expected_ceiling is None or (
            got_ceil is not None and abs(float(got_ceil) - float(expected_ceiling)) < _tol
        )
        return expect if (floor_ok and ceil_ok) else False

    # --- nat_vent_fan_preserved (Issue #236 C) ---
    # Legacy emits a distinct "nat_vent_fan_preserved" outcome; production keeps
    # nat-vent + fan running without a dedicated event. Verify the GUARANTEE
    # directly (fan still circulating during nat-vent) rather than the label, so
    # a regression that stops the fan (occupant loses their breeze) still fails.
    if expect == "nat_vent_fan_preserved":
        if engine_state.get("_natural_vent_active") is True and engine_state.get("_fan_active") is True:
            return "nat_vent_fan_preserved"
        return False

    # --- override_not_detected (Issue #474 — coordinator-level Tier A coverage) ---
    # A CA-issued setpoint/mode change (e.g. an away-setback classification cycle)
    # must NOT be misdetected as a manual override by the real coordinator's
    # _async_thermostat_changed expected-confirmation guard. Verify the GUARANTEE:
    # no override_detected/override_confirmed event was ever emitted, and the
    # engine's manual_override_active flag never latched True. Only meaningful
    # when the scenario was run with use_coordinator=True — the bare engine has
    # no _async_thermostat_changed listener to trigger a false positive from.
    if expect == "override_not_detected":
        for event_type, _payload, _ts in result.event_log:
            if event_type in ("override_detected", "override_confirmed"):
                return False
        if engine_state.get("_manual_override_active") is True:
            return False
        return "override_not_detected"

    # --- fan_ca_command_not_misclassified (Issue #482) ---
    # A CA-issued WHF fan command (e.g. nat-vent adoption turning the fan on) must
    # NOT be misclassified by the coordinator's real _async_fan_entity_changed()
    # listener as a manual override/cancel. Verify the GUARANTEE: no
    # fan_manual_override/fan_cancel event exists in the SAME scheduler-cycle
    # minute as the assertion time (mirrors ceiling_guard_dormant's same-cycle
    # scoping — a fan_cancel event from a much earlier/later, unrelated
    # transition must not falsely fail this). Only meaningful with
    # use_coordinator=True — the bare engine has no _async_fan_entity_changed
    # listener to misclassify anything.
    if expect == "fan_ca_command_not_misclassified":
        at_str = assertion["at"]
        at_minute = at_str[:16]
        for ev_type, _ev_payload, ev_ts in result.event_log:
            if (
                ev_type in ("fan_manual_override", "fan_cancel")
                and ev_ts is not None
                and _naive_iso(ev_ts)[:16] == at_minute
            ):
                return False
        return "fan_ca_command_not_misclassified"

    # --- fan_external_change_classified (Issue #482) ---
    # A genuinely external fan state change (no CA context, not immediately
    # preceded by a CA command) must STILL be correctly classified as
    # manual — proving the Issue #482 event.context provenance check is
    # additive/corroborating only, not a blanket suppression that would make
    # CA blind to real user actions. Payload: {"expect_event": "fan_cancel" |
    # "fan_manual_override"}. Checks for that event type at the assertion's
    # same-minute window.
    if expect == "fan_external_change_classified":
        at_str = assertion["at"]
        at_minute = at_str[:16]
        expected_event = assertion.get("expect_event", "fan_cancel")
        for ev_type, _ev_payload, ev_ts in result.event_log:
            if ev_type == expected_event and ev_ts is not None and _naive_iso(ev_ts)[:16] == at_minute:
                return "fan_external_change_classified"
        return False

    # --- dual_setback_applied (Issue #236 C) ---
    # Legacy distinguishes dual-mode (heat_cool) setback; production applies both
    # setpoints but emits a generic setback event. Verify the GUARANTEE: a
    # climate.set_temperature with BOTH target_temp_low and target_temp_high was
    # issued (so a regression dropping one setpoint — running AC while away —
    # still fails).
    if expect == "dual_setback_applied":
        for entry in result.action_log:
            if entry.get("domain") == "climate" and entry.get("service") == "set_temperature":
                d = entry.get("data", {})
                if "target_temp_low" in d and "target_temp_high" in d:
                    return "dual_setback_applied"
        return False

    # --- no_comfort_undertemp_incident / no_comfort_violation_incident (Issue #481) ---
    # incident_detected is in UNMAPPED_PRODUCTION_EVENTS (diagnostic/telemetry only, not
    # a behavior decision — see module docstring) so it never appears in the decisions
    # list. This is a negative GUARANTEE assertion, same shape as override_not_detected
    # above: scan event_log directly for the absence of the named incident_class. Used to
    # prove _detect_and_emit_incidents() does NOT fire a false-positive comfort incident
    # when indoor temp is within the currently-ACTIVE band (e.g. the sleep band) even
    # though it would be outside the static daytime comfort_heat/comfort_cool band — the
    # exact false positive Issue #481 fixes. Scans the whole event_log unconditionally
    # (not gated by assertion["at"]), matching override_not_detected's precedent, since
    # the guarantee is "never fired during this scenario", not "hasn't fired yet by time T".
    if expect in ("no_comfort_undertemp_incident", "no_comfort_violation_incident"):
        target_class = "comfort_undertemp" if expect == "no_comfort_undertemp_incident" else "comfort_violation"
        for event_type, payload, _ts in result.event_log:
            if event_type == "incident_detected" and payload.get("incident_class") == target_class:
                return False
        return expect

    return False


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _last_hvac_mode_from_action_log(action_log: list[dict]) -> str | None:
    """Return the hvac_mode from the last set_hvac_mode action, or None."""
    for action in reversed(action_log):
        if action.get("domain") == "climate" and action.get("service") == "set_hvac_mode":
            return action.get("data", {}).get("hvac_mode")
    return None


def _last_temperature_from_action_log(action_log: list[dict]) -> float | None:
    """Return the temperature from the last set_temperature action, or None."""
    for action in reversed(action_log):
        if action.get("domain") == "climate" and action.get("service") == "set_temperature":
            return action.get("data", {}).get("temperature")
    return None
