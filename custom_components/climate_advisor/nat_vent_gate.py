"""Pure decision core for the nat-vent reactivation gate (architecture-reset Step 2).

This is the first proof slice of the functional-core approach: a pure
reimplementation of the exact boolean gate already unified across 5 production
call sites in ``automation.py`` (``_nat_vent_may_reactivate()``,
``_ceiling_threshold()``, ``_nat_vent_reactivation_floor()`` — consolidated by
Issues #411/#417 after the site-drift bug class documented in project memory).

Scope: this module answers exactly one question — "given current conditions,
should nat-vent (re)activate right now?" — as a pure function of an explicit
``NatVentGateInputs`` value. It does NOT model the surrounding session state
machine (grace periods, reactivation lockout timers, physical fan drift
reconciliation) — those stay in ``automation.py`` for now; expanding the pure
core to cover them is a later step (see the architecture-reset plan's Step 3).

Every field on ``NatVentGateInputs`` corresponds to a value the real
``_nat_vent_may_reactivate()`` call chain reads (directly, or via
``self.config``/``dt_util.now()``) — nothing is hidden or implicit. In
particular ``in_sleep_window`` replaces the real code's internal
``dt_util.now()`` read: the caller resolves wall-clock time once, the pure core
never touches it.

Validated via differential replay against the real production method — see
``tools/nat_vent_gate_diff.py`` and the Step-2 status report for results.
"""

from __future__ import annotations

from dataclasses import dataclass

# Mirrors custom_components.climate_advisor.const.CEILING_ESCALATION_SAVINGS_MARGIN_F —
# duplicated here (a plain float constant, not logic) rather than imported, to keep this
# module import-independent of the rest of the integration during the proof-slice phase.
# TODO(step 3+): import from const.py once this module is wired into production.
_CEILING_ESCALATION_SAVINGS_MARGIN_F = 2.0

FAN_MODE_DISABLED = "disabled"
FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
FAN_MODE_HVAC = "hvac_fan"
FAN_MODE_BOTH = "both"


@dataclass(frozen=True)
class NatVentGateInputs:
    """Every input the reactivation gate may read — explicit, nothing hidden.

    Field-by-field correspondence to the real code:
      outdoor, indoor          -> _nat_vent_may_reactivate(outdoor=, indoor=)
      comfort_heat_raw         -> the caller's raw config comfort_heat (before sleep adjustment)
      sleep_heat               -> config CONF_SLEEP_HEAT, falls back to comfort_heat_raw
      in_sleep_window          -> replaces _in_sleep_window(dt_util.now(), config) — resolved
                                   by the caller, never read from the wall clock in here
      comfort_cool             -> _nat_vent_may_reactivate(comfort_cool=) / _ceiling_threshold(comfort_cool)
      nat_vent_delta           -> used to compute threshold = comfort_cool + nat_vent_delta
                                   (every real call site computes this identically)
      hysteresis               -> _nat_vent_may_reactivate(hysteresis=) — 0.0 at 2 call sites
                                   (handle_door_window_open, _re_pause_for_open_sensor),
                                   the configured value at the other 3
      fan_mode                 -> _ceiling_threshold()'s archetype branch
      aggressive_savings       -> _ceiling_threshold()'s savings-margin branch
    """

    outdoor: float | None
    indoor: float | None
    comfort_heat_raw: float
    sleep_heat: float
    in_sleep_window: bool
    comfort_cool: float
    nat_vent_delta: float
    hysteresis: float
    fan_mode: str
    aggressive_savings: bool


def _resolve_comfort_heat(inputs: NatVentGateInputs) -> float:
    """Pure reimplementation of _nat_vent_reactivation_floor() (Issue #417)."""
    return inputs.sleep_heat if inputs.in_sleep_window else inputs.comfort_heat_raw


def _resolve_ceiling_threshold(inputs: NatVentGateInputs) -> float | None:
    """Pure reimplementation of _ceiling_threshold()."""
    if inputs.fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return None
    if inputs.aggressive_savings:
        return inputs.comfort_cool + _CEILING_ESCALATION_SAVINGS_MARGIN_F
    return inputs.comfort_cool


def decide_nat_vent_gate(inputs: NatVentGateInputs) -> bool:
    """Pure reimplementation of _nat_vent_may_reactivate() (Issue #411 Pass 4).

    Same 4-part gate, same boundary semantics (strict '<', '<=' on the ceiling
    check) as production — this function exists to be differentially validated
    against the real method, not to introduce new behavior.
    """
    if inputs.outdoor is None or inputs.indoor is None:
        return False

    comfort_heat = _resolve_comfort_heat(inputs)
    threshold = inputs.comfort_cool + inputs.nat_vent_delta
    ceiling_threshold = _resolve_ceiling_threshold(inputs)
    ceiling_ok = ceiling_threshold is None or inputs.indoor <= ceiling_threshold

    return (
        inputs.outdoor < inputs.indoor - inputs.hysteresis
        and inputs.indoor > comfort_heat
        and inputs.outdoor < threshold
        and ceiling_ok
    )
