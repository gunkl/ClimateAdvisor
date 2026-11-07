"""Issue #1397: a thermostat that reports Climate Advisor's setpoint 0.1 C low is not the occupant.

Occupant story (a Celsius, HomeKit, heat-only bedroom zone): Climate Advisor writes 18.0 C. The thermostat reports
18.0, then 30-60 s later 17.9 -- nobody touched it. Climate Advisor read 18.0 -> 17.9 as the occupant turning the
dial: confirmed a manual override 10 minutes later and paused the zone for the 30-minute manual grace, rewrote 18.0
when it ended, and the drift started the next round. All evening, every ~40 minutes, with an "override expired" push
each time. The #1232 echo memory did not catch it: 18.0 is on the 0.5 C grid so only an exact 18.0 matched, and the
18.0 report had already consumed the remembered write.

After the fix (owner decision 2026-11-07): once the thermostat has shown Climate Advisor's setpoint, a report within
0.5 F (0.28 C) of it is the thermostat holding that setpoint, not the occupant -- no time window, any number of times.
Every one-step occupant change (1 F; 0.5 C = 0.9 F) is still caught.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.own_write_echo import (
    hold_tolerance,
    holds_own_setpoint,
    values_match,
)
from tests.test_setpoint_echo_by_value_1232 import FAN_MODES, _House
from tools.sim_harness._loop import run_coro

# ---------------------------------------------------------------------------------------------------------------------
# The one rule (pure)
# ---------------------------------------------------------------------------------------------------------------------

_C_TOL = hold_tolerance("celsius")
_F_TOL = hold_tolerance("fahrenheit")


def test_the_tolerance_is_half_a_degree_fahrenheit_in_either_unit():
    assert pytest.approx(0.5) == _F_TOL
    assert pytest.approx(0.5 / 1.8) == _C_TOL  # 0.28 C


@pytest.mark.parametrize(
    ("held", "observed", "tol", "holding"),
    [
        ({"temperature": 18.0}, {"temperature": 17.9}, _C_TOL, True),  # #1397: 0.1 C low
        ({"temperature": 18.0}, {"temperature": 18.2}, _C_TOL, True),  # 0.2 C high (0.36 F)
        ({"temperature": 18.0}, {"temperature": 17.7}, _C_TOL, False),  # 0.3 C = 0.54 F: past the tolerance
        ({"temperature": 18.0}, {"temperature": 18.5}, _C_TOL, False),  # one 0.5 C step: the occupant
        ({"temperature": 64.0}, {"temperature": 64.2}, _F_TOL, True),  # Fahrenheit drift
        ({"temperature": 64.0}, {"temperature": 64.5}, _F_TOL, True),  # exactly 0.5 F: inside
        ({"temperature": 64.0}, {"temperature": 65.0}, _F_TOL, False),  # one 1 F step: the occupant
        ({}, {"temperature": 17.9}, _C_TOL, False),  # CA holds nothing here: the old rules decide
        ({"temperature": 18.0}, {"temperature": "unavailable"}, _C_TOL, False),
        ({"temperature": 18.0}, {"temperature": float("nan")}, _C_TOL, False),
        # A single ``temperature`` write accounts for a band edge; every changed edge must hold.
        ({"temperature": 77.0}, {"target_temp_high": 77.2}, _F_TOL, True),
        ({"target_temp_low": 69.0, "target_temp_high": 77.0}, {"target_temp_low": 69.2}, _F_TOL, True),
        (
            {"target_temp_low": 69.0, "target_temp_high": 77.0},
            {"target_temp_low": 69.2, "target_temp_high": 80.0},
            _F_TOL,
            False,
        ),
        ({"target_temp_low": 69.0}, {"target_temp_high": 69.0}, _F_TOL, False),  # an edge write holds only its edge
    ],
)
def test_holds_own_setpoint(held, observed, tol, holding):
    assert holds_own_setpoint(held=held, observed=observed, tolerance=tol) is holding


def test_a_signed_in_person_is_never_the_thermostat_holding_the_setpoint():
    assert (
        holds_own_setpoint(
            held={"temperature": 18.0}, observed={"temperature": 17.9}, tolerance=_C_TOL, event_context_user_id="u1"
        )
        is False
    )


@pytest.mark.parametrize(
    ("recorded", "observed", "step", "tol", "match"),
    [
        (18.0, 17.9, 0.5, 0.0, False),  # before #1397: an on-grid write had to come back exactly
        (18.0, 17.9, 0.5, _C_TOL, True),  # the echo memory now uses the same tolerance
        (18.0, 18.5, 0.5, _C_TOL, False),  # the occupant's one-step tweak is still not the echo
        (72.0, 73.0, 1.0, _F_TOL, False),
        (22.22, 22.5, 0.5, _C_TOL, True),  # off-grid rounding is unchanged
    ],
)
def test_the_echo_value_match_uses_the_same_tolerance(recorded, observed, step, tol, match):
    assert values_match(recorded, observed, step, hold_tolerance=tol) is match


# ---------------------------------------------------------------------------------------------------------------------
# The real coordinator and engine (headless)
# ---------------------------------------------------------------------------------------------------------------------

_18C_F = 64.4  # 18.0 C in Climate Advisor's internal F


def _homekit(fan_mode: str, *, step: float = 0.5) -> _House:
    """The #1397 zone: Celsius, heat, a thermostat at 17.0 C."""
    return _House(fan_mode, unit="celsius", initial=17.0, attrs={"target_temp_step": step})


def _quiet(house: _House) -> None:
    assert house.overrides == 0, "a manual change was counted"
    assert house.override_state == (False, False), "a manual override is pending or active"
    assert not house.ae._grace_active, "the zone was paused for the manual grace period"


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_the_homekit_drift_after_the_echo_is_not_an_override(fan_mode):
    """The production cycle: 18.0 at 36 s (CA's echo), 17.9 at 67 s, then nothing for 15 minutes."""
    house = _homekit(fan_mode)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(36)
        house.thermostat_reports(temperature=18.0)
        house.wait(31)
        house.thermostat_reports(temperature=17.9)
        house.wait(15 * 60)  # past the 10-minute override confirmation
    _quiet(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_the_drift_keeps_holding_past_the_echo_window_and_repeats(fan_mode):
    """No time window and nothing consumed: the thermostat wobbling 17.9 / 18.0 / 17.9 later is still CA's setpoint."""
    house = _homekit(fan_mode)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(36)
        house.thermostat_reports(temperature=18.0)
        for _ in range(3):
            house.wait(400)  # each report well past the 180 s echo window
            house.thermostat_reports(temperature=17.9)
            house.wait(400)
            house.thermostat_reports(temperature=18.0)
        house.wait(15 * 60)
    _quiet(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_the_drifted_value_reported_first_is_the_echo(fan_mode):
    """The thermostat answers the 18.0 write with 17.9 straight away (the echo memory's own tolerance)."""
    house = _homekit(fan_mode)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(45)
        house.thermostat_reports(temperature=17.9)
        house.wait(15 * 60)
    _quiet(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_fahrenheit_drift_is_not_an_override(fan_mode):
    house = _House(fan_mode, initial=62.0)
    with house.sch.installed():
        house.ca_writes(64.0)
        house.wait(40)
        house.thermostat_reports(temperature=64.0)
        house.wait(400)
        house.thermostat_reports(temperature=64.2)
        house.wait(15 * 60)
    _quiet(house)


# --- the occupant who really changes it is still caught ---------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize(
    ("unit", "write_f", "echo", "occupant"),
    [
        ("celsius", _18C_F, 18.0, 18.5),  # one 0.5 C step up
        ("celsius", _18C_F, 18.0, 17.5),  # one 0.5 C step down
        ("fahrenheit", 64.0, 64.0, 65.0),  # one 1 F step
        ("fahrenheit", 64.0, 64.0, 63.0),
    ],
)
def test_a_one_step_occupant_change_after_the_echo_is_still_an_override(fan_mode, unit, write_f, echo, occupant):
    house = _House(fan_mode, unit=unit, initial=echo - 1.0, attrs={"target_temp_step": 0.5 if unit == "celsius" else 1})
    with house.sch.installed():
        house.ca_writes(write_f)
        house.wait(40)
        house.thermostat_reports(temperature=echo)
        house.wait(400)
        house.thermostat_reports(temperature=occupant)
    assert house.overrides == 1, "the occupant's one-step change was swallowed as the thermostat's drift"
    assert any(house.override_state)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_a_change_just_past_the_tolerance_is_the_occupant(fan_mode):
    """A 0.1 C-step thermostat: 0.2 C (0.36 F) is CA's setpoint, 0.3 C (0.54 F) is a change."""
    house = _homekit(fan_mode, step=0.1)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(40)
        house.thermostat_reports(temperature=18.0)
        house.wait(400)
        house.thermostat_reports(temperature=17.8)
        assert house.overrides == 0, "0.2 C from CA's setpoint is inside the tolerance"
        house.wait(60)
        house.thermostat_reports(temperature=18.3)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_dialling_back_to_cas_value_after_a_real_change_is_the_occupant(fan_mode):
    """Once a change is counted as the occupant's CA no longer holds the setpoint: back to 17.9 / 18.0 counts too."""
    house = _homekit(fan_mode)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(40)
        house.thermostat_reports(temperature=18.0)
        house.wait(400)
        house.thermostat_reports(temperature=20.0)  # the occupant
        first = house.overrides
        house.wait(30)
        house.thermostat_reports(temperature=17.9)  # ...and back near CA's value
    assert first == 1
    assert house.overrides == 2, "the occupant's dial-back was swallowed as the thermostat holding CA's setpoint"


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_a_value_the_thermostat_never_showed_is_judged_as_before(fan_mode):
    """CA wrote 18.0 but the thermostat never showed it; 17.9 reported after the echo window is a possible change.

    Same as #1232's "equal value after the window": with no sign the thermostat took the write there is nothing it is
    holding.
    """
    house = _homekit(fan_mode)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(400)
        house.thermostat_reports(temperature=17.9)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_a_new_write_replaces_the_held_setpoint(fan_mode):
    """CA moves the zone to 16.0 C; until the thermostat shows 16.0, a 17.9 report is not CA's setpoint any more."""
    house = _homekit(fan_mode)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(40)
        house.thermostat_reports(temperature=18.0)
        house.wait(400)
        house.ca_writes(60.8)  # 16.0 C
        assert house.ae._held_own_setpoints.get(house.ce) is None
        house.wait(400)  # no echo of 16.0 arrives
        house.thermostat_reports(temperature=17.9)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_a_signed_in_person_near_cas_value_is_the_occupant(fan_mode):
    from homeassistant.core import Context

    house = _homekit(fan_mode, step=0.1)
    with house.sch.installed():
        house.ca_writes(_18C_F)
        house.wait(40)
        house.thermostat_reports(temperature=18.0)
        house.wait(400)
        house.thermostat_reports(context=Context(user_id="user-123"), temperature=17.9)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_heat_cool_one_edge_drifting_and_one_moved_is_the_occupant(fan_mode):
    house = _House(fan_mode, state="heat_cool", attrs={"target_temp_low": 68.0, "target_temp_high": 76.0})
    with house.sch.installed():
        run_coro(
            house.ae._call_climate(
                "set_temperature",
                {"entity_id": house.ce, "target_temp_low": 69.0, "target_temp_high": 77.0},
                "band",
            )
        )
        house.wait(60)
        house.thermostat_reports(target_temp_low=69.0, target_temp_high=77.0)  # CA's echo
        house.wait(400)
        house.thermostat_reports(target_temp_low=69.2)  # drift: holding
        assert house.overrides == 0
        house.wait(60)
        house.thermostat_reports(target_temp_low=69.0, target_temp_high=80.0)  # the high edge moved: the occupant
    assert house.overrides == 1
