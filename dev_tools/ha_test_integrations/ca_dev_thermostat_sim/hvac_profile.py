"""HVAC equipment profile for CA Dev Thermostat Sim: rates, reported precision, sensor lag, blower and coast.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Pure Python, no Home Assistant import and no relative import, so the simulator's plain-script tests
(``test_hvac_profile.py``) import and exercise THIS code directly instead of a hand-transcribed copy. climate.py,
number.py, select.py and config_flow.py all read the presets and option values from here.

Why it exists (owner, 2026-11-12): the simulated zones could never teach Climate Advisor's cycle model a heating or
cooling rate. They reported whole degrees, each maintenance run moved the room about 0.6 F, and every zone had the
same small system. "A normal HVAC should be sized to manage 10-15 degrees F per hour, but lower rates like 3-6F are
possible during extreme weather"; an undersized system may manage only 1-3 F/h.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

# --------------------------------------------------------------------------- rate presets
# Gross equipment capacity in F/h (the ODE's k_active). The room's NET rise is this minus the envelope loss,
# |k_passive| * (indoor - outdoor): at the default k_passive -0.15 and a 20 F indoor-outdoor gap that loss is 3 F/h.
RATE_PRESET_UNDERSIZED = "undersized"
RATE_PRESET_EXTREME_WEATHER = "extreme_weather"
RATE_PRESET_NORMAL = "normal"
RATE_PRESET_OVERSIZED = "oversized"
RATE_PRESET_CUSTOM = "custom"
RATE_PRESETS: dict[str, float] = {
    RATE_PRESET_UNDERSIZED: 2.0,
    RATE_PRESET_EXTREME_WEATHER: 5.0,
    RATE_PRESET_NORMAL: 12.0,
    RATE_PRESET_OVERSIZED: 18.0,
}
RATE_PRESET_OPTIONS: tuple[str, ...] = (*RATE_PRESETS, RATE_PRESET_CUSTOM)
# Unset preset = custom = use the entry's own k_active_heat / k_active_cool, so entries saved before presets existed
# load exactly as before.
DEFAULT_RATE_PRESET = RATE_PRESET_CUSTOM
RATE_MIN_F_PER_H = 0.1
RATE_MAX_F_PER_H = 30.0


def resolve_rate(preset: str | None, k_active: float, *, cooling: bool) -> float:
    """The signed k_active to simulate: the preset's rate when one is chosen, else the entry's own ``k_active``.

    Heating is positive, cooling negative (the sign k_active_cool has always had). An unknown or missing preset is
    "custom".
    """
    if preset in RATE_PRESETS:
        rate = RATE_PRESETS[preset]
        return -rate if cooling else rate
    return float(k_active)


def preset_for_rate(rate_f_per_h: float) -> str:
    """The preset whose rate is ``|rate_f_per_h|``, else ``custom``."""
    magnitude = abs(float(rate_f_per_h))
    for name, value in RATE_PRESETS.items():
        if math.isclose(magnitude, value, abs_tol=1e-9):
            return name
    return RATE_PRESET_CUSTOM


# --------------------------------------------------------------------------- reported precision
PRECISION_DEFAULT = "default"  # Home Assistant's own: whole degrees on a Fahrenheit system, tenths on Celsius
PRECISION_WHOLE = "whole"
PRECISION_HALF = "half"
PRECISION_TENTH = "tenth"
PRECISION_VALUES: dict[str, float | None] = {
    PRECISION_DEFAULT: None,
    PRECISION_WHOLE: 1.0,
    PRECISION_HALF: 0.5,
    PRECISION_TENTH: 0.1,
}
PRECISION_OPTIONS: tuple[str, ...] = tuple(PRECISION_VALUES)
DEFAULT_PRECISION = PRECISION_DEFAULT


def precision_value(option: str | None) -> float | None:
    """The climate entity's ``precision`` for an option; None = leave Home Assistant's default."""
    return PRECISION_VALUES.get(option or PRECISION_DEFAULT)


def round_to_precision(value: float, precision: float) -> float:
    """Round like Home Assistant's display_temp (to the nearest multiple of ``precision``); for tests and docs."""
    if precision == 1.0:
        return float(round(value))
    if precision == 0.5:
        return round(value * 2) / 2.0
    return round(value, 1)


# --------------------------------------------------------------------------- sensor lag
DEFAULT_SENSOR_LAG_MIN = 0.0  # 0 = the reading IS the simulated air, as before
SENSOR_LAG_MAX_MIN = 60.0
DEFAULT_EDGE_REPORT_DELAY_S = 0.0  # 0 = the thermostat reports its on/off edges as they happen
EDGE_REPORT_DELAY_MAX_S = 600.0


def lag_step(reading: float | None, air: float | None, dt_hours: float, lag_minutes: float) -> float | None:
    """One step of a first-order lag from the simulated air to the reported reading.

    ``reading += (air - reading) * (1 - exp(-dt / tau))`` with ``tau = lag_minutes``. A step input therefore reaches
    63 % after one time constant and 95 % after three. ``lag_minutes <= 0`` or no previous reading returns the air.
    """
    if air is None:
        return reading
    if lag_minutes <= 0 or reading is None:
        return air
    if dt_hours <= 0:
        return reading
    alpha = 1.0 - math.exp(-(dt_hours * 60.0) / lag_minutes)
    return reading + (air - reading) * alpha


# --------------------------------------------------------------------------- blower and coast
PHASE_OFF = "off"
PHASE_LEAD_IN = "lead_in"  # blower runs, no heating/cooling yet (hvac_action: fan)
PHASE_RUNNING = "running"  # burner / compressor on (hvac_action: heating / cooling)
PHASE_OVERRUN = "overrun"  # blower keeps running after the stop (hvac_action: fan)
BLOWER_MAX_MIN = 30.0
COAST_RATE_MAX_F_PER_H = 30.0
COAST_TAU_MAX_MIN = 30.0


@dataclass(frozen=True)
class BlowerConfig:
    """One direction's (heat or cool) blower timing and after-stop coast. All zero = no blower phases, no coast."""

    lead_in_min: float = 0.0
    overrun_min: float = 0.0
    coast_rate_f_per_h: float = 0.0  # magnitude; heat coast warms, cool coast cools
    coast_tau_min: float = 0.0


@dataclass
class EquipmentState:
    """Where the equipment is in its cycle, and any coast still decaying from the last stop."""

    phase: str = PHASE_OFF
    direction: str | None = None  # "heat" / "cool" of the current or last run
    phase_until: datetime | None = None
    coast_direction: str | None = None
    coast_start: datetime | None = None
    coast_rate_f_per_h: float = 0.0
    coast_tau_min: float = 0.0


def _start_coast(state: EquipmentState, direction: str | None, cfg: BlowerConfig, now: datetime) -> None:
    if direction is not None and cfg.coast_rate_f_per_h > 0 and cfg.coast_tau_min > 0:
        state.coast_direction = direction
        state.coast_start = now
        state.coast_rate_f_per_h = float(cfg.coast_rate_f_per_h)
        state.coast_tau_min = float(cfg.coast_tau_min)


def stop_equipment(state: EquipmentState, cfg: BlowerConfig, now: datetime) -> EquipmentState:
    """The call ends (thermostat satisfied, or its mode changed): running goes to overrun (or off) and starts coast.

    A lead-in that never reached running ends with the blower off and no coast (nothing was heated or cooled).
    """
    if state.phase == PHASE_RUNNING:
        _start_coast(state, state.direction, cfg, now)
        if cfg.overrun_min > 0:
            state.phase = PHASE_OVERRUN
            state.phase_until = _plus_minutes(now, cfg.overrun_min)
        else:
            state.phase, state.phase_until = PHASE_OFF, None
    elif state.phase == PHASE_LEAD_IN:
        state.phase, state.phase_until = PHASE_OFF, None
    return state


def advance_equipment(
    state: EquipmentState,
    *,
    call_on: bool,
    direction: str | None,
    now: datetime,
    cfg: BlowerConfig,
    cfg_last: BlowerConfig | None = None,
) -> EquipmentState:
    """Advance the blower phase for one tick.

    ``call_on`` is the thermostat's call (the compressor state machine's output); ``direction`` the current mode's
    ``"heat"``/``"cool"`` (None when off); ``cfg`` that direction's timing; ``cfg_last`` the timing of the direction
    the last run used (defaults to ``cfg``), for the overrun and coast that follow a stop.

    Sequence for one call: off -> [lead_in] -> running -> [overrun] -> off. A call that returns during overrun goes
    straight to running (the blower is already moving air). Starting a run clears any coast still decaying.
    """
    last = cfg_last or cfg
    if call_on and direction is not None:
        if state.phase in (PHASE_OFF, PHASE_OVERRUN) or state.direction != direction:
            if state.phase == PHASE_OFF and cfg.lead_in_min > 0:
                state.phase, state.phase_until = PHASE_LEAD_IN, _plus_minutes(now, cfg.lead_in_min)
            else:
                state.phase, state.phase_until = PHASE_RUNNING, None
            state.direction = direction
            state.coast_direction, state.coast_start = None, None
        if state.phase == PHASE_LEAD_IN and state.phase_until is not None and now >= state.phase_until:
            state.phase, state.phase_until = PHASE_RUNNING, None
        return state

    if state.phase in (PHASE_RUNNING, PHASE_LEAD_IN):
        stop_equipment(state, last, now)
    if state.phase == PHASE_OVERRUN and state.phase_until is not None and now >= state.phase_until:
        state.phase, state.phase_until = PHASE_OFF, None
    return state


def coast_average_f_per_h(state: EquipmentState, now: datetime, dt_hours: float) -> float:
    """Signed average coast power (F/h) for the tick interval that the tick at ``now`` simulates.

    The coast decays as ``rate * exp(-age / tau)``. ``now`` is the START of the simulated interval: a tick at ``t``
    advances the room over ``[t - dt, t]`` with the equipment as it was, then decides (the stop at ``t`` is reported at
    ``t``, before any of its coast), so the tick after the stop asks for ``now = stop`` and simulates ages
    ``[0, dt]``, the next one ``[dt, 2 dt]``, and so on. Returns 0 without a coast, and for an interval that ends at or
    before the coast began; positive warms, negative cools.
    """
    if state.coast_start is None or state.coast_rate_f_per_h <= 0 or state.coast_tau_min <= 0 or dt_hours <= 0:
        return 0.0
    if (state.coast_start - now).total_seconds() / 3600.0 >= dt_hours - 1e-9:
        return 0.0  # the whole interval is before the stop
    tau_h = state.coast_tau_min / 60.0
    age0 = max(0.0, (now - state.coast_start).total_seconds() / 3600.0)
    if age0 > 10 * tau_h:  # spent (under 0.005 % left): drop it so it stops showing in the debug attributes
        state.coast_direction, state.coast_start = None, None
        return 0.0
    age1 = age0 + dt_hours
    avg = state.coast_rate_f_per_h * tau_h * (math.exp(-age0 / tau_h) - math.exp(-age1 / tau_h)) / dt_hours
    return avg if state.coast_direction == "heat" else -avg


def coast_total_f(rate_f_per_h: float, tau_min: float) -> float:
    """Total coast delivered after a stop, in F of room temperature before envelope loss: ``rate * tau``."""
    return rate_f_per_h * tau_min / 60.0


def _plus_minutes(now: datetime, minutes: float) -> datetime:
    return now + timedelta(minutes=float(minutes))


# --------------------------------------------------------------------------- learning arithmetic (README table)
def rise_per_cycle_f(start_distance_f: float, stop_distance_f: float) -> float:
    """Room movement per maintenance run: start distance minus stop distance.

    Both are measured on the side of the target the room starts from (heat: below it). The defaults start 1.5 F
    below and stop 0.9 F below (off_offset -0.9), so each run moves the room 1.5 - 0.9 = 0.6 F.
    """
    return start_distance_f - stop_distance_f


def run_minutes(rise_f: float, net_rate_f_per_h: float) -> float:
    """Minutes the equipment runs to move the room ``rise_f`` at a net rate (capacity minus envelope loss)."""
    if net_rate_f_per_h <= 0:
        return math.inf
    return rise_f / net_rate_f_per_h * 60.0


def reported_action(history: list[tuple[datetime, str]], now: datetime, delay_s: float, current: str) -> str:
    """The hvac_action a thermostat reporting ``delay_s`` late shows at ``now`` (``history``: ``(time, action)`` of
    every change, oldest first). ``delay_s`` 0 (the default) is the current action."""
    if delay_s <= 0 or not history:
        return current
    cutoff = now - timedelta(seconds=float(delay_s))
    shown = history[0][1]
    for ts, action in history:
        if ts <= cutoff:
            shown = action
        else:
            break
    return shown
