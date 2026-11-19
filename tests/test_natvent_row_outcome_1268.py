"""Issues #1268 / #1597: the nat-vent start and comfort-floor exit rows report what the thermostat was actually told.

Occupant framing:
- #1268: on a mild or warm day natural ventilation ends at the comfort floor. The Activity Report said
  "mode: off->off" or "off->cool" (the day label) while Climate Advisor armed heat at the floor -- or "off->cool"
  while a window was still open and the thermostat was correctly held Off (Rule 3).
- #1597: a window opens and ventilation starts. The row said the mode changed to "band-armed" while Climate Advisor
  turned the thermostat Off (whole-house fan suppression, or the blower hold with a window open).

The rows now carry a structured record (``hvac_restore`` on the exit, ``hvac_mode_set`` on the start) filled from
what was written, rendered through the activity catalog. Real ``AutomationEngine`` + the state-following fake
thermostat (``test_return_home_off_day_1208``); no production logic re-implemented.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS
from custom_components.climate_advisor.const import FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE
from custom_components.climate_advisor.nat_vent_exit import NatVentExitReason
from tests.test_return_home_off_day_1208 import _FANS, _engine_for, _run_at, _set_indoor, _setpoints, _writes

_DAYS = ["hot", "warm", "mild", "cold"]
_FLOOR_ROW = "nat_vent_comfort_floor_exit"
# indoor 66 F is below the 68 F comfort floor: the selector arms heat at 68 on every day type.
_INDOOR = 66.0


def _ventilating(day: str, fan: str, *, sensor_open: bool):
    engine = _engine_for(day, fan)
    engine._clock.advance(hours=1)
    if fan == FAN_MODE_WHOLE_HOUSE:
        engine._pre_fan_hvac_mode = "cool"  # the WHF suppressed a cool-armed thermostat
    engine._last_outdoor_temp = 60.0
    engine._sensor_check_callback = lambda: sensor_open
    engine._sensor_debounce_pending_callback = lambda: False
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._fan_on_since = engine._clock.now - timedelta(minutes=20)
    engine._fan_toggle_command_time = engine._clock.now - timedelta(seconds=400)  # past the 5-minute floor
    engine._nat_vent_exit_candidate_reason = NatVentExitReason.COMFORT_FLOOR
    engine._nat_vent_exit_candidate_since = engine._clock.now - timedelta(hours=1)  # already sustained
    engine._test_state.state = "off"
    _set_indoor(engine, _INDOOR)
    return engine


_ENTRY = {
    "slow_loop": lambda e: e.check_natural_vent_conditions(),
    "temp_check": lambda e: e.nat_vent_temperature_check(_INDOOR, outdoor=60.0),
    "fan_thermostat_check": lambda e: e.fan_thermostat_check(indoor=_INDOOR, outdoor=60.0, trigger="test"),
}


def _floor_rows(engine):
    return [d for k, d in engine._events if k == _FLOOR_ROW]


def _record_emit_order(engine):
    seen: list[tuple[str, int]] = []
    inner = engine._emit_event_callback

    def _emit(kind, data):
        seen.append((kind, len(engine.hass.services.async_call.call_args_list)))
        inner(kind, data)

    engine._emit_event_callback = _emit
    return seen


# ---------------------------------------------------------------------------
# 1. #1268: comfort-floor exit, windows closed -> the row names the family actually armed (every entry point)
# ---------------------------------------------------------------------------


def _closed_cases():
    for entry in _ENTRY:
        for fan in _FANS:
            # A WHF session with every window shut is force-closed by the fast loop before its comfort-floor check
            # (Issue #561), so temp_check reaches the floor exit with the windows closed only for the blower.
            if entry == "temp_check" and fan == FAN_MODE_WHOLE_HOUSE:
                continue
            for day in _DAYS:
                yield entry, fan, day


@pytest.mark.parametrize(("entry", "fan", "day"), list(_closed_cases()))
def test_closed_window_floor_exit_reports_the_armed_family(entry, fan, day):
    engine = _ventilating(day, fan, sensor_open=False)
    seen = _record_emit_order(engine)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: _ENTRY[entry](engine))
    assert engine._natural_vent_active is False, "precondition: the exit ran"
    assert ("heat", 68.0) in _setpoints(engine, mark), "precondition: the comfort floor was armed"
    (row,) = _floor_rows(engine)
    assert row["hvac_restore"] == {"mode": "heat", "outcome": "sent"}
    assert "hvac_mode_restored" not in row, "the day label is no longer reported as the restored mode"
    (calls_at_emit,) = [n for k, n in seen if k == _FLOOR_ROW]
    assert calls_at_emit == len(engine.hass.services.async_call.call_args_list), "row emitted before the restore"
    _label, settings = EVENT_RENDERERS[_FLOOR_ROW](row, "F")
    assert settings == "mode: off->heat"


# ---------------------------------------------------------------------------
# 2. #1268: window still open -> held Off, the row says so (every entry point, both fans)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day", ["warm", "mild"])
@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("entry", list(_ENTRY))
def test_open_window_floor_exit_reports_held_not_restored(entry, fan, day):
    engine = _ventilating(day, fan, sensor_open=True)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: _ENTRY[entry](engine))
    assert engine._natural_vent_active is False, "precondition: the exit ran"
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    (row,) = _floor_rows(engine)
    assert row["hvac_restore"]["held_reason"] == "window_open"
    _label, settings = EVENT_RENDERERS[_FLOOR_ROW](row, "F")
    assert settings == "mode: unchanged (window open, HVAC paused)"


# ---------------------------------------------------------------------------
# 3. #1268: the other _exit_nat_vent branches -- occupant change kept, deferred fan-off and its retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sensor_open", [False, True])
@pytest.mark.parametrize("fan", _FANS)
def test_occupant_change_kept_is_reported_held(fan, sensor_open):
    engine = _ventilating("mild", fan, sensor_open=sensor_open)
    engine._manual_override_active = True
    engine._manual_override_mode = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: _ENTRY["fan_thermostat_check"](engine))
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    (row,) = _floor_rows(engine)
    assert row["hvac_restore"]["held_reason"] == "occupant_change"
    _label, settings = EVENT_RENDERERS[_FLOOR_ROW](row, "F")
    assert settings == "mode: unchanged (occupant's change kept)"


@pytest.mark.parametrize("fan", _FANS)
def test_slow_loop_occupant_change_kept_is_reported_held(fan):
    """The inline (slow-loop) comfort-floor exit's own protected branch."""
    engine = _ventilating("warm", fan, sensor_open=False)
    engine._override_confirm_pending = "off"
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, lambda: _ENTRY["slow_loop"](engine))
    assert [w for w in _setpoints(engine, mark) if w[0] in ("heat", "cool")] == []
    (row,) = _floor_rows(engine)
    assert row["hvac_restore"]["held_reason"] == "occupant_change"


@pytest.mark.parametrize("sensor_open", [False, True])
@pytest.mark.parametrize("fan", _FANS)
def test_deferred_fan_off_reports_pending_then_the_retry_reports_the_restore(fan, sensor_open):
    engine = _ventilating("mild", fan, sensor_open=sensor_open)
    engine._fan_toggle_command_time = engine._clock.now - timedelta(seconds=60)  # inside the 5-minute floor
    _run_at(engine, lambda: _ENTRY["fan_thermostat_check"](engine))
    assert engine._natural_vent_active is True, "precondition: the fan-off was deferred, the session kept"
    (row,) = _floor_rows(engine)
    assert row["hvac_restore"]["held_reason"] == "fan_off_pending"
    _label, settings = EVENT_RENDERERS[_FLOOR_ROW](row, "F")
    assert settings == "mode: unchanged (fan-off deferred, restore follows)"
    # The floor ends: the retry (the same choke point, same payload) runs the fan-off and reports what it did.
    engine._clock.advance(minutes=5)
    _run_at(engine, lambda: _ENTRY["fan_thermostat_check"](engine))
    assert engine._natural_vent_active is False
    last = _floor_rows(engine)[-1]
    if sensor_open:
        assert last["hvac_restore"]["held_reason"] == "window_open"
    else:
        assert last["hvac_restore"] == {"mode": "heat", "outcome": "sent"}


def test_deferred_closed_exit_retry_payload_reports_the_real_restore():
    """The scheduled closed-window retry re-enters with the caller's payload: the stale 'pending' is not reused."""
    engine = _ventilating("mild", FAN_MODE_HVAC, sensor_open=False)
    engine._fan_toggle_command_time = engine._clock.now - timedelta(seconds=60)
    payload = {"indoor_temp": _INDOOR, "comfort_heat": 68.0}
    _run_at(
        engine,
        lambda: engine._exit_nat_vent(reason="test", event_type=_FLOOR_ROW, event_payload=payload),
    )
    assert _floor_rows(engine)[-1]["hvac_restore"]["held_reason"] == "fan_off_pending"
    assert "hvac_restore" not in payload, "the caller's payload (kept by the retry) must not carry the stale record"
    engine._clock.advance(minutes=5)
    _run_at(
        engine,
        lambda: engine._exit_nat_vent(reason="test", event_type=_FLOOR_ROW, event_payload=payload),
    )
    assert _floor_rows(engine)[-1]["hvac_restore"] == {"mode": "heat", "outcome": "sent"}


def test_failed_restore_is_rendered_not_sent():
    row = {"indoor_temp": 66, "comfort_heat": 68, "hvac_restore": {"mode": "heat", "outcome": "failed"}}
    _label, settings = EVENT_RENDERERS[_FLOOR_ROW](row, "F")
    assert settings == "mode: off->heat NOT sent (command failed)"


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ("heat", "mode: off->heat"),
        ("off", "mode: off->off"),
        ("unknown", ""),
        ("unchanged (window still open)", "mode: off->unchanged (window still open)"),
    ],
)
def test_historical_floor_exit_rows_keep_their_wording(old, expected):
    _label, settings = EVENT_RENDERERS[_FLOOR_ROW](
        {"indoor_temp": 66, "comfort_heat": 68, "hvac_mode_restored": old}, "F"
    )
    assert settings == expected


# ---------------------------------------------------------------------------
# 4. #1597: the nat-vent start row says what the thermostat holds
# ---------------------------------------------------------------------------


def _open_window(day: str, fan: str, *, settled: bool = True):
    engine = _engine_for(day, fan)
    engine._clock.advance(hours=1)
    engine._last_outdoor_temp = 65.0
    engine._test_state.attributes["fan_modes"] = ["auto", "on"]
    _set_indoor(engine, 73.0)
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: not settled
    before = engine._test_state.state
    with patch("custom_components.climate_advisor.automation.async_call_later"):
        _run_at(engine, lambda: engine.handle_door_window_open("binary_sensor.window"))
    (row,) = [d for k, d in engine._events if k == "sensor_opened"]
    assert row["result"] == "natural_ventilation", "precondition: ventilation started"
    return engine, row, before


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _DAYS)
def test_nat_vent_start_reports_the_thermostat_turned_off(day, fan):
    engine, row, before = _open_window(day, fan)
    assert before in ("heat", "cool") and engine._test_state.state == "off", "precondition: CA turned it Off"
    assert "hvac_mode_change" not in row, "no more '->band-armed'"
    assert row["hvac_mode_before"] == before
    if fan == FAN_MODE_HVAC:  # the blower hold wrote the Off itself: its result is reported
        assert row["hvac_mode_set"] == {"mode": "off", "outcome": "sent"}
    else:  # the whole-house fan's suppression wrote it (ownership = Off actually sent, #1293)
        assert row["hvac_mode_set"] == {"mode": "off"}
    _label, settings = EVENT_RENDERERS["sensor_opened"](row, "F")
    assert f"mode: {before}->off" in settings and "band-armed" not in settings


def test_blower_start_whose_off_failed_says_not_sent():
    from unittest.mock import AsyncMock

    from custom_components.climate_advisor.automation import HvacCommandResult

    engine = _engine_for("warm", FAN_MODE_HVAC)
    engine._set_hvac_mode = AsyncMock(return_value=HvacCommandResult.FAILED)
    engine._clock.advance(hours=1)
    engine._last_outdoor_temp = 65.0
    engine._test_state.attributes["fan_modes"] = ["auto", "on"]
    _set_indoor(engine, 73.0)
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    with patch("custom_components.climate_advisor.automation.async_call_later"):
        _run_at(engine, lambda: engine.handle_door_window_open("binary_sensor.window"))
    (row,) = [d for k, d in engine._events if k == "sensor_opened"]
    assert row["hvac_mode_set"] == {"mode": "off", "outcome": "failed"}
    _label, settings = EVENT_RENDERERS["sensor_opened"](row, "F")
    assert "mode: cool->off NOT sent (command failed)" in settings


@pytest.mark.parametrize("day", _DAYS)
def test_blower_start_with_the_window_inside_its_delay_reports_unchanged(day):
    engine, row, before = _open_window(day, FAN_MODE_HVAC, settled=False)
    assert engine._test_state.state == before, "precondition: nothing written"
    assert row["hvac_mode_set"]["held_reason"] == "no_window_settled"
    _label, settings = EVENT_RENDERERS["sensor_opened"](row, "F")
    assert "mode: unchanged (no window open past its delay)" in settings


def test_blower_hold_leaves_a_protected_occupant_change_and_says_so():
    engine = _ventilating("warm", FAN_MODE_HVAC, sensor_open=True)
    engine._test_state.state = "cool"
    engine._manual_override_active = True
    mark = len(engine.hass.services.async_call.call_args_list)
    hold = _run_at(engine, engine._apply_nat_vent_hvac_state)
    assert _writes(engine, mark) == []
    assert hold.held_reason == "occupant_change"


def test_whf_start_without_ownership_reports_fan_not_started():
    engine = _ventilating("warm", FAN_MODE_WHOLE_HOUSE, sensor_open=True)
    engine._pre_fan_hvac_mode = None  # the fan session took no ownership (e.g. observe-only, #1293)
    hold = _run_at(engine, engine._apply_nat_vent_hvac_state)
    assert hold.held_reason == "fan_not_started"


@pytest.mark.parametrize(
    ("hvac_mode_change", "expected"),
    [("off→band-armed", "mode: off→band-armed"), ("heat→off", "mode: heat→off")],
)
def test_historical_sensor_opened_rows_keep_their_wording(hvac_mode_change, expected):
    _label, settings = EVENT_RENDERERS["sensor_opened"](
        {"entity": "binary_sensor.window", "result": "natural_ventilation", "hvac_mode_change": hvac_mode_change}, "F"
    )
    assert expected in settings
