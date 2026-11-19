"""Issues #1605 / #1606 (mirrors found while fixing #1268 / #1597).

#1605 -- occupant framing: the house is ventilating and cools to the comfort floor with the windows shut, but the
fan-off is deferred by the 5-minute toggle floor, so the fan keeps running. The slow-loop exit nevertheless started
the "nat-vent exit resume" grace, wrote the restore (cool with the blower; blocked with a whole-house fan) and logged
"fan on->off". It now follows the #1141 rule every other exit follows (``_exit_nat_vent``): the deferral is recorded,
no grace and no restore are written, the row says the fan-off is deferred, and the retry restores once the fan stops.

#1606 -- occupant framing: when ventilation restarts from a periodic re-check (not a window opening), the Activity
row did not say what happened to the thermostat. It now carries the same ``hvac_mode_before`` / ``hvac_mode_set`` as
the window-open start row (#1597).

Real ``AutomationEngine`` + the state-following fake thermostat (``test_return_home_off_day_1208``).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS
from custom_components.climate_advisor.const import FAN_MODE_HVAC
from tests.test_natvent_row_outcome_1268 import _FLOOR_ROW, _floor_rows, _ventilating
from tests.test_return_home_off_day_1208 import _FANS, _engine_for, _run_at, _set_indoor, _setpoints, _writes

_OFF_DAYS = ["warm", "mild"]


# ---------------------------------------------------------------------------
# #1605: the slow-loop comfort-floor exit with a deferred fan-off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
def test_deferred_fan_off_at_the_comfort_floor_starts_no_grace_and_writes_no_restore(day, fan):
    engine = _ventilating(day, fan, sensor_open=False)
    engine._fan_toggle_command_time = engine._clock.now - timedelta(seconds=60)  # inside the 5-minute floor
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.check_natural_vent_conditions)
    assert engine._natural_vent_active is True and engine._fan_active is True, "precondition: the fan-off was deferred"
    assert engine._grace_active is False, "an automation grace started while the session is still live"
    assert _writes(engine, mark) == [], "a restore was written while the fan still runs"
    (row,) = _floor_rows(engine)
    assert row["hvac_restore"]["held_reason"] == "fan_off_pending"
    assert "deferred" in row["fan_mode_change"], row["fan_mode_change"]
    label, settings = EVENT_RENDERERS[_FLOOR_ROW](row, "F")
    assert "on->off" not in label and settings == "mode: unchanged (fan-off deferred, restore follows)"


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
def test_after_the_floor_the_retry_ends_the_session_and_restores_once(day, fan):
    engine = _ventilating(day, fan, sensor_open=False)
    engine._fan_toggle_command_time = engine._clock.now - timedelta(seconds=60)
    _run_at(engine, engine.check_natural_vent_conditions)
    engine._clock.advance(minutes=5)
    mark = len(engine.hass.services.async_call.call_args_list)
    _run_at(engine, engine.check_natural_vent_conditions)
    assert engine._natural_vent_active is False
    assert engine._grace_active is True
    assert ("heat", 68.0) in _setpoints(engine, mark)
    modes = [w[1].get("hvac_mode") for w in _writes(engine, mark)]
    assert "cool" not in modes, f"blind pre-fan cool replayed before the selector's heat (#1066 F4): {modes}"
    assert _floor_rows(engine)[-1]["hvac_restore"] == {"mode": "heat", "outcome": "sent"}


@pytest.mark.parametrize("fan", _FANS)
def test_repeat_deferral_is_not_reported_twice(fan):
    """RATE_LIMITED_DUP (a retry still blocked) writes no second row, like every other exit."""
    engine = _ventilating("warm", fan, sensor_open=False)
    engine._fan_toggle_command_time = engine._clock.now - timedelta(seconds=60)
    _run_at(engine, engine.check_natural_vent_conditions)
    engine._clock.advance(seconds=30)
    _run_at(engine, engine.check_natural_vent_conditions)
    assert len(_floor_rows(engine)) == 1
    assert engine._grace_active is False


# ---------------------------------------------------------------------------
# #1606: re-check start rows say what the thermostat holds
# ---------------------------------------------------------------------------


def _recheck(day, fan, path):
    engine = _engine_for(day, fan)
    engine._clock.advance(hours=1)
    engine._last_outdoor_temp = 65.0
    engine._test_state.attributes["fan_modes"] = ["auto", "on"]
    _set_indoor(engine, 73.0)
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    if path == "re_pause":
        engine._paused_by_door = True
        engine._pre_pause_mode = None
        engine._test_state.state = "off"
    handler = engine.check_natural_vent_conditions if path == "reeval" else engine._re_pause_for_open_sensor
    with patch("custom_components.climate_advisor.automation.async_call_later"):
        _run_at(engine, handler)
    (row,) = [d for k, d in engine._events if k == "sensor_opened"]
    assert row["result"] == "natural_ventilation", "precondition: ventilation restarted"
    return engine, row


@pytest.mark.parametrize("path", ["reeval", "re_pause"])
@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
def test_recheck_start_row_reports_the_thermostat_off(day, fan, path):
    engine, row = _recheck(day, fan, path)
    assert engine._test_state.state == "off"
    assert row["hvac_mode_before"] in ("heat", "cool", "off")
    expected = (
        {"mode": "off", "outcome": "sent"}
        if (fan == FAN_MODE_HVAC and row["hvac_mode_before"] != "off")
        else {"mode": "off"}
    )
    assert row["hvac_mode_set"] == expected
    _label, settings = EVENT_RENDERERS["sensor_opened"](row, "F")
    assert f"mode: {row['hvac_mode_before']}->off" in settings


def test_reeval_before_mode_is_read_right_before_the_fan_start():
    """The re-check may arm the band first; 'before' is what the thermostat held when ventilation started."""
    engine, row = _recheck("warm", FAN_MODE_HVAC, "reeval")
    assert row["hvac_mode_before"] == "cool"
