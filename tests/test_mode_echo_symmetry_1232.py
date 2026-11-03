"""Issue #1232 (symmetry) and Issue #1246: CA's own MODE writes echoing back late.

``_async_thermostat_changed`` recognises the echo of the mode CA last commanded BY VALUE (the plain mode-override
branch refuses any change to ``_last_commanded_hvac_mode``; the other branches add the 120 s
``_is_expected_confirmation`` guard). Issue #1246 covers the shape that does not: two mode writes inside the echo
window (a door-close restore of the pre-pause mode, then the comfort selector flipping to the other family), where a
cloud thermostat reports the FIRST write after the second was sent. The engine now remembers every mode write for
``OWN_WRITE_ECHO_WINDOW_S`` (``_own_mode_writes``) and the coordinator asks whether the reported mode is one of them.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.const import OWN_WRITE_ECHO_WINDOW_S
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


def _door_close_restore_with_family_flip(house: _House) -> None:
    """The house drifted hot during a window pause: closing restores 'heat', then the selector flips to 'cool'."""
    house.ae._current_classification = house.co._current_classification  # the engine's own copy drives the restore
    run_coro(house.ae._restore_comfort_after_door_window_close(restore_mode="heat", trigger="sensor_closed"))
    written = [a["data"]["hvac_mode"] for a in house.fh.action_log if a["data"].get("hvac_mode")]
    assert written[0] == "heat" and written[-1] == "cool", written  # the shape under test: restore, then family flip
    assert [w.mode for w in house.ae._own_mode_writes][:2] == ["heat", "cool"]


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_door_close_restore_then_family_flip_late_first_echo_is_not_an_override(fan_mode):
    """Occupant: the window closes, the house is restored, and the late 'heat' report does not pause automation."""
    house = _House(fan_mode, state="off", attrs={"current_temperature": 86.0})
    with house.sch.installed():
        _door_close_restore_with_family_flip(house)
        house.wait(60)
        house.thermostat_reports(state="heat")  # the slow thermostat reports CA's FIRST write
        # Checked at once: a 0-minute confirm window can resolve a false override before the end of the test.
        assert not _mode_overrides(house)
        house.wait(30)
        house.thermostat_reports(state="cool")
    assert not _mode_overrides(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_real_occupant_mode_change_inside_the_window_is_still_an_override(fan_mode):
    """Occupant: switching the thermostat to a mode CA never commanded is theirs, even right after CA's two writes."""
    house = _House(fan_mode, state="off", attrs={"current_temperature": 86.0})
    with house.sch.installed():
        _door_close_restore_with_family_flip(house)
        house.wait(60)
        house.thermostat_reports(state="heat_cool")  # CA wrote heat and cool, never heat_cool
    assert _mode_overrides(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_an_echo_is_consumed_once(fan_mode):
    """A second report of an earlier mode (after its echo was consumed) is the occupant, not a replay of CA's write."""
    house = _House(fan_mode, state="off")
    with house.sch.installed():
        run_coro(house.ae._set_hvac_mode("cool", reason="test"))
        house.wait(20)
        run_coro(house.ae._set_hvac_mode("heat", reason="test"))
        house.wait(30)
        house.thermostat_reports(state="cool")
        house.thermostat_reports(state="heat")
        assert not _mode_overrides(house)
        house.wait(30)
        house.thermostat_reports(state="cool")  # no cool write left in memory: the occupant
    assert _mode_overrides(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_an_earlier_mode_write_is_forgotten_after_the_window(fan_mode):
    house = _House(fan_mode, state="off")
    with house.sch.installed():
        run_coro(house.ae._set_hvac_mode("cool", reason="test"))
        house.wait(20)
        run_coro(house.ae._set_hvac_mode("heat", reason="test"))
        house.wait(OWN_WRITE_ECHO_WINDOW_S + 10)
        house.thermostat_reports(state="cool")
    assert _mode_overrides(house)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_post_fan_restore_then_family_flip_late_first_echo_is_not_an_override(fan_mode):
    """Occupant: the fan session ends, the pre-fan mode comes back, then the selector flips family; no false pause.

    Same two-write shape as the door-close restore: ``_deactivate_fan`` blind-restores ``_pre_fan_hvac_mode``
    (nat_vent context) and the periodic comfort selector then writes the other family.
    """
    house = _House(fan_mode, state="off", attrs={"current_temperature": 86.0})
    with house.sch.installed():
        run_coro(house.ae._set_hvac_mode("heat", reason="post-fan restore"))
        house.wait(20)
        run_coro(house.ae._set_hvac_mode("cool", reason="comfort selector flip"))
        house.wait(60)
        house.thermostat_reports(state="heat")
        assert not _mode_overrides(house)
        house.wait(30)
        house.thermostat_reports(state="cool")
    assert not _mode_overrides(house)


# --- a FAILED mode write is forgotten (symmetry with the setpoint memory, Issue #1246) -----------------------------


def _fail_next_mode_write(house: _House) -> None:
    """The next ``set_hvac_mode`` service call raises (the thermostat never receives it); later ones succeed."""
    real = house.fh.services.async_call
    state = {"armed": True}

    async def _call(domain, service, data=None, *a, **kw):
        if service == "set_hvac_mode" and state["armed"]:
            state["armed"] = False
            raise RuntimeError("thermostat unreachable")
        return await real(domain, service, data, *a, **kw)

    house.fh.services.async_call = _call


def _ca_writes_heat(house: _House, via: str) -> None:
    if via == "set_hvac_mode":
        run_coro(house.ae._set_hvac_mode("heat", reason="test"))
    else:  # _set_temperature switches the mode first (recording it twice) via _ensure_hvac_mode_for_write
        house.ca_writes(70.0, mode="heat")


@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("via", ["set_hvac_mode", "set_temperature"])
def test_failed_mode_write_is_forgotten_so_occupant_choosing_it_is_detected(fan_mode, via):
    """Occupant: CA's heat write fails, its cool write lands; the person then picks heat and is not ignored."""
    house = _House(fan_mode, state="off")
    _fail_next_mode_write(house)
    with house.sch.installed():
        _ca_writes_heat(house, via)
        house.wait(20)
        run_coro(house.ae._set_hvac_mode("cool", reason="test"))
        house.wait(60)
        house.thermostat_reports(state="heat")  # the occupant: CA's heat never reached the thermostat
    assert _mode_overrides(house)
    assert [w.mode for w in house.ae._own_mode_writes] == ["cool"]


@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("via", ["set_hvac_mode", "set_temperature"])
def test_successful_mode_write_late_echo_is_still_suppressed_after_a_failed_one(fan_mode, via):
    house = _House(fan_mode, state="off")
    _fail_next_mode_write(house)
    with house.sch.installed():
        _ca_writes_heat(house, via)
        house.wait(20)
        run_coro(house.ae._set_hvac_mode("cool", reason="test"))
        house.wait(100)
        house.thermostat_reports(state="cool")  # CA's successful write, echoing late
    assert not _mode_overrides(house)
