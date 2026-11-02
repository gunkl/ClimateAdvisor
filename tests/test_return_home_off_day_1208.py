"""Issue #1208: coming home on a warm or mild day restores comfort on arrival, not up to 30 minutes later.

Occupant framing: you come home on a warm afternoon, the push says "Restoring comfort temperature", and the
thermostat still holds the 80 F away setback until the next half-hourly refresh. The classifier labels warm and
mild days "off" (``hvac_mode == "off"``) even though Climate Advisor arms the comfort band on them every cycle.
Cool and cold days are labelled "heat" and were never affected, and neither were hot days ("cool").

The same no-op sat on every restore path that keyed on the label instead of the armed band: the window closing
after a nat-vent exit, the nat-vent exit itself, the economizer ending, and the dashboard "Resume".

Real ``AutomationEngine`` + the state-following fake thermostat from ``tests.test_comfort_settling_guard``; the
assertions are on what the thermostat is told and the events emitted. Nothing here re-implements production logic.
The real-HA counterpart is ``tests_ha/test_return_home_off_day_1208.py``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from tests.test_comfort_settling_guard import (
    _AUTOMATION,
    _apply,
    _classification,
    _Clock,
    _make_engine,
    _run,
    _set_indoor,
)

_FANS = [FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC]
# comfort 68/74, setback 62/80 (the shared fake thermostat's config).  indoor chosen so the comfort-edge selector
# arms the family the day label implies; "mild" at 69 is next to the 68 floor (heat), "warm" at 73 next to 74 (cool).
_COMFORT = {
    "warm": ("cool", 74.0, 73.0),
    "mild": ("heat", 68.0, 69.0),
    "hot": ("cool", 74.0, 76.0),
    "cold": ("heat", 68.0, 66.0),
}
_OFF_DAYS = ["warm", "mild"]
_CONTROL_DAYS = ["hot", "cold"]


def _setpoints(engine, since: int = 0):
    return [
        (c.args[2].get("hvac_mode"), c.args[2]["temperature"])
        for c in engine.hass.services.async_call.call_args_list[since:]
        if c.args[0] == "climate" and c.args[1] == "set_temperature"
    ]


def _writes(engine, since: int = 0):
    return [
        c.args[1:3]
        for c in engine.hass.services.async_call.call_args_list[since:]
        if c.args[0] == "climate" and c.args[1] in ("set_temperature", "set_hvac_mode")
    ]


def _engine_for(day: str, fan: str, **cfg):
    _expected_mode, _expected_temp, indoor = _COMFORT[day]
    engine = _make_engine(indoor=indoor, mode="off", **{CONF_FAN_MODE: fan, **cfg})
    if fan == FAN_MODE_WHOLE_HOUSE:
        engine.config[CONF_FAN_ENTITY] = "fan.whf"
    clock = _Clock()
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda kind, data: events.append((kind, data))
    notes: list[str] = []

    async def _notify(message, *_a, **_k):
        notes.append(message)

    engine._notify = _notify
    _apply(engine, clock, day, indoor)
    engine._events = events
    engine._notes = notes
    engine._clock = clock
    return engine


def _run_at(engine, coro_factory):
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=engine._clock):
        return _run(coro_factory())


def _leave_and_return(engine):
    """Away setback held, 4 h later the occupant comes home. Returns the index where arrival writes start."""
    _run_at(engine, engine.handle_occupancy_away)
    assert _setpoints(engine)[-1][1] in (80.0, 62.0), "precondition: the away setback is armed"
    engine._clock.advance(hours=4)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    return mark


# ---------------------------------------------------------------------------
# 1. The symptom: arrival home restores comfort at once, every day type, both fan modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS + _CONTROL_DAYS)
def test_arrival_home_writes_the_comfort_edge_at_once(day, fan):
    expected_mode, expected_temp, _ = _COMFORT[day]
    engine = _engine_for(day, fan)
    mark = _leave_and_return(engine)
    assert _setpoints(engine, mark) == [(expected_mode, expected_temp)], (
        f"{day}/{fan}: the occupant is home and the thermostat must be told the comfort edge now"
    )
    assert engine._test_state.attributes["temperature"] == expected_temp
    restored = [d for k, d in engine._events if k == "occupancy_comfort_restored"]
    assert restored and restored[-1]["mode"] == expected_mode


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
def test_the_push_never_says_restoring_without_a_restore(day, fan):
    """The welcome-home push says "Restoring comfort temperature": a restore must then have been written."""
    engine = _engine_for(day, fan)
    mark = _leave_and_return(engine)
    said_restoring = any("Restoring comfort temperature" in m for m in engine._notes)
    assert not said_restoring or _setpoints(engine, mark), "pushed 'Restoring' but wrote nothing"


@pytest.mark.parametrize("day", _OFF_DAYS)
def test_off_day_arrival_near_comfort_suppresses_the_push_like_a_hot_day(day):
    """Near-comfort inputs use the band-derived family: a house already near its comfort edge needs no push."""
    engine = _engine_for(day, FAN_MODE_HVAC)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    _run_at(engine, engine.handle_occupancy_home)
    assert not any("Restoring comfort temperature" in m for m in engine._notes)
    assert any(k == "occupancy_comfort_restored" for k, _ in engine._events)


# ---------------------------------------------------------------------------
# 2. The restore-hold (#1050) applies on off days too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
def test_manual_override_holds_the_arrival_restore_on_off_days(day, fan):
    engine = _engine_for(day, fan)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._manual_override_active = True
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    assert _setpoints(engine, mark) == [], "an occupant's manual change must not be overwritten on arrival"
    deferred = [d for k, d in engine._events if k == "occupancy_restore_deferred"]
    assert deferred and deferred[-1]["reason"] == "manual_override"
    assert engine._occupancy_restore_deferred_pending is True


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
def test_window_pause_keeps_the_thermostat_off_on_arrival(day, fan):
    """Rule 3: a window open past its debounce (HVAC paused Off) must stay Off when the occupant arrives."""
    engine = _engine_for(day, fan)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._paused_by_door = True
    engine._test_state.state = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    assert _writes(engine, mark) == [], "arrival wrote a setpoint/mode through a window pause"
    assert engine._test_state.state == "off"


@pytest.mark.parametrize("day", _OFF_DAYS)
def test_whole_house_fan_session_is_not_overwritten_on_arrival(day):
    """Rule 2: arrival during a WHF session must not start heat/cool next to the running fan."""
    engine = _engine_for(day, FAN_MODE_WHOLE_HOUSE)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._pre_fan_hvac_mode = "cool"  # a WHF suppression session is live
    engine._test_state.state = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    assert engine._test_state.state == "off"


@pytest.mark.parametrize("day", _OFF_DAYS + _CONTROL_DAYS)
def test_blower_ventilation_with_a_window_open_stays_off_on_arrival(day):
    """Rule 3 (#1142 D7): a blower session ventilating through an open window keeps the thermostat Off."""
    engine = _engine_for(day, FAN_MODE_HVAC)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._natural_vent_active = True
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    engine._test_state.state = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == [], (
        f"{day}: arrival armed heat/cool through the open window while the blower ventilates"
    )


@pytest.mark.parametrize("day", _OFF_DAYS)
def test_occupant_off_pending_confirmation_is_kept_on_arrival(day):
    engine = _engine_for(day, FAN_MODE_HVAC)
    _run_at(engine, engine.handle_occupancy_away)
    engine._clock.advance(hours=4)
    engine._override_confirm_pending = True
    engine._test_state.state = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.handle_occupancy_home)
    assert _writes(engine, mark) == []
    assert engine._test_state.state == "off"


# ---------------------------------------------------------------------------
# 3. Mirror paths: each had the same label-keyed no-op
# ---------------------------------------------------------------------------


def _paused_for_window(engine, *, was: str):
    engine._paused_by_door = True
    engine._paused_entity = "binary_sensor.front_window"
    engine._pre_pause_mode = was
    engine._test_state.state = "off"


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS + _CONTROL_DAYS)
def test_door_close_after_a_nat_vent_exit_rearms_the_thermostat(day, fan):
    """Mirror 1 (door-close, restore_mode None = the #1068 nat-vent-exit Off): the house must be re-armed."""
    expected_mode, expected_temp, _ = _COMFORT[day]
    engine = _engine_for(day, fan)
    engine._test_state.state = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(
        engine,
        lambda: engine._restore_comfort_after_door_window_close(restore_mode=None, trigger="door_window_closed"),
    )
    assert _setpoints(engine, mark) == [(expected_mode, expected_temp)]
    assert engine._test_state.state == expected_mode


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS + _CONTROL_DAYS)
def test_door_close_after_a_plain_window_pause_restores_the_setpoint(day, fan):
    """Mirror 1b (door-close with a pre-pause mode): the mode comes back AND the comfort setpoint is written."""
    expected_mode, expected_temp, _ = _COMFORT[day]
    engine = _engine_for(day, fan)
    _paused_for_window(engine, was=expected_mode)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(
        engine,
        lambda: engine._restore_comfort_after_door_window_close(
            restore_mode=expected_mode, trigger="door_window_closed"
        ),
    )
    assert (expected_mode, expected_temp) in _setpoints(engine, mark)


@pytest.mark.parametrize("day", ["warm", "mild", "hot"])
def test_economizer_off_restores_comfort_when_the_day_is_reclassified(day):
    """Mirror 3 (economizer-off): a hot day's economizer ends; the day may now be warm/mild (label "off")."""
    expected_mode, expected_temp, _ = _COMFORT[day]
    engine = _engine_for(day, FAN_MODE_HVAC)
    engine._economizer_active = True
    engine._test_state.state = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: engine._deactivate_economizer(60.0))
    assert _setpoints(engine, mark) == [(expected_mode, expected_temp)]


def _ventilating(day: str, *, sensor_open: bool):
    expected_mode = _COMFORT[day][0]
    engine = _engine_for(day, FAN_MODE_WHOLE_HOUSE)
    engine.config[CONF_FAN_ENTITY] = "fan.whf"
    engine._last_outdoor_temp = 65.0
    engine._sensor_check_callback = lambda: sensor_open
    engine._sensor_debounce_pending_callback = lambda: False
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._fan_on_since = engine._clock.now - timedelta(minutes=20)
    engine._pre_fan_hvac_mode = expected_mode
    engine._fan_toggle_command_time = engine._clock.now - timedelta(seconds=400)  # past the 5-minute floor
    engine._test_state.state = "off"
    return engine


@pytest.mark.parametrize("day", _OFF_DAYS + _CONTROL_DAYS)
def test_nat_vent_exit_with_the_house_sealed_writes_the_comfort_edge(day):
    """Mirror 2 (nat-vent exit, sensors closed): ventilation ends, the comfort edge must be written."""
    expected_mode, expected_temp, _ = _COMFORT[day]
    engine = _ventilating(day, sensor_open=False)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: engine._exit_nat_vent(reason="outdoor warmed past the threshold"))
    assert engine._natural_vent_active is False, "precondition: the session ended"
    assert (expected_mode, expected_temp) in _setpoints(engine, mark)
    assert engine._grace_active is True


# ---------------------------------------------------------------------------
# 4. Dashboard Resume (the removed ``restore_mode != "off"`` guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS + _CONTROL_DAYS)
def test_dashboard_resume_restores_the_comfort_edge(day, fan):
    expected_mode, expected_temp, _ = _COMFORT[day]
    engine = _engine_for(day, fan)
    _paused_for_window(engine, was=expected_mode)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.resume_from_pause)
    assert _setpoints(engine, mark) == [(expected_mode, expected_temp)]


@pytest.mark.parametrize("day", _OFF_DAYS)
def test_dashboard_resume_on_an_off_day_never_writes_a_blind_off(day):
    """What the old guard rightly prevented: no ``set_hvac_mode("off")`` is ever sent by a resume."""
    engine = _engine_for(day, FAN_MODE_HVAC)
    _paused_for_window(engine, was="cool")
    mark = len(engine.hass.services.async_call.call_args_list)
    result = _run_at(engine, engine.resume_from_pause)
    assert result == "off", "the returned label is unchanged"
    assert not [w for w in _writes(engine, mark) if w[0] == "set_hvac_mode" and w[1].get("hvac_mode") == "off"]


# ---------------------------------------------------------------------------
# 5. The choke point itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day", _OFF_DAYS)
def test_target_override_still_bypasses_the_band_on_off_days(day):
    """TOU pre-conditioning passes its own target: an "off" label keeps writing nothing (unchanged contract)."""
    engine = _engine_for(day, FAN_MODE_HVAC)
    c = _classification(day)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: engine._set_temperature_for_mode(c, reason="tou", target_override=70.0))
    assert _setpoints(engine, mark) == []


def test_sleep_window_off_day_restore_uses_the_sleep_band():
    """The family AND the edge come from the resolved (sleep) band on an off day."""
    engine = _engine_for("warm", FAN_MODE_HVAC, sleep_time="22:00", wake_time="07:00", sleep_heat=64.0, sleep_cool=70.0)
    engine._clock.now = engine._clock.now.replace(hour=23)
    _set_indoor(engine, 69.5)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: engine._set_temperature_for_mode(_classification("warm"), reason="restore"))
    assert _setpoints(engine, mark) == [("cool", 70.0)]


# ---------------------------------------------------------------------------
# 6. Mirror 4: the nat-vent comfort-floor exit (slow loop) gated its restore on the label too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day", _OFF_DAYS + ["cold"])
def test_nat_vent_comfort_floor_exit_arms_the_floor_on_every_day_type(day):
    """Ventilation ended because the house fell to the comfort floor: the heating floor must be armed.

    Warm/mild days (label "off") skipped the restore entirely, so the cooled house sat with the fan off and the
    thermostat as ventilation left it (Off). The cold day is the control (it always restored)."""
    import asyncio

    from tests.test_fan_control import _DT_NOW_PATH, _FIXED_NOW, _PATCH_CALL_LATER, _make_nat_vent_engine

    engine = _make_nat_vent_engine(indoor_temp=70.0)
    c = _classification("mild" if day == "mild" else ("warm" if day == "warm" else "cold"))
    for attr, value in (("window_opportunity_morning", False), ("window_opportunity_evening", False)):
        setattr(c, attr, value)
    engine._current_classification = c
    with patch(_PATCH_CALL_LATER), patch(_DT_NOW_PATH, return_value=_FIXED_NOW):
        asyncio.run(engine.check_natural_vent_conditions())
    assert engine._natural_vent_active is False, "precondition: the comfort-floor exit ran"
    heat_writes = [
        call
        for call in engine.hass.services.async_call.call_args_list
        if call.args[:2] == ("climate", "set_temperature") and call.args[2].get("hvac_mode") == "heat"
    ]
    assert heat_writes, f"{day}: the comfort floor was not armed after the exit"
    assert engine._grace_active is True


@pytest.mark.parametrize("day", _OFF_DAYS)
def test_whole_house_fan_comfort_floor_exit_writes_one_mode_on_off_days(day):
    """WHF mode, comfort-floor exit on a warm/mild day: the house cooled to its floor, heat is armed in one switch.

    Pins the ``_selector_follows`` widening: with the label gate kept, ``_deactivate_fan()`` blindly replayed the
    pre-fan mode (cool) and the selector wrote heat a moment later -- a cool-then-heat double mode write (#1066 F4).
    """
    import asyncio

    from tests.test_fan_control import _DT_NOW_PATH, _FIXED_NOW, _PATCH_CALL_LATER, _make_nat_vent_engine

    engine = _make_nat_vent_engine(indoor_temp=70.0)
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "fan.whf"
    engine._pre_fan_hvac_mode = "cool"  # the WHF suppressed a cool-armed thermostat
    c = _classification(day)
    for attr in ("window_opportunity_morning", "window_opportunity_evening"):
        setattr(c, attr, False)
    engine._current_classification = c
    with patch(_PATCH_CALL_LATER), patch(_DT_NOW_PATH, return_value=_FIXED_NOW):
        asyncio.run(engine.check_natural_vent_conditions())
    assert engine._natural_vent_active is False, "precondition: the comfort-floor exit ran"
    modes = [w[1].get("hvac_mode") for w in _writes(engine)]
    assert "cool" not in modes, f"{day}: blind pre-fan cool replayed before the selector's heat: {modes}"
    assert "heat" in modes
