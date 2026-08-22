"""Pure decision core for the ODE ceiling guard (Issue #742, Phase 3 of the
strangler-fig completion program — classification FSM).

Extracts the single largest never-extracted block inside ``apply_classification()``
(automation.py, ~190 lines): the proactive-cooling escalation that fires when the
ODE-predicted indoor temperature curve shows a comfort_cool breach within lead
time, while nat-vent/free-cooling is dormant or has stopped being viable
(Issue #136, dormancy fixed per Issue #247, archetype-aware threshold per
Issue #392 Fix 1).

Same "explicit ``*Inputs`` frozen dataclass, an outcome enum + decision
dataclass, zero side effects, zero logging" contract every other pure leaf
module in this codebase documents for itself (``nat_vent_exit.py``,
``fan_thermostat_decision.py``). Logging and side effects (HVAC mode/setpoint
writes, fan deactivation, event emission) stay in the ``automation.py`` shell —
this module only decides WHETHER and WHY to escalate.

**Five-whys on scope: why does ``ceiling_threshold`` stay a caller-resolved
input rather than a field this module derives itself?** (1) Why not derive it
here? Because the real value comes from ``AutomationEngine._ceiling_threshold()``,
which reads live config (``CONF_FAN_MODE``, ``aggressive_savings``,
``comfort_cool``) — this module could re-derive it (as ``nat_vent_gate.py``'s
``_resolve_ceiling_threshold()`` already does for its own caller), but (2) why
not reuse ``nat_vent_gate.py``'s reimplementation? Doing so would make this
module depend on nat-vent's own gate module for a single scalar, coupling two
otherwise-independent subsystems for no behavioral gain — ``apply_classification()``
already resolves ``_ceiling_threshold()`` once, inline, before this section runs
today, exactly the same way ``nat_vent_exit.py``'s ``NatVentExitInputs.comfort_heat_raw``
and ``in_sleep_window`` are caller-resolved rather than re-derived. (3) Why does
that matter? A caller-resolved scalar is the lowest-risk extraction shape —
it changes nothing about who owns the archetype-aware threshold logic, so a
latent bug in ``_ceiling_threshold()`` itself (none found during this phase)
would surface identically on both the legacy and FSM-authoritative paths,
never causing a false divergence in the differential comparator. (4) Why not
just inline the threshold formula here for good measure? Because
``_ceiling_threshold()`` is config-dependent (``CONF_FAN_MODE``,
``aggressive_savings``) in a way this module's own "explicit inputs, no
hidden config reads" contract already forbids reading directly — the existing
convention (every pure module in this codebase resolves config via the
caller, never via a ``self.config.get()`` of its own) settles it. (5) Why
mention this at all? Because a future session extracting ``_ceiling_threshold()``
itself into a pure leaf should wire it in as an additional caller-resolved
step BEFORE this module, not fold its logic into this module's own inputs.

Validated via differential replay against the real production method (which
this module's logic mirrors line-for-line) — see
``tools/sim_harness/classification_fsm_authoritative_compare.py`` and
``tests/test_classification_fsm_authoritative_compare.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

# Mirrors const.py's CEILING_PRECOOL_FALLBACK_MIN / CEILING_BRIDGE_TOLERANCE_F —
# duplicated here (plain scalar constants, not logic), matching every other pure
# leaf module's own "never import from const.py or automation.py" convention.
_CEILING_PRECOOL_FALLBACK_MIN = 120.0
_CEILING_BRIDGE_TOLERANCE_F = 1.0

_LEAD_MIN_FLOOR = 30.0
_LEAD_MIN_CEIL = 240.0
_LEAD_MIN_LOOKAHEAD_MULTIPLIER = 1.3

_HVAC_MODE_OFF = "off"


class OdeCeilingGuardOutcome(Enum):
    """Why the guard did (or did not) escalate — one member per distinct
    ``elif``/terminal branch in the production method this mirrors, in the
    same priority order production checks them."""

    NOT_APPLICABLE = "not_applicable"  # predicted_indoor empty/None, or classification.hvac_mode != "off"
    MODEL_INELIGIBLE = "model_ineligible"  # k_passive/confidence/comfort_cool preconditions not met
    MISSING_TEMPS = "missing_temps"  # outdoor or indoor reading unavailable
    NO_CEILING_THRESHOLD = "no_ceiling_threshold"  # WHOLE_HOUSE/BOTH archetype — no compressor handoff point
    DORMANT = "dormant"  # free cooling viable (Issue #247's 3-condition dormancy test)
    NO_BREACH_PREDICTED = "no_breach_predicted"  # curve never crosses comfort_cool + tolerance
    STANDING_BY = "standing_by"  # breach predicted, but outside the lead-time window
    ESCALATE = "escalate"  # breach within lead time — shell must switch to active cooling


@dataclass(frozen=True)
class OdeCeilingGuardDecision:
    """The outcome, plus every computed value the shell's logging/event payload
    needs (avoids re-deriving breach_ts/hours_to_breach/lead_min a second time
    in automation.py — mirrors ``nat_vent_exit.py``'s ``NatVentExitDecision``
    convention of carrying its own computed audit fields)."""

    outcome: OdeCeilingGuardOutcome
    breach_ts: datetime | None = None
    hours_to_breach: float | None = None
    lead_min: float | None = None
    should_deactivate_fan: bool = False


@dataclass(frozen=True)
class OdeCeilingGuardInputs:
    """Every input the guard may read — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine``/config reads
    (all inside ``apply_classification()``'s ODE ceiling guard block, automation.py
    ~L2664-2851):
      predicted_indoor        -> apply_classification()'s own predicted_indoor param
      hvac_mode                -> classification.hvac_mode
      k_passive                 -> (self._thermal_model or {}).get("k_passive")
      confidence_k_passive       -> (self._thermal_model or {}).get("confidence_k_passive")
                                    or .get("confidence", "none")
      k_passive_via_bridge        -> bool((self._thermal_model or {}).get("k_passive_via_bridge"))
      k_active_cool                 -> (self._thermal_model or {}).get("k_active_cool")
      comfort_cool                   -> config comfort_cool
      outdoor                          -> self._last_outdoor_temp
      indoor                             -> self._get_indoor_temp_f()
      natural_vent_active                 -> self._natural_vent_active
      ceiling_threshold                    -> self._ceiling_threshold(comfort_cool) — caller-
                                              resolved; see module docstring's five-whys for why
                                              this module does not re-derive it itself
      now                                   -> caller-resolved wall-clock time (dt_util.now()),
                                              same convention as every other pure module's `now`
    """

    predicted_indoor: list[dict] | None
    hvac_mode: str | None
    k_passive: float | None
    confidence_k_passive: str
    k_passive_via_bridge: bool
    k_active_cool: float | None
    comfort_cool: float | None
    outdoor: float | None
    indoor: float | None
    natural_vent_active: bool
    ceiling_threshold: float | None
    now: datetime


def _model_eligible(inputs: OdeCeilingGuardInputs) -> bool:
    return (
        inputs.k_passive is not None
        and inputs.k_passive < 0
        and (inputs.confidence_k_passive != "none" or inputs.k_passive_via_bridge)
        and inputs.comfort_cool is not None
    )


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _scan_for_breach(predicted_indoor: list[dict], threshold: float) -> datetime | None:
    """Pure reimplementation of the inline breach scan in apply_classification()
    (which itself documents itself as "avoids circular import" — coordinator.py's
    ``_find_ceiling_breach_time()`` cannot be imported from automation.py without
    a cycle, since coordinator.py imports automation.py). This module is a leaf
    with no automation.py/coordinator.py imports, so it does not inherit that
    constraint — but it still keeps its own independent copy rather than
    importing coordinator.py, preserving the "pure modules never import the
    orchestration layer" convention every other leaf in this codebase follows."""
    for entry in predicted_indoor:
        temp = entry.get("temp")
        if temp is None or temp <= threshold:
            continue
        ts_str = entry.get("ts")
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str)
        except (KeyError, ValueError, TypeError):
            return None
    return None


def decide_ode_ceiling_guard(inputs: OdeCeilingGuardInputs) -> OdeCeilingGuardDecision:
    """Pure reimplementation of apply_classification()'s ODE ceiling guard
    (automation.py ~L2664-2851). Same eligibility order, same dormancy test
    (Issue #247's 3 conditions), same breach scan, same lead-time formula as
    production — exists to be differentially validated against the real
    method, not to introduce new behavior.
    """
    if not inputs.predicted_indoor or inputs.hvac_mode != _HVAC_MODE_OFF:
        return OdeCeilingGuardDecision(outcome=OdeCeilingGuardOutcome.NOT_APPLICABLE)

    if not _model_eligible(inputs):
        return OdeCeilingGuardDecision(outcome=OdeCeilingGuardOutcome.MODEL_INELIGIBLE)

    if inputs.outdoor is None or inputs.indoor is None:
        return OdeCeilingGuardDecision(outcome=OdeCeilingGuardOutcome.MISSING_TEMPS)

    if inputs.ceiling_threshold is None:
        # Issue #402: WHOLE_HOUSE/BOTH archetypes have no ceiling-based compressor
        # handoff at all — never escalate, regardless of nat-vent transients.
        return OdeCeilingGuardDecision(outcome=OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD)

    # Issue #247: dormancy is THREE conditions — outdoor cooler than indoor AND
    # nat-vent actually running AND indoor still within the ceiling threshold.
    if inputs.outdoor <= inputs.indoor and inputs.natural_vent_active and inputs.indoor <= inputs.ceiling_threshold:
        return OdeCeilingGuardDecision(outcome=OdeCeilingGuardOutcome.DORMANT)

    tolerance = _CEILING_BRIDGE_TOLERANCE_F if inputs.k_passive_via_bridge else 0.0
    threshold = inputs.comfort_cool + tolerance
    breach_ts = _scan_for_breach(inputs.predicted_indoor, threshold)
    if breach_ts is None:
        return OdeCeilingGuardDecision(outcome=OdeCeilingGuardOutcome.NO_BREACH_PREDICTED)

    hours_to_breach = (_to_utc(breach_ts) - _to_utc(inputs.now)).total_seconds() / 3600

    if inputs.k_active_cool is not None and abs(inputs.k_active_cool) > 0:
        _raw_lead_hr = (inputs.comfort_cool - inputs.indoor) / abs(inputs.k_active_cool)
        lead_min = _raw_lead_hr * 60 * _LEAD_MIN_LOOKAHEAD_MULTIPLIER
    else:
        lead_min = _CEILING_PRECOOL_FALLBACK_MIN
    lead_min = max(_LEAD_MIN_FLOOR, min(_LEAD_MIN_CEIL, lead_min))

    if hours_to_breach <= lead_min / 60:
        return OdeCeilingGuardDecision(
            outcome=OdeCeilingGuardOutcome.ESCALATE,
            breach_ts=breach_ts,
            hours_to_breach=hours_to_breach,
            lead_min=lead_min,
            should_deactivate_fan=inputs.natural_vent_active,
        )

    return OdeCeilingGuardDecision(
        outcome=OdeCeilingGuardOutcome.STANDING_BY,
        breach_ts=breach_ts,
        hours_to_breach=hours_to_breach,
        lead_min=lead_min,
    )
