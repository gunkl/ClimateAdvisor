"""Pure decision core for the whole-house-fan (WHF) safety enforcer (Issue #1144).

Two owner rules, both about the fan's physical state, both enforced from ground truth only:

1. **The WHF never runs on a sealed house.** If every configured door/window sensor reads
   closed, a running WHF must be off within 30 s worst case, even under a manual or RF
   override. (Zero configured sensors: sealed-ness cannot be judged, the rule does not apply.)
2. **WHF and HVAC are mutually exclusive.** HVAC counts as "on" when its mode is heat, cool,
   heat_cool (or auto). When both run, the occupant's NEWEST action wins: a newer HVAC-on
   stops the fan, a newer fan-on suppresses HVAC. Climate Advisor's own commands are never
   occupant actions (the caller passes ``None`` for them).

Templated on ``fan_toggle_rate_limit.py``: a leaf module with no logging, no HA imports and no
state. Every field on ``WhfSafetyInputs`` corresponds to a value the shell reads; the shell
(``AutomationEngine._enforce_whf_safety_locked``) owns commands, logging, timers and alerts.

Sensor handling (owner decision 2): a sensor that is unavailable/unknown counts as OPEN for the
first 60 s (benefit of the doubt, so a restart blip never stops a legitimately running fan) and as
CLOSED afterwards (fail-safe toward stopping), and the shell alerts the occupant about it.

Settle (owner decision 3): a fan that came on less than 10 s ago is not stopped yet, so a person
who opens a window and presses the fan in one motion is not fought. The total stays within 30 s.

Observed incident (2026-10-30 21:32): HomeKit turned the WHF on while the house had been sealed
for an hour; the fan ran about 55 minutes because the only sealed-house checks were event driven
and none fired. This module has no event dependency: the shell can ask it at any time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# Fan modes in which a physical whole-house fan exists (mirrors const.FAN_MODE_WHOLE_HOUSE / _BOTH;
# kept literal so this leaf has no package imports).
_WHF_FAN_MODES = frozenset({"whole_house_fan", "both"})

# HVAC modes that count as "HVAC on" for rule 2 (armed, not only actively running).
WHF_ACTIVE_HVAC_MODES = frozenset({"heat", "cool", "heat_cool", "auto"})

WHF_SETTLE_AFTER_ON_S = 10.0
WHF_SEALED_CONFIRM_S = 10.0
WHF_SENSOR_UNKNOWN_GRACE_S = 60.0
# An on-transition this close to Climate Advisor's own last real fan command is CA's, not the occupant's.
WHF_OWN_COMMAND_ECHO_S = 45.0


class WhfSafetyAction(Enum):
    NONE = "none"
    STOP_SEALED = "stop_sealed"
    STOP_MUTEX_FAN = "stop_mutex_fan"
    SUPPRESS_HVAC = "suppress_hvac"
    DEFER = "defer"


@dataclass(frozen=True)
class SensorReading:
    """One configured door/window sensor.

    is_open        -> the sensor reads open right now (polarity already applied; False when unreadable)
    unknown_for_s  -> None while the sensor is readable; otherwise seconds it has been
                      unavailable/unknown/missing
    stable_for_s   -> seconds since this sensor's reading last changed (None when not known)
    """

    is_open: bool
    unknown_for_s: float | None = None
    stable_for_s: float | None = None


@dataclass(frozen=True)
class WhfSafetySnapshot:
    """What the coordinator can read from Home Assistant right now (the ground-truth half of the inputs).

    fan_on        -> physical fan state, None when unknown
    fan_on_for_s  -> seconds since the physical fan entity last changed state (None when unknown)
    sensors       -> one reading per configured door/window sensor (groups already resolved)
    stale_names   -> display names of sensors unknown for longer than the grace period (for the alert)
    occupant_fan_on_at -> when the OCCUPANT turned the fan on, derived from the fan's physical on-time while
                    override detection is suppressed (startup coalescing); None otherwise. Climate Advisor's
                    own fan commands never produce it (owner decision 7).
    occupant_hvac_on_at -> the mirror: when the OCCUPANT turned HVAC on, from the thermostat mode's age, under the
                    same suppression and the same exclusion of Climate Advisor's own commands.
    """

    fan_on: bool | None
    fan_on_for_s: float | None
    sensors: tuple[SensorReading, ...]
    stale_names: tuple[str, ...] = ()
    occupant_fan_on_at: datetime | None = None
    occupant_hvac_on_at: datetime | None = None


@dataclass(frozen=True)
class WhfSafetyInputs:
    """Every input the safety check may read — ground truth, explicit, nothing hidden.

    fan_mode            -> config fan mode
    fan_on              -> the physical fan reading (True/False), or None when unknown (command-only
                           install, unavailable entity)
    fan_believed_on     -> Climate Advisor's own flags (_fan_active or _fan_override_active); used
                           ONLY when ``fan_on`` is None (preserves the pre-#1144 behaviour on installs
                           that cannot see the fan; they are unenforceable beyond that)
    fan_on_for_s        -> seconds since the fan physically came on (None when unknown)
    sensors             -> one reading per configured sensor (empty: rule 1 does not apply)
    sealed_confirmed    -> True when the caller is the debounced sensor-closed event (the debounce
                           already confirmed the close), so the sealed-for check is skipped
    thermostat_mode     -> the thermostat's current hvac mode string
    occupant_fan_on_at  -> when the occupant turned the fan on (None: not an occupant action)
    occupant_hvac_on_at -> when the occupant turned HVAC on (None: not an occupant action)
    whf_owns_hvac       -> HVAC is already suppressed for a WHF session
    """

    fan_mode: str
    fan_on: bool | None
    fan_believed_on: bool
    fan_on_for_s: float | None
    sensors: tuple[SensorReading, ...]
    sealed_confirmed: bool
    thermostat_mode: str | None
    occupant_fan_on_at: datetime | None
    occupant_hvac_on_at: datetime | None
    whf_owns_hvac: bool


@dataclass(frozen=True)
class WhfSafetyDecision:
    """The decision plus what the shell needs to act on it."""

    action: WhfSafetyAction
    reason: str
    retry_in_s: float | None = None
    stale_sensor_count: int = 0  # sensors counted closed because they were unknown for > 60 s
    sealed: bool = False


def house_is_sealed(sensors: tuple[SensorReading, ...]) -> bool:
    """The one definition of "sealed house" (rule 1): at least one sensor is configured and none counts as open.

    An unreadable sensor counts as open for ``WHF_SENSOR_UNKNOWN_GRACE_S`` and closed after. Zero configured
    sensors: sealed-ness cannot be judged, so this is False and rule 1 never applies. Shared by the enforcer's
    stop decision and Climate Advisor's own start gate so the two can never disagree.
    """
    if not sensors:
        return False
    return not any(
        (s.is_open if s.unknown_for_s is None else s.unknown_for_s <= WHF_SENSOR_UNKNOWN_GRACE_S) for s in sensors
    )


def decide_whf_safety(inputs: WhfSafetyInputs) -> WhfSafetyDecision:
    """Decide whether the running whole-house fan must be stopped, HVAC suppressed, or nothing.

    Precedence: not-a-WHF-install and fan-not-running exit first; then rule 1 (sealed house);
    then rule 2 (WHF vs HVAC, newest occupant action wins).
    """
    if inputs.fan_mode not in _WHF_FAN_MODES:
        return WhfSafetyDecision(WhfSafetyAction.NONE, "not a whole-house-fan install")

    running = inputs.fan_on if inputs.fan_on is not None else inputs.fan_believed_on
    if not running:
        return WhfSafetyDecision(WhfSafetyAction.NONE, "fan not running")

    stale = sum(
        1 for s in inputs.sensors if s.unknown_for_s is not None and s.unknown_for_s > WHF_SENSOR_UNKNOWN_GRACE_S
    )

    # ---- Rule 1: a sealed house -------------------------------------------------------------
    if house_is_sealed(inputs.sensors):
        retry: float | None = None
        if inputs.fan_on_for_s is not None and inputs.fan_on_for_s < WHF_SETTLE_AFTER_ON_S:
            retry = WHF_SETTLE_AFTER_ON_S - inputs.fan_on_for_s
        if not inputs.sealed_confirmed:
            stable = [s.stable_for_s for s in inputs.sensors if s.stable_for_s is not None]
            if stable and min(stable) < WHF_SEALED_CONFIRM_S:
                wait = WHF_SEALED_CONFIRM_S - min(stable)
                retry = max(retry or 0.0, wait)
        if retry is not None:
            return WhfSafetyDecision(
                WhfSafetyAction.DEFER,
                "sealed house — settling before stopping the fan",
                retry_in_s=retry,
                stale_sensor_count=stale,
                sealed=True,
            )
        return WhfSafetyDecision(
            WhfSafetyAction.STOP_SEALED,
            "whole-house fan running with every door/window closed",
            stale_sensor_count=stale,
            sealed=True,
        )

    # ---- Rule 2: WHF and HVAC are mutually exclusive ----------------------------------------
    if inputs.thermostat_mode in WHF_ACTIVE_HVAC_MODES:
        fan_t, hvac_t = inputs.occupant_fan_on_at, inputs.occupant_hvac_on_at
        if hvac_t is not None and (fan_t is None or hvac_t > fan_t):
            return WhfSafetyDecision(
                WhfSafetyAction.STOP_MUTEX_FAN,
                f"occupant turned HVAC on ({inputs.thermostat_mode}) after the fan — newest action wins",
                stale_sensor_count=stale,
            )
        if fan_t is not None and not inputs.whf_owns_hvac:
            return WhfSafetyDecision(
                WhfSafetyAction.SUPPRESS_HVAC,
                f"occupant turned the fan on after HVAC ({inputs.thermostat_mode}) — newest action wins",
                stale_sensor_count=stale,
            )

    return WhfSafetyDecision(WhfSafetyAction.NONE, "no conflict", stale_sensor_count=stale)
