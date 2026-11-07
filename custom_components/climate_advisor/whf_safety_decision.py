"""Pure decision core for the whole-house-fan (WHF) safety enforcer (Issue #1144).

Two owner rules, both about the fan's physical state, both enforced from ground truth only:

1. **The WHF never runs on a sealed house.** If every configured door/window sensor reads
   closed, a running WHF must be off within 30 s worst case, even under a manual or RF
   override. (Zero configured sensors: sealed-ness cannot be judged, the rule does not apply.)
2. **WHF and HVAC are mutually exclusive.** HVAC counts as "on" when its mode is heat, cool,
   heat_cool (or auto). When both run, the occupant's NEWEST action wins: a newer HVAC-on
   stops the fan, a newer fan-on suppresses HVAC. Climate Advisor's own commands are never
   occupant actions (the caller passes ``None`` for them).

   **Rule-2 state backstop (Issue #1288).** The press-based half above needs a stamp for the
   occupant's action, and a press can be missed (heat pressed 1-2 s after the fan, a door pause
   running). The backstop decides from device state alone: the fan reads physically on, the
   thermostat is in an active mode and reports heating/cooling, and both have been true together
   for ``WHF_RULE2_OVERLAP_CONFIRM_S``. Then one device is stopped: the fan when the thermostat
   entered its mode after the fan came on (the HVAC is the newer action), else the HVAC. It never
   fires while Climate Advisor's own stop or HVAC-off is still landing, and never for a fan whose
   state cannot be read.

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

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# Fan modes in which a physical whole-house fan exists (mirrors const.FAN_MODE_WHOLE_HOUSE / _BOTH;
# kept literal so this leaf has no package imports).
_WHF_FAN_MODES = frozenset({"whole_house_fan", "both"})

# HVAC modes that count as "HVAC on" for rule 2 (armed, not only actively running).
WHF_ACTIVE_HVAC_MODES = frozenset({"heat", "cool", "heat_cool", "auto"})

WHF_SETTLE_AFTER_ON_S = 10.0
# Issue #1288: the rule-2 state backstop waits until the fan and the compressor have read on together this long (a
# person switching one off as they switch the other on is not fought), and stays silent this long after Climate
# Advisor's own HVAC-off command (the thermostat's hvac_action lags the mode write). 10 s + the 15 s tick at worst
# + the command itself keeps the overlap under 30 s.
WHF_RULE2_OVERLAP_CONFIRM_S = 10.0
WHF_RULE2_OWN_OFF_QUIET_S = 10.0
# hvac_action values that mean the compressor/burner is running (mirrors hvac_action.HVAC_ACTION_COMPRESSOR_ACTIVE;
# kept literal so this leaf has no package imports).
_COMPRESSOR_ACTIVE_ACTIONS = frozenset({"heating", "cooling"})
WHF_SEALED_CONFIRM_S = 10.0
WHF_SENSOR_UNKNOWN_GRACE_S = 60.0
# Issue #1215: how long after Climate Advisor's own WHF stop command the physical reading is still ALLOWED to read
# "on". The fan is read through a power-detect helper that lags the command: 15.3 s and 16.0 s measured on the
# production install (15:06:45.9 -> 15:07:01.26, 15:12:35.02 -> 15:12:51.07), 15.5 s typical. 20 s is the 16.0 s worst
# case plus a 4 s margin; it is also inside Rule 1's 30 s bound, so a stop that really did not take is still retried
# in time for the next 30 s window. Inside it, "control entity off, physical on" is expected lag, never drift.
WHF_STOP_CONVERGE_WINDOW_S = 20.0
# The mirror for a start: the power reading shows "on" ~5.5 s after the command (15:12:30.135 -> 15:12:35.622).
# 10 s is that, doubled. Inside it, "control entity on, physical off" is the fan spinning up, not the #449 drift.
WHF_START_CONVERGE_WINDOW_S = 10.0
# After a safety stop the enforcer re-checks this long later: a stop that did not take is retried, and a
# command-only fan (no readable physical state) is treated as off from then on (Issue #1176). Issue #1215: it
# fires just after the convergence window (it was 12 s, i.e. BEFORE a 15.5 s lag could ever have confirmed the stop).
WHF_STOP_VERIFY_S = WHF_STOP_CONVERGE_WINDOW_S + 0.5
# Issue #1215: a stop that did not take is retried (a plain ``turn_off``, never an "on" first) at most this many times,
# so at most 1 + 2 = 3 commands per stop episode. One command is all a working install needs; the retries cover a
# dropped one-way RF transmission, and each costs a whole convergence window, so three attempts span ~60 s, enough to
# know the stop is not taking without ever becoming the 66-commands-in-two-minutes storm of 2026-11-01.
WHF_STOP_MAX_RETRIES = 2
# After the retries are spent the enforcer leaves the fan alone this long (the alert has been raised) and then starts
# a fresh episode, so a fan that stays on is re-attempted every 5 minutes, not every 15 s tick. Equals the toggle floor.
WHF_STOP_GIVEUP_HOLD_S = 300.0
# Issue #1215: after a sealed-house stop the HVAC restore waits until the fan has read physically off this long. Just
# over the 5.5 s power-reading lag on a start: a restart commanded by anything while the stop was confirming would show
# as "on" within that time, so a restore never lands on a fan that is about to read on again (15:12:35 -> 15:12:46).
WHF_RESTORE_QUIET_S = 6.0
# Issue #1215: after an OCCUPANT remote press (seen at once as an RF event) the power reading still lags: 5.5 s on the
# production history, 8-15 s in #1198, up to the firmware's 20 s on-lag default. For that long Climate Advisor starts
# no fan of its own (the fan is on its way up; a start would send off, then on, to it).
WHF_OCCUPANT_PRESS_PENDING_S = 20.0
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
    overlap_for_s -> Issue #1288: seconds the fan has read physically on while the thermostat (in an active mode)
                    reported heating/cooling, continuously, as the coordinator observed it; None when not overlapping.
    """

    fan_on: bool | None
    fan_on_for_s: float | None
    sensors: tuple[SensorReading, ...]
    stale_names: tuple[str, ...] = ()
    occupant_fan_on_at: datetime | None = None
    occupant_hvac_on_at: datetime | None = None
    overlap_for_s: float | None = None


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
    stop_in_flight_for_s -> Issue #1215: seconds since Climate Advisor's own stop command while that stop is still
                           inside its convergence window and the fan has not yet read off; None otherwise. The power
                           reading lags the command, so "running" inside the window is the stop working, not a fresh
                           violation.
    hvac_action         -> Issue #1288: the thermostat's live ``hvac_action`` (heating/cooling/idle/...), None unknown
    overlap_for_s       -> Issue #1288: seconds the fan has read physically on while the thermostat reported
                           heating/cooling, continuously (None: not overlapping)
    thermostat_mode_for_s -> Issue #1288: seconds since the thermostat's mode last changed (None when unknown)
    own_hvac_off_for_s  -> Issue #1288: seconds since Climate Advisor's own last HVAC-off command (None: none known)
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
    stop_in_flight_for_s: float | None = None
    hvac_action: str | None = None
    overlap_for_s: float | None = None
    thermostat_mode_for_s: float | None = None
    own_hvac_off_for_s: float | None = None


@dataclass(frozen=True)
class WhfSafetyDecision:
    """The decision plus what the shell needs to act on it."""

    action: WhfSafetyAction
    reason: str
    retry_in_s: float | None = None
    stale_sensor_count: int = 0  # sensors counted closed because they were unknown for > 60 s
    sealed: bool = False
    in_flight: bool = False  # Issue #1215: NONE because Climate Advisor's own stop is still converging
    backstop: bool = False  # Issue #1288: decided by the rule-2 state backstop (device state, no press stamp)


def sensor_reading(
    *,
    state: str | None,
    is_open: bool,
    age_s: float | None,
    since_start_s: float | None,
    readable_for_s: float | None = None,
) -> SensorReading:
    """One door/window sensor as every "is the house sealed" judge reads it (Issue #1177).

    ``state`` is the raw HA state string, ``None`` when the entity is missing; ``is_open`` is the polarity-aware
    "reads open" of a readable state; ``age_s`` is seconds since its ``last_changed`` (None when untracked);
    ``since_start_s`` is seconds since the integration started (what a MISSING entity is aged from). An
    unavailable/unknown/missing sensor carries ``unknown_for_s`` so ``house_is_sealed`` counts it open for
    ``WHF_SENSOR_UNKNOWN_GRACE_S`` and closed after. When neither age is known the grace counts as long over
    (rule 1 fails safe: an unreadable sensor of unknown age can never keep the fan running on a sealed house;
    "just now" would have counted it open forever). The coordinator's snapshot builder and the promise adapter
    both build their readings here, so they cannot disagree.

    ``readable_for_s`` (Issue #1389) is seconds since the sensors could first be read: Home Assistant finished
    starting with this integration already running (0.0 while it is still starting). A sensor reads
    unavailable from the moment HA shuts down until its own integration (a group helper waits for its
    members) has loaded on the next start, so its ``last_changed`` age includes the downtime and the boot:
    the 60 s grace must not run before HA could deliver the reading. ``None`` (a reload, HA already running)
    leaves the age as it was.
    """
    if state is None or state in ("unavailable", "unknown"):
        since = age_s if state is not None else None
        if since is None:
            since = since_start_s
        unknown_for = since if since is not None else math.inf
        if readable_for_s is not None:
            unknown_for = min(unknown_for, max(0.0, readable_for_s))
        return SensorReading(is_open=False, unknown_for_s=unknown_for, stable_for_s=unknown_for)
    return SensorReading(is_open=is_open, stable_for_s=age_s)


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
    then rule 2 (WHF vs HVAC, newest occupant action wins), then the rule-2 state backstop.
    """
    if inputs.fan_mode not in _WHF_FAN_MODES:
        return WhfSafetyDecision(WhfSafetyAction.NONE, "not a whole-house-fan install")

    running = inputs.fan_on if inputs.fan_on is not None else inputs.fan_believed_on
    if not running:
        return WhfSafetyDecision(WhfSafetyAction.NONE, "fan not running")

    # Issue #1215: Climate Advisor's own stop is still converging. One stop runs at a time: the physical reading lags
    # the command, so "running" inside the window is the stop working, not a fresh violation. Deciding again here is
    # what restarted the fan 29 times in two minutes. The window ends (and the stop is judged, retried or reported)
    # in the shell, not here.
    if inputs.stop_in_flight_for_s is not None:
        return WhfSafetyDecision(
            WhfSafetyAction.NONE,
            f"stop in flight ({inputs.stop_in_flight_for_s:.0f}s of {WHF_STOP_CONVERGE_WINDOW_S:.0f}s):"
            " awaiting the physical reading",
            in_flight=True,
        )

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

    backstop = _rule2_state_backstop(inputs, stale)
    if backstop is not None:
        return backstop
    return WhfSafetyDecision(WhfSafetyAction.NONE, "no conflict", stale_sensor_count=stale)


def _rule2_state_backstop(inputs: WhfSafetyInputs, stale: int) -> WhfSafetyDecision | None:
    """Issue #1288: the fan reads on and the thermostat is heating/cooling, whatever the press stamps say.

    Ground truth only: ``fan_on`` must be a real physical reading (a command-only fan is blind by design) and the
    thermostat must be in an active mode AND report the compressor running. Below the overlap threshold, or while
    Climate Advisor's own HVAC-off is still landing, it waits (DEFER re-asks in time). Which device stops is chosen
    by state: a thermostat mode that changed after the fan physically came on is the newer action, so the fan
    stops; otherwise the heating/cooling is suppressed. Either way one device stops within ~30 s.
    """
    if inputs.fan_on is not True:
        return None
    if inputs.thermostat_mode not in WHF_ACTIVE_HVAC_MODES:
        return None
    if str(inputs.hvac_action).lower() not in _COMPRESSOR_ACTIVE_ACTIONS:
        return None
    if inputs.overlap_for_s is None:
        return None
    retry = 0.0
    if inputs.overlap_for_s < WHF_RULE2_OVERLAP_CONFIRM_S:
        retry = WHF_RULE2_OVERLAP_CONFIRM_S - inputs.overlap_for_s
    if inputs.own_hvac_off_for_s is not None and inputs.own_hvac_off_for_s < WHF_RULE2_OWN_OFF_QUIET_S:
        retry = max(retry, WHF_RULE2_OWN_OFF_QUIET_S - inputs.own_hvac_off_for_s)
    if retry > 0.0:
        return WhfSafetyDecision(
            WhfSafetyAction.DEFER,
            "rule-2 state backstop: fan and heating/cooling both on — confirming before acting",
            retry_in_s=retry,
            stale_sensor_count=stale,
            backstop=True,
        )
    fan_for, mode_for = inputs.fan_on_for_s, inputs.thermostat_mode_for_s
    if fan_for is not None and mode_for is not None and mode_for < fan_for:
        return WhfSafetyDecision(
            WhfSafetyAction.STOP_MUTEX_FAN,
            f"rule-2 state backstop: thermostat switched to {inputs.thermostat_mode} after the fan came on and is"
            f" {inputs.hvac_action} beside it ({inputs.overlap_for_s:.0f}s) — stopping the fan (newest action wins)",
            stale_sensor_count=stale,
            backstop=True,
        )
    return WhfSafetyDecision(
        WhfSafetyAction.SUPPRESS_HVAC,
        f"rule-2 state backstop: thermostat {inputs.hvac_action} ({inputs.thermostat_mode}) beside the running fan"
        f" ({inputs.overlap_for_s:.0f}s) — suppressing heating/cooling (the fan is the newer action)",
        stale_sensor_count=stale,
        backstop=True,
    )
