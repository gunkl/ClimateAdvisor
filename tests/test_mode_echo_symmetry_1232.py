"""Issue #1232 (symmetry): CA's own MODE writes echoing back late.

Evidence for the decision that the mode path needs no new memory: ``_async_thermostat_changed`` already recognises a
mode echo BY VALUE -- the plain mode-override branch refuses any change to the mode CA last commanded
(``new_state.state != _last_commanded_hvac_mode``), with no time limit, and the other branches add the 120 s
``_is_expected_confirmation`` guard. These pin that, and the one shape it does not cover (two mode writes in flight).
"""

from __future__ import annotations

import pytest

from tests.test_setpoint_echo_by_value_1232 import FAN_MODES, _House
from tools.sim_harness._loop import run_coro


def _mode_overrides(house: _House) -> bool:
    return house.overrides > 0 or any(house.override_state)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("delay_s", [45, 110, 170, 400])
def test_late_echo_of_the_last_commanded_mode_is_not_an_override(fan_mode, delay_s):
    house = _House(fan_mode, state="off")
    with house.sch.installed():
        run_coro(house.ae._set_hvac_mode("heat", reason="test"))
        house.wait(delay_s)
        house.thermostat_reports(state="heat")
    assert not _mode_overrides(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_a_different_mode_than_commanded_is_still_an_override(fan_mode):
    house = _House(fan_mode, state="off")
    with house.sch.installed():
        run_coro(house.ae._set_hvac_mode("heat", reason="test"))
        house.wait(60)
        house.thermostat_reports(state="cool")
    assert _mode_overrides(house)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP (reported with Issue #1232, not fixed here): the mode path remembers only the LAST commanded "
    "mode, so the late echo of an EARLIER mode write, reported after a newer one was sent, reads as the occupant. "
    "Needs two mode writes inside ~120 s (no current writer does that); flip this to a plain test when fixed.",
)
@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_echo_of_an_earlier_mode_write_after_a_newer_one_is_not_an_override(fan_mode):
    house = _House(fan_mode, state="off")
    with house.sch.installed():
        run_coro(house.ae._set_hvac_mode("cool", reason="test"))
        house.wait(20)
        run_coro(house.ae._set_hvac_mode("heat", reason="test"))
        house.wait(40)
        house.thermostat_reports(state="cool")  # the slow thermostat finally reports the FIRST write
        house.wait(30)
        house.thermostat_reports(state="heat")
    assert not _mode_overrides(house)
