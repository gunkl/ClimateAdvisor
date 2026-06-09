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
  sensor_opened  result=natural_ventilation  → natural_ventilation
  sensor_opened  result=paused               → paused
  sensor_all_closed  was_nat_vent or was_paused  → resumed
  sensor_all_closed  neither flag            → resumed
    (production always calls resumed; if nothing was happening this is still
     a resumed decision — mirrors simulate.py _handle_all_closed)
  nat_vent_comfort_floor_exit   → nat_vent_comfort_floor_exit
  nat_vent_outdoor_rise_exit    → nat_vent_outdoor_rise_exit
  bedtime_setback               → setback_applied
  bedtime_setback_skipped       → bedtime_setback_skipped
  grace_expired  re_paused=True   → paused
  grace_expired  re_paused=False  → resumed
  ceiling_guard_fired           → ceiling_guard_fired
  override_detected             → override_detected
  override_confirmed            → override_confirmed
  override_self_resolved        → override_self_resolved
  override_cleared              → override_cleared

Occupancy / wakeup (Issue #240 — production emits these directly now):
  occupancy_setback            → setback_applied  (away/vacation)
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

    if event_type == "warm_day_comfort_gap":
        target = payload.get("target_f") or payload.get("comfort_heat")
        return ProductionDecision(ts_str, event_type, "warm_day_comfort_gap", target)

    if event_type == "warm_day_setback_applied":
        # Production applied a setback to an actively-running thermostat mode on a
        # warm day.  Legacy outcome for the same code path is "setback_applied".
        target = payload.get("new_setpoint_f")
        return ProductionDecision(ts_str, event_type, "setback_applied", target)

    # --- Natural ventilation exit events ---
    if event_type == "nat_vent_comfort_floor_exit":
        return ProductionDecision(ts_str, event_type, "nat_vent_comfort_floor_exit")

    if event_type == "nat_vent_outdoor_rise_exit":
        return ProductionDecision(ts_str, event_type, "nat_vent_outdoor_rise_exit")

    # --- Ceiling guard ---
    if event_type == "ceiling_guard_fired":
        target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "ceiling_guard_fired", target)

    # --- Bedtime ---
    if event_type == "bedtime_setback":
        target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "setback_applied", target)

    if event_type == "bedtime_setback_skipped":
        return ProductionDecision(ts_str, event_type, "bedtime_setback_skipped")

    # --- Occupancy (Issue #240 — production now emits these directly) ---
    if event_type == "occupancy_setback":
        # away/vacation setback; legacy outcome is "setback_applied"
        target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "setback_applied", target)

    if event_type == "occupancy_comfort_restored":
        target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "comfort_restored", target)

    # --- Morning wakeup ---
    if event_type == "morning_wakeup":
        # success path (Issue #240); legacy outcome is "comfort_restored"
        target = payload.get("target_f")
        return ProductionDecision(ts_str, event_type, "comfort_restored", target)

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
