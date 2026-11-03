"""Real-HA regression test for Issue #1286: an HVAC stop (or start) during startup coalescing after a restart.

Occupant story: Home Assistant restarts while the furnace (or AC) is running and the equipment stops a minute
later. The dashboard's "HVAC runtime today" must stop growing at the real stop, the idle minutes must not be credited
as runtime by a later flush (23:59 rollover, TOU edge, mode glide, shutdown), and the heat/cool thermal observation
opened by the Issue #96 late start must end at the stop instead of sampling idle decay as if the equipment ran.

Mirror (symmetry check): Home Assistant restarts while the equipment is idle and it starts a minute later, inside
the same 300 s coalescing window. That run must be counted from (roughly) its real start, not lost or shortened.

Shape: a settled house runs the equipment, Climate Advisor restarts with the thermostat still running (no settle,
so ``_startup_coalesce_active`` is True and the first refresh arms the late-start session), the thermostat changes
state at ~60 s, then the clock runs past coalescing end and a further 30 minutes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.const import (
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    STARTUP_COALESCE_SECONDS,
)

pytestmark = pytest.mark.house

_COLD_DAY = {
    "start": datetime(2026, 1, 15, 15, 0, tzinfo=UTC),
    "outdoor_f": 30.0,
    "high_f": 36.0,
    "low_f": 22.0,
    "initial_indoor_f": 62.0,
}
_HOT_DAY = {
    "start": datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
    "outdoor_f": 92.0,
    "high_f": 96.0,
    "low_f": 76.0,
    "initial_indoor_f": 82.0,
}
_DIRECTIONS = {
    # direction: (house kwargs, running action, observation type, sign toward "satisfied")
    "heat": (_COLD_DAY, "heating", OBS_TYPE_HVAC_HEAT, +1),
    "cool": (_HOT_DAY, "cooling", OBS_TYPE_HVAC_COOL, -1),
}
_TAIL_S = 30 * 60
_TOL_MIN = 1.0


async def _running_house(make_house, direction: str):
    kwargs, action, _obs, _sign = _DIRECTIONS[direction]
    house = await make_house(profile="scripted", startup="settled", **kwargs)
    await house.advance(minutes=10)  # past the sim's 300 s min-run, so a later stop is not held by dwell
    assert house.thermostat.mode == direction, f"precondition: mode {house.thermostat.mode}"
    assert house.thermostat.action == action, f"precondition: action {house.thermostat.action}"
    return house


def _satisfied_f(house, direction: str) -> float:
    sign = _DIRECTIONS[direction][3]
    return house.thermostat.setpoint_f + sign * 2.5  # beyond the sim's 1.5 F stop point


def _demand_f(house, direction: str) -> float:
    sign = _DIRECTIONS[direction][3]
    return house.thermostat.setpoint_f - sign * 3.0  # beyond the sim's 1.5 F start point


def _obs_phase(coord, obs_type: str):
    obs = (getattr(coord, "_pending_observations", None) or {}).get(obs_type)
    return None if obs is None else obs.get("_phase")


@pytest.mark.parametrize("direction", ["heat", "cool"])
async def test_stop_during_coalescing_is_not_credited(make_house, direction: str) -> None:
    _kw, action, obs_type, _sign = _DIRECTIONS[direction]
    house = await _running_house(make_house, direction)

    await house.restart(settle=False)
    coord = house.coordinator
    assert coord._startup_coalesce_active, "precondition: restart left coalescing open"
    assert coord._hvac_on_since is not None, "precondition: the Issue #96 late start armed a session"
    assert coord._hvac_session_mode == direction, coord._hvac_session_mode
    t0 = dt_util.now()
    r0 = coord.get_hvac_runtime_today()

    await house.advance(seconds=30)
    house.set_indoor(_satisfied_f(house, direction))
    await house.advance(seconds=30)
    assert house.thermostat.action == "idle", f"precondition: stop at ~60 s, action {house.thermostat.action}"
    assert coord._startup_coalesce_active, "precondition: the stop landed inside the coalescing window"
    t_stop = dt_util.now()
    ran_min = (t_stop - t0).total_seconds() / 60.0

    await house.advance(seconds=STARTUP_COALESCE_SECONDS - 60 + 30 + _TAIL_S)
    assert not coord._startup_coalesce_active, "precondition: coalescing ended"
    assert house.thermostat.mode == direction, f"precondition: mode changed to {house.thermostat.mode}"
    assert house.thermostat.action == "idle", f"precondition: action {house.thermostat.action}"
    idle_min = (dt_util.now() - t_stop).total_seconds() / 60.0

    live = coord.get_hvac_runtime_today()
    phase = _obs_phase(coord, obs_type)
    on_since = coord._hvac_on_since
    coord._flush_hvac_runtime()  # what the 23:59 rollover / TOU edge / mode glide / shutdown flush does
    flushed = coord._today_record.hvac_runtime_minutes
    summary = (
        f"{direction}: base={r0:.1f} ran={ran_min:.1f} idle={idle_min:.1f} live={live:.1f} "
        f"flushed={flushed:.1f} on_since={on_since} obs_phase={phase}"
    )
    failures = []
    if live > r0 + ran_min + _TOL_MIN:
        failures.append(f"live runtime counts {live - r0 - ran_min:.1f} idle min")
    if flushed > r0 + ran_min + _TOL_MIN:
        failures.append(f"flush credited {flushed - r0 - ran_min:.1f} idle min")
    if on_since is not None:
        failures.append("session timer still armed after the stop")
    if phase == "active":
        failures.append(f"{obs_type} observation still active after the stop")
    assert not failures, f"{summary}: {failures}"


@pytest.mark.parametrize("direction", ["heat", "cool"])
async def test_mirror_start_during_coalescing_is_counted(make_house, direction: str) -> None:
    _kw, action, obs_type, _sign = _DIRECTIONS[direction]
    house = await _running_house(make_house, direction)
    house.set_indoor(_satisfied_f(house, direction))
    await house.advance(minutes=7)  # stop, then past the sim's 300 s min-off
    assert house.thermostat.action == "idle", f"precondition: idle before restart, {house.thermostat.action}"

    await house.restart(settle=False)
    coord = house.coordinator
    assert coord._startup_coalesce_active, "precondition: restart left coalescing open"
    assert coord._hvac_on_since is None, "precondition: no session at an idle restart"
    r0 = coord.get_hvac_runtime_today()

    await house.advance(seconds=30)
    house.set_indoor(_demand_f(house, direction))
    await house.advance(seconds=30)
    assert house.thermostat.action == action, f"precondition: start at ~60 s, action {house.thermostat.action}"
    assert coord._startup_coalesce_active, "precondition: the start landed inside the coalescing window"
    t_start = dt_util.now()
    on_since_in_window = coord._hvac_on_since

    await house.advance(seconds=STARTUP_COALESCE_SECONDS - 60 + 30 + _TAIL_S)
    assert not coord._startup_coalesce_active, "precondition: coalescing ended"
    assert house.thermostat.action == action, f"precondition: still running, {house.thermostat.action}"
    ran_min = (dt_util.now() - t_start).total_seconds() / 60.0

    live = coord.get_hvac_runtime_today()
    phase = _obs_phase(coord, obs_type)
    on_since = coord._hvac_on_since
    armed_late_min = None if on_since is None else (on_since - t_start).total_seconds() / 60.0
    summary = (
        f"{direction} mirror: base={r0:.1f} ran={ran_min:.1f} live={live:.1f} counted={live - r0:.1f} "
        f"on_since_in_window={on_since_in_window} armed_late_by_min={armed_late_min} obs_phase={phase}"
    )
    failures = []
    if live < r0 + ran_min - _TOL_MIN:
        failures.append(f"undercount {r0 + ran_min - live:.1f} min")
    if on_since is None:
        failures.append("no session armed for the running equipment")
    if phase != "active":
        failures.append(f"{obs_type} observation not active while running (phase={phase})")
    assert not failures, f"{summary}: {failures}"
