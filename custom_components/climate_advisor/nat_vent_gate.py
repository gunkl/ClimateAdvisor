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

The savings margin is imported from ``const.py`` (``CEILING_ESCALATION_SAVINGS_MARGIN_F``), the single
source -- ``const.py`` is a leaf (it imports only ``typing``), so this module stays free of any
engine/coordinator import while no longer carrying its own copy of the number (Issue #1093 review).

Validated via differential replay against the real production method — see
``tools/nat_vent_gate_diff.py`` and the Step-2 status report for results.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import CEILING_ESCALATION_SAVINGS_MARGIN_F, MIN_VIABLE_NAT_VENT_HOURS

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


def resolve_comfort_heat(comfort_heat_raw: float, sleep_heat: float, in_sleep_window: bool) -> float:
    """Pure reimplementation of _nat_vent_reactivation_floor() (Issue #417).

    Standalone (not NatVentGateInputs-shaped) so callers that only need the
    comfort-floor resolution — not the full activation gate — can use it
    without constructing a NatVentGateInputs (Issue #535).
    """
    return sleep_heat if in_sleep_window else comfort_heat_raw


def _resolve_comfort_heat(inputs: NatVentGateInputs) -> float:
    return resolve_comfort_heat(inputs.comfort_heat_raw, inputs.sleep_heat, inputs.in_sleep_window)


def resolve_comfort_cool(comfort_cool_raw: float, sleep_cool: float, in_sleep_window: bool) -> float:
    """Cool-side counterpart to :func:`resolve_comfort_heat` (Issue #786).

    Standalone for the same reason: callers that only need the comfort-ceiling resolution
    (e.g. the TOU scheduler's pre-conditioning heat-banking target) can use it without
    constructing a NatVentGateInputs.
    """
    return sleep_cool if in_sleep_window else comfort_cool_raw


def _resolve_ceiling_threshold(inputs: NatVentGateInputs) -> float | None:
    """Pure reimplementation of _ceiling_threshold()."""
    if inputs.fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return None
    if inputs.aggressive_savings:
        return inputs.comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F
    return inputs.comfort_cool


def explain_nat_vent_gate(inputs: NatVentGateInputs) -> list[str]:
    """Return why the reactivation gate rejects these inputs (empty list = gate passes).

    The single implementation of the 4-part gate (Issue #1095): ``decide_nat_vent_gate()``
    is defined as ``not explain_nat_vent_gate()``, so a logged rejection reason can never
    drift from the real decision. Reason slugs mirror the exit side's ``NatVentExitReason``
    style (short snake_case). Same boundary semantics as production (strict '<', '<=' on
    the ceiling check).
    """
    if inputs.outdoor is None or inputs.indoor is None:
        return ["temperature_unavailable"]

    comfort_heat = _resolve_comfort_heat(inputs)
    threshold = inputs.comfort_cool + inputs.nat_vent_delta
    ceiling_threshold = _resolve_ceiling_threshold(inputs)

    failures: list[str] = []
    if not inputs.outdoor < inputs.indoor - inputs.hysteresis:
        failures.append("outdoor_not_cooler_than_indoor")
    if not inputs.indoor > comfort_heat:
        failures.append("indoor_at_or_below_comfort_floor")
    if not inputs.outdoor < threshold:
        failures.append("outdoor_above_threshold")
    if ceiling_threshold is not None and not inputs.indoor <= ceiling_threshold:
        failures.append("indoor_above_ceiling")
    return failures


def decide_nat_vent_gate(inputs: NatVentGateInputs) -> bool:
    """Pure reimplementation of _nat_vent_may_reactivate() (Issue #411 Pass 4).

    Same 4-part gate, same boundary semantics (strict '<', '<=' on the ceiling
    check) as production — this function exists to be differentially validated
    against the real method, not to introduce new behavior. Implemented as
    ``not explain_nat_vent_gate()`` (Issue #1095) so there is one definition.
    """
    return not explain_nat_vent_gate(inputs)


# Minimum number of same-day outdoor-temp samples required before trusting
# "today's peak" (Issue #540). Below this, _outdoor_temp_history may be thin
# after a restart that crossed local midnight (see coordinator.py's
# async_restore_state day-boundary gate) — failing closed avoids a false
# "already past peak" read from 0-1 samples.
_MIN_PEAK_SAMPLE_COUNT = 3


@dataclass(frozen=True)
class NatVentSoftStartGateInputs:
    """Inputs for the nat-vent soft-start sub-gate (Issue #540, scoped from #533).

    Distinct from, and does not modify, ``NatVentGateInputs``/``decide_nat_vent_gate()``.
    Soft-start is a separate WHF-purge/comfort entry path that fires only in the gap
    before the full bulk-cooling gate would already apply.

    Field-by-field correspondence to the real code:
      outdoor, indoor          -> same live reads as NatVentGateInputs
      comfort_heat             -> already sleep-resolved by the caller (mirrors
                                   NatVentGateInputs.comfort_heat_raw/sleep_heat resolution)
      comfort_cool             -> raw config comfort_cool
      fan_mode                 -> config CONF_FAN_MODE; soft-start is WHF-only
                                   (whole_house_fan/both) — the attic-purge claim doesn't
                                   apply to HVAC-only fan archetypes
      outdoor_today_peak       -> max of coordinator._outdoor_temp_history for today,
                                   plumbed to the caller via
                                   automation_engine._outdoor_temp_today_peak
      outdoor_sample_count     -> len(coordinator._outdoor_temp_history) for today, plumbed
                                   via automation_engine._outdoor_temp_today_sample_count
      peak_decline_margin      -> PEAK_DECLINE_MARGIN_F (const.py), degrees below today's
                                   observed peak required before calling it "declining"
      full_gate_active         -> the caller's own decide_nat_vent_gate(...) result for the
                                   same inputs — soft-start stands down once the full gate
                                   would already apply, so the two gates never compete for
                                   the same activation
    """

    outdoor: float | None
    indoor: float | None
    comfort_heat: float
    comfort_cool: float
    fan_mode: str
    outdoor_today_peak: float | None
    outdoor_sample_count: int
    peak_decline_margin: float
    full_gate_active: bool


def explain_nat_vent_soft_start_gate(inputs: NatVentSoftStartGateInputs) -> list[str]:
    """Return why the soft-start sub-gate rejects these inputs (empty list = passes).

    Single implementation (Issue #1095); ``decide_nat_vent_soft_start_gate()`` is
    ``not explain_nat_vent_soft_start_gate()``. Early-exit preconditions return one reason,
    mirroring the original short-circuit order.
    """
    if inputs.outdoor is None or inputs.indoor is None:
        return ["temperature_unavailable"]
    if inputs.full_gate_active:
        return ["full_gate_already_applies"]
    if inputs.fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return ["fan_mode_not_whole_house"]
    if inputs.outdoor_sample_count < _MIN_PEAK_SAMPLE_COUNT or inputs.outdoor_today_peak is None:
        return ["insufficient_peak_samples"]

    past_peak = inputs.outdoor < inputs.outdoor_today_peak - inputs.peak_decline_margin

    failures: list[str] = []
    if not past_peak:
        failures.append("outdoor_not_past_peak")
    if not inputs.indoor > inputs.comfort_heat:
        failures.append("indoor_at_or_below_comfort_floor")
    if not inputs.indoor > inputs.comfort_cool:
        failures.append("indoor_at_or_below_comfort_ceiling")
    if not inputs.outdoor <= inputs.indoor:
        failures.append("outdoor_warmer_than_indoor")
    return failures


def decide_nat_vent_soft_start_gate(inputs: NatVentSoftStartGateInputs) -> bool:
    """Should nat-vent soft-start (purge/comfort sub-mode) activate right now?

    Unlike ``decide_nat_vent_gate()``, this does not require outdoor to be below
    indoor by any margin — only that outdoor has reached parity (``<=``) with
    indoor, and that today's outdoor temperature is confirmed past its peak and
    declining. See Issue #540 (scoped from #533's soft-start hypothesis).
    """
    return not explain_nat_vent_soft_start_gate(inputs)


# ---------------------------------------------------------------------------
# Start-decision rules shared by every site that can START nat-vent (Issue #1142)
# ---------------------------------------------------------------------------

NO_WORKING_FAN = "no_working_fan"
FORECAST_PEAK_ABOVE_THRESHOLD = "forecast_peak_above_threshold"
FLOOR_IMMINENT = "floor_imminent"


def fan_available_for_nat_vent(fan_mode: str) -> bool:
    """Whether Climate Advisor has a fan to run for nat-vent at all (Issue #1140).

    Owner decision (#1142 D1): with no working fan an open window is just an open window --
    HVAC is paused like any other (household rule 3). A disabled ``fan_mode`` owns no fan, so
    nat-vent may not start. (A configured fan that refuses the command at run time is the
    ``SUPPRESSED`` result, handled by the caller.)
    """
    return fan_mode != FAN_MODE_DISABLED


@dataclass(frozen=True)
class NatVentStartVetoInputs:
    """Inputs for the forecast-peak and thermal-floor start guards (Issue #1138, moved out of
    ``handle_door_window_open()``'s inline code so every start site shares one definition).

      comfort_heat       -> the caller's sleep-resolved reactivation floor
      forecast_peak_2h   -> highest hourly-forecast temperature in the next 2 hours (None = no
                            forecast data; the caller resolves the clock and the parsing)
      thermal_confidence -> ``thermal_model["confidence"]`` ("none" when there is no model)
      thermal_k_passive  -> ``thermal_model["k_passive"]``
    """

    outdoor: float | None
    indoor: float | None
    comfort_heat: float
    comfort_cool: float
    nat_vent_delta: float
    forecast_peak_2h: float | None
    thermal_confidence: str
    thermal_k_passive: float | None


@dataclass(frozen=True)
class NatVentStartVeto:
    """Why a start was vetoed, with the numbers the Activity Report events carry."""

    reason: str
    forecast_peak: float | None = None
    threshold: float | None = None
    time_to_floor_hr: float | None = None
    k_passive: float | None = None
    indoor: float | None = None
    comfort_heat: float | None = None


def decide_nat_vent_start_veto(inputs: NatVentStartVetoInputs) -> NatVentStartVeto | None:
    """Pure forecast-peak then thermal-floor start guards (the former inline Phase 2 guards).

    Guard 1: a hourly-forecast peak within 2 h above ``comfort_cool + nat_vent_delta`` means the
    window of useful outdoor air is about to close. Guard 2: a medium/high-confidence thermal
    model predicting the indoor comfort floor within ``MIN_VIABLE_NAT_VENT_HOURS`` means a session
    would end almost immediately. Guard 1 wins when both hold. None = no veto.
    """
    threshold = inputs.comfort_cool + inputs.nat_vent_delta
    if inputs.forecast_peak_2h is not None and inputs.forecast_peak_2h > threshold:
        return NatVentStartVeto(
            FORECAST_PEAK_ABOVE_THRESHOLD, forecast_peak=inputs.forecast_peak_2h, threshold=threshold
        )
    if (
        inputs.thermal_confidence in ("medium", "high")
        and inputs.thermal_k_passive is not None
        and inputs.thermal_k_passive < 0
        and inputs.indoor is not None
        and inputs.outdoor is not None
    ):
        passive_rate = inputs.thermal_k_passive * (inputs.indoor - inputs.outdoor)  # F/hr, negative
        if passive_rate < 0:
            time_to_floor = (inputs.indoor - inputs.comfort_heat) / abs(passive_rate)
            if time_to_floor < MIN_VIABLE_NAT_VENT_HOURS:
                return NatVentStartVeto(
                    FLOOR_IMMINENT,
                    time_to_floor_hr=time_to_floor,
                    k_passive=inputs.thermal_k_passive,
                    indoor=inputs.indoor,
                    comfort_heat=inputs.comfort_heat,
                )
    return None
