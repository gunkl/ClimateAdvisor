"""Pure decision core for the tick-level fan thermostatic stop check (architecture-reset Step 2).

Second proof slice, expanding outward from the reactivation gate
(``nat_vent_gate.py``) per the roadmap's Step 2. Pure reimplementation of
``automation.py``'s ``fan_thermostat_check()`` Check 1 (free-cooling-direction
stop, Issue #327) and Check 2 (cooled-to-target stop, sleep-aware per Issue
#402) — the tick-level safety check that fires on every indoor/outdoor
temperature change plus a 5-min backstop, historically the site of #327 and
#402's original bugs.

Scope: unlike the boolean reactivation gate, this decision has FOUR real
outcomes (keep / stop-via-nat-vent-exit / stop-deactivate / stop-cooled-to-floor),
each with a different action shape in production — captured here as an enum,
the smallest step toward the plan's `DesiredState` design without building the
full general schema yet (that's Step 3). This function does NOT decide whether
the fan is active or overridden — those are cheap precondition checks the shell
keeps, mirroring `_nat_vent_may_reactivate()`'s own precedent ("callers keep
their own additional guards; this function returns only the shared gate").

Every field on ``FanThermostatInputs`` traces to a real production read; in
particular ``in_sleep_window`` replaces the real code's internal
``dt_util.now()`` read, exactly as in ``nat_vent_gate.py``.

Validated via shadow + substitution differential replay against the real
production method — see ``tools/fan_thermostat_decision_diff.py`` and the
Step-2 status report.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FanThermostatOutcome(Enum):
    """The four real outcomes fan_thermostat_check() can produce."""

    KEEP = "keep"
    STOP_VIA_NAT_VENT_EXIT = "stop_via_nat_vent_exit"
    STOP_DEACTIVATE = "stop_deactivate"
    STOP_COOLED_TO_FLOOR = "stop_cooled_to_floor"


@dataclass(frozen=True)
class FanThermostatInputs:
    """Every input the tick-level fan stop check may read — explicit, nothing hidden.

    Field-by-field correspondence to the real code:
      indoor, outdoor      -> fan_thermostat_check(indoor=, outdoor=)
      comfort_heat_raw     -> config comfort_heat (Check 2's awake-branch floor, unmodified)
      sleep_heat           -> config CONF_SLEEP_HEAT, falls back to comfort_heat_raw
      in_sleep_window      -> replaces _in_sleep_window(dt_util.now(), config)
      hysteresis           -> config CONF_NAT_VENT_HYSTERESIS_F — used ONLY in Check 2's
                               sleep-branch floor (_sleep_heat - hysteresis); Check 1's
                               direction-reversal stop deliberately uses NO hysteresis
                               (see the real code's comment: subtracting it there would
                               kill free cooling ~1F early)
      natural_vent_active  -> self._natural_vent_active — determines whether Check 1's
                               stop routes through the nat-vent exit path or a plain
                               deactivate
    """

    indoor: float | None
    outdoor: float | None
    comfort_heat_raw: float
    sleep_heat: float
    in_sleep_window: bool
    hysteresis: float
    natural_vent_active: bool


def _resolve_vent_floor(inputs: FanThermostatInputs) -> float:
    """Pure reimplementation of Check 2's floor resolution.

    Deliberately asymmetric, matching production exactly: the sleep-window
    branch subtracts hysteresis from sleep_heat; the awake branch does NOT
    subtract hysteresis from comfort_heat_raw. This is not a simplification
    opportunity — it's what the real code does, and the two floors are allowed
    to have different boundary shapes.
    """
    if inputs.in_sleep_window:
        return inputs.sleep_heat - inputs.hysteresis
    return inputs.comfort_heat_raw


def decide_fan_thermostat_check(inputs: FanThermostatInputs) -> FanThermostatOutcome:
    """Pure reimplementation of fan_thermostat_check()'s Check 1 + Check 2.

    Preconditions (fan active, not overridden) are the shell's responsibility,
    not this function's — mirrors decide_nat_vent_gate()'s scoping.
    """
    # --- Check 1: free-cooling direction guard (Issue #327) ---
    # Non-strict >=, NO hysteresis — this boundary is deliberately different
    # from the reactivation gate's strict < with configurable hysteresis.
    if inputs.outdoor is not None and inputs.indoor is not None and inputs.outdoor >= inputs.indoor:
        if inputs.natural_vent_active:
            return FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT
        return FanThermostatOutcome.STOP_DEACTIVATE

    # --- Check 2: cooled to target, sleep-aware (Issue #402) ---
    vent_floor = _resolve_vent_floor(inputs)
    if inputs.indoor is not None and inputs.indoor <= vent_floor:
        return FanThermostatOutcome.STOP_COOLED_TO_FLOOR

    return FanThermostatOutcome.KEEP
