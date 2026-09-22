"""Pure decision core for the nat-vent active-session exit chain (Issue #608,
Block 5 / epic #594, Phase 2).

Extends the "architecture-reset" functional-core methodology proven by Issue
#441 to the one part of the nat-vent lifecycle that extraction never reached:
the priority-ordered chain of exit conditions inside
``check_natural_vent_conditions()`` that decides WHY an active session ends
(comfort-floor, away-mode ceiling, outdoor-rise, ceiling-threshold — in that
order, first match wins). The entry gate (``nat_vent_gate.py``) and the
reactivation lockout (``nat_vent_reactivation_lockout.py``) were already
pure; this closes the exit side.

Deliberately narrow scope, matching ``nat_vent_lifecycle-spec.md``'s own
documented finding: several OTHER exit paths (the RF-timer-boundary settle
reconcile clear, the reconcile "fan confirmed off" clear, the bedtime-handler
shutdowns) mutate the flags directly and are NOT part of this priority chain
— they're not reached from ``check_natural_vent_conditions()`` at all, so
folding them in here would silently change what this function models. Scope
is exactly the 4 checks inside ``check_natural_vent_conditions()`` guarded by
``if self._natural_vent_active:`` in the code this replaces.

Reuses ``fan_thermostat_decision.resolve_hard_exit_floor()`` (already pure,
already the single source of truth for the comfort-floor threshold per Issue
#456) rather than re-deriving it — composing existing pure functions instead
of duplicating them.

Validated via differential replay against the real production method — see
``tools/sim_harness/differential.py`` and ``tests/test_nat_vent_exit.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .fan_thermostat_decision import is_outdoor_rise_exit, resolve_hard_exit_floor

OCCUPANCY_AWAY = "away"


class NatVentExitReason(Enum):
    """The exit reasons check_natural_vent_conditions() checks, in priority
    order — first match wins. NONE means the session should continue."""

    NONE = "none"
    MANUAL_OVERRIDE_CONFLICT = "manual_override_conflict"
    COMFORT_FLOOR = "comfort_floor"
    AWAY_CEILING = "away_ceiling"
    OUTDOOR_RISE = "outdoor_rise"
    CEILING_THRESHOLD = "ceiling_threshold"


@dataclass(frozen=True)
class NatVentExitDecision:
    """The exit reason, plus the few computed values the shell's event payload
    needs (avoids re-deriving vent_floor/time_to_floor a second time in
    automation.py — the pure function computes them once, the shell just
    reads them off the decision)."""

    reason: NatVentExitReason
    vent_floor: float | None = None


@dataclass(frozen=True)
class NatVentExitInputs:
    """Every input the exit chain may read — explicit, nothing hidden.

    Field-by-field correspondence to the real code (all reads inside
    check_natural_vent_conditions(), only reached while
    self._natural_vent_active is True):
      indoor, outdoor        -> self._get_indoor_temp_f() / self._last_outdoor_temp
      comfort_heat_raw       -> config comfort_heat
      sleep_heat             -> config CONF_SLEEP_HEAT, falls back to comfort_heat_raw
      in_sleep_window        -> replaces _in_sleep_window(dt_util.now(), config)
      hysteresis             -> config CONF_NAT_VENT_HYSTERESIS_F
      comfort_cool           -> config comfort_cool
      nat_vent_delta         -> config CONF_NATURAL_VENT_DELTA
      occupancy_mode         -> self._occupancy_mode
      manual_override_active -> self._manual_override_active (Issue #714)
      manual_override_mode   -> self._manual_override_mode (Issue #714)
    """

    indoor: float | None
    outdoor: float | None
    comfort_heat_raw: float
    sleep_heat: float
    in_sleep_window: bool
    hysteresis: float
    comfort_cool: float
    nat_vent_delta: float
    occupancy_mode: str
    manual_override_active: bool
    manual_override_mode: str | None


def decide_nat_vent_exit(inputs: NatVentExitInputs) -> NatVentExitDecision:
    """Pure reimplementation of check_natural_vent_conditions()'s active-session
    exit chain (automation.py ~L3134-3331). Same 4 checks, same priority order,
    same boundary semantics as production — exists to be differentially
    validated against the real method, not to introduce new behavior.

    Callers must only invoke this while the session is active — mirrors every
    real call site's own ``if self._natural_vent_active:`` guard, which this
    function does not re-check (the flag itself isn't one of its inputs).
    """
    # 0. Manual override conflict (Issue #714) — checked before every other
    # reason. A manual override to an active HVAC mode (heat/cool/heat_cool)
    # structurally conflicts with WHF/nat-vent (Issue #392's whole premise) —
    # this must win over every other condition, including comfort-floor,
    # since the user's own thermostat choice is not something the automation
    # should keep silently working around.
    if inputs.manual_override_active and inputs.manual_override_mode not in (None, "off"):
        return NatVentExitDecision(reason=NatVentExitReason.MANUAL_OVERRIDE_CONFLICT)

    # 1. Comfort-floor exit (Issue #99/#456).
    vent_floor = resolve_hard_exit_floor(
        comfort_heat_raw=inputs.comfort_heat_raw,
        sleep_heat=inputs.sleep_heat,
        in_sleep_window=inputs.in_sleep_window,
        hysteresis=inputs.hysteresis,
    )
    if inputs.indoor is not None and inputs.indoor <= vent_floor:
        return NatVentExitDecision(reason=NatVentExitReason.COMFORT_FLOOR, vent_floor=vent_floor)

    # 2. Away-mode ceiling exit (Priority 2b).
    if inputs.occupancy_mode == OCCUPANCY_AWAY and inputs.indoor is not None and inputs.indoor >= inputs.comfort_cool:
        return NatVentExitDecision(reason=NatVentExitReason.AWAY_CEILING)

    # 3. Outdoor-rise exit (Issue #115) — airflow reversed.
    # Delegates to is_outdoor_rise_exit(), the single source of truth shared
    # with fan_thermostat_decision.py's Check 1 (Issue #690). BEHAVIOR CHANGE:
    # this boundary was previously strict (>), disagreeing with Check 1's
    # non-strict (>=) at exact outdoor==indoor equality — the fast-loop path
    # would stop nat-vent one tick before this slow-loop path did. Now
    # non-strict to match: at equality, no cooling benefit remains, so this
    # exit now also fires immediately rather than waiting for the next check
    # (comfort-floor/ceiling-threshold) to catch it.
    if is_outdoor_rise_exit(indoor=inputs.indoor, outdoor=inputs.outdoor):
        return NatVentExitDecision(reason=NatVentExitReason.OUTDOOR_RISE)

    # 4. Ceiling-threshold exit — outdoor too warm.
    threshold = inputs.comfort_cool + inputs.nat_vent_delta
    if inputs.outdoor is not None and inputs.outdoor > threshold:
        return NatVentExitDecision(reason=NatVentExitReason.CEILING_THRESHOLD)

    return NatVentExitDecision(reason=NatVentExitReason.NONE)
