"""Issue #1142: one owner for "ventilate or pause" an open door/window; blower mode never heats/cools into it.

Occupant stories, each pinned with the REAL ``AutomationEngine`` and the state-following fake thermostat
(``tests.test_restore_resolved_band``); nothing re-implements production logic.

* D1  Whole-house-fan home, hot evening: the window finishes its debounce and the occupant gets an "HVAC paused"
      message, then seconds later the fan starts and the pause clears. AC was off either way.
* D2  A scheduled routine pauses the HVAC and notifies; the next check starts the fan and clears the pause.
* D3  An automation grace should let ventilation decide, but a forecast heat spike paused the window inside it.
* D4  Blower (``hvac_fan``) home: ventilation ends with a window still open and the thermostat stays armed to
      cool (or heat) into the open window. Closing the window must not flap modes.
* D7  Blower home: ventilation arms the occupied comfort band (AC assist) with the window open; away it writes
      ``cool`` at the occupied ceiling, below the away setback.
* D8  Whole-house-fan home, away, window open: the away-ceiling exit stops the fan and sends ``cool`` at the
      comfort ceiling with the window still open.

Strict ``xfail``: each fix flips its own test (the marker is removed in the commit that fixes it).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    OCCUPANCY_AWAY,
)
from tests.test_door_pause_recorded_1132 import (
    _AUTOMATION,
    _DAY,
    _SENSOR,
    _engine,
    _forecast_peak_ahead,
    _run,
)
from tests.test_restore_resolved_band import _classification, _make_engine

_AWAY_SETBACK_COOL = 80.0

_XF_D1 = pytest.mark.xfail(strict=True, reason="#1142 D1: handler pauses before a soft start")
_XF_D2 = pytest.mark.xfail(strict=True, reason="#1142 D2: routine pause notifies although a start is due")
_XF_D3 = pytest.mark.xfail(strict=True, reason="#1142 D3: grace pre-check and nat-vent FSM disagree")
_XF_D4 = pytest.mark.xfail(strict=True, reason="#1142 D4: blower exit with a window open leaves the thermostat armed")
_XF_D7 = pytest.mark.xfail(strict=True, reason="#1142 D7: blower ventilation arms the occupied band")
_XF_D8 = pytest.mark.xfail(strict=True, reason="#1142 D8: away-ceiling exit sends cool with the window open")


def _blower_engine(
    *,
    mode: str = "cool",
    indoor: float = 72.0,
    outdoor: float = 65.0,
    away: bool = False,
    day: str = "mild",
    **cfg,
) -> AutomationEngine:
    """``hvac_fan`` install on a mild day: the nat-vent entry gate is satisfied (outdoor < indoor)."""
    engine = _make_engine(indoor=indoor, mode=mode, **{CONF_FAN_MODE: FAN_MODE_HVAC, **cfg})
    engine._last_outdoor_temp = outdoor
    engine._hourly_forecast_temps = []
    engine._current_classification = _classification(day)
    engine._test_state.attributes.update({"fan_modes": ["auto", "on"], "fan_mode": "auto"})
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: False
    if away:
        engine._occupancy_mode = OCCUPANCY_AWAY
    return engine


def _at(coro_fn, seconds: float = 0.0):
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY + timedelta(seconds=seconds)),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        return _run(coro_fn())


def _open(engine) -> None:
    _at(lambda: engine.handle_door_window_open(_SENSOR))


def _tick(engine, seconds: float) -> None:
    _at(lambda: engine.check_natural_vent_conditions(), seconds)


def _close(engine, seconds: float = 900.0) -> None:
    engine._sensor_check_callback = lambda: False
    _at(lambda: engine.handle_all_doors_windows_closed(), seconds)


def _notes(engine) -> AsyncMock:
    engine._notify = AsyncMock()
    return engine._notify


def _climate_calls(engine, since: int = 0) -> list[tuple[str, dict]]:
    out = []
    for call in engine.hass.services.async_call.call_args_list[since:]:
        domain, service, data = call.args[0], call.args[1], call.args[2]
        if domain == "climate":
            out.append((service, {k: v for k, v in data.items() if k != "entity_id"}))
    return out


def _mode_writes(calls) -> list[str]:
    modes = []
    for service, data in calls:
        if service in ("set_hvac_mode", "set_temperature") and "hvac_mode" in data:
            modes.append(data["hvac_mode"])
    return modes


def _below_away_setback(calls) -> list[tuple[str, dict]]:
    return [
        (s, d)
        for s, d in calls
        if s == "set_temperature" and d.get("hvac_mode") == "cool" and d.get("temperature", 999) < _AWAY_SETBACK_COOL
    ]


def _whf_soft_start_engine() -> AutomationEngine:
    """Hot evening, whole-house fan: outdoor 79 is above the full-gate threshold but below indoor 80 and past
    today's 85 peak, so only the soft start qualifies."""
    engine = _engine(mode="cool", indoor=80.0, outdoor=79.0)
    engine._outdoor_temp_today_peak = 85.0
    engine._outdoor_temp_today_sample_count = 20
    return engine


def _paused_blower_engine(*, away: bool) -> AutomationEngine:
    engine = _blower_engine(mode="off", away=away)
    engine._paused_by_door = True
    engine._paused_with_hvac_already_off = False
    engine._pre_pause_mode = "cool"
    engine._paused_entity = _SENSOR
    engine._paused_since = _DAY - timedelta(minutes=20)
    return engine


# ---------------------------------------------------------------------------
# D1 -- soft start at the window-open handler
# ---------------------------------------------------------------------------


class TestD1SoftStartAtHandler:
    @_XF_D1
    def test_soft_start_is_started_not_paused_first(self):
        engine = _whf_soft_start_engine()
        notify = _notes(engine)
        _open(engine)
        assert engine._natural_vent_active is True, "the fan session starts straight from the window-open handler"
        assert engine._nat_vent_soft_start is True
        assert engine._paused_by_door is False
        assert not notify.called, "no 'HVAC paused' message for a pause that is cancelled seconds later"

    def test_control_soft_start_disabled_still_pauses(self):
        engine = _whf_soft_start_engine()
        engine.config["nat_vent_soft_start_enabled"] = False
        _notes(engine)
        _open(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"


# ---------------------------------------------------------------------------
# D2 -- routine re-check / comfort-band guard pause quietly when a fan session is about to take over
# ---------------------------------------------------------------------------


class TestD2QuietPause:
    @_XF_D2
    def test_sync_recheck_pauses_without_notifying_when_a_start_is_due(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        notify = _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is True, "rule 3: the thermostat is still switched off for the open window"
        assert engine._test_state.state == "off"
        assert not notify.called, "no false 'HVAC paused' message: the fan takes over on the next check"
        _tick(engine, 60)
        assert engine._natural_vent_active is True and engine._paused_by_door is False

    def test_control_sync_recheck_still_notifies_when_the_start_is_vetoed(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _forecast_peak_ahead(engine)
        notify = _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is True
        assert notify.called, "a pause that stays is announced"

    @_XF_D2
    def test_comfort_band_guard_pauses_without_notifying_when_a_start_is_due(self):
        from custom_components.climate_advisor.automation import ComfortBand  # noqa: PLC0415

        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        notify = _notes(engine)
        band = ComfortBand(floor=68.0, ceiling=74.0, active="ceiling", reason="test")
        _at(lambda: engine._apply_comfort_band(band, reason="test"))
        assert engine._paused_by_door is True
        assert not notify.called


# ---------------------------------------------------------------------------
# D3 -- the grace pre-check and the nat-vent FSM must agree
# ---------------------------------------------------------------------------


class TestD3GraceAgreement:
    @_XF_D3
    def test_forecast_veto_inside_automation_grace_does_not_pause(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _forecast_peak_ahead(engine)
        engine._grace_active = True
        engine._last_grace_trigger = "automation"
        engine._last_resume_source = "automation"
        notify = _notes(engine)
        _open(engine)
        assert engine._paused_by_door is False, "the grace holds the window; the grace-expiry re-check decides"
        assert engine._test_state.state == "cool"
        assert not notify.called

    def test_control_gate_failure_inside_grace_does_not_pause(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=85.0)
        engine._grace_active = True
        engine._last_grace_trigger = "automation"
        engine._last_resume_source = "automation"
        _open(engine)
        assert engine._paused_by_door is False
        assert engine._test_state.state == "cool"


# ---------------------------------------------------------------------------
# D4 -- blower: ventilation ends with a window open -> thermostat Off now, no flap on close
# ---------------------------------------------------------------------------


@_XF_D4
class TestD4BlowerExitWithWindowOpen:
    def test_cool_side_outdoor_rise_exit_leaves_thermostat_off(self):
        engine = _blower_engine()
        _open(engine)
        assert engine._natural_vent_active is True, "precondition: blower ventilation started"
        engine._last_outdoor_temp = 80.0
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True, "precondition: the window pause is recorded"
        assert engine._test_state.state == "off", "rule 3: not left armed to cool into the open window"

    def test_cool_side_close_after_exit_does_not_flap(self):
        engine = _blower_engine()
        _open(engine)
        engine._last_outdoor_temp = 80.0
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._test_state.state == "off", "precondition"
        before = len(_climate_calls(engine))
        _close(engine)
        writes = _mode_writes(_climate_calls(engine, before))
        assert len(set(writes)) <= 1, f"mode flapped on close: {writes}"
        assert engine._test_state.state == "cool"

    def test_heat_side_floor_exit_leaves_thermostat_off(self):
        engine = _blower_engine(mode="heat", indoor=72.5, outdoor=56.0, day="cool")
        _open(engine)
        assert engine._natural_vent_active is True, "precondition: blower ventilation started"
        engine._test_state.attributes["current_temperature"] = 66.0
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._natural_vent_active is False
        assert engine._test_state.state == "off", "rule 3: the furnace must not heat into the open window"
        assert engine._paused_by_door is True

    def test_heat_side_close_after_exit_does_not_flap(self):
        engine = _blower_engine(mode="heat", indoor=72.5, outdoor=56.0, day="cool")
        _open(engine)
        engine._test_state.attributes["current_temperature"] = 66.0
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._test_state.state == "off", "precondition"
        before = len(_climate_calls(engine))
        _close(engine)
        writes = _mode_writes(_climate_calls(engine, before))
        assert len(set(writes)) <= 1, f"mode flapped on close: {writes}"
        assert engine._test_state.state == "heat"


# ---------------------------------------------------------------------------
# D7 -- blower: no heat/AC while ventilating with the window open (home and away)
# ---------------------------------------------------------------------------


@_XF_D7
class TestD7BlowerNeverArmsHvacWithOpenWindow:
    @pytest.mark.parametrize("mode", ["cool", "heat", "off"])
    def test_start_on_open_leaves_thermostat_off(self, mode):
        engine = _blower_engine(mode=mode)
        _open(engine)
        assert engine._natural_vent_active is True
        assert engine._test_state.state == "off", "no occupied band armed (AC assist) with the window open"
        assert not [c for c in _climate_calls(engine) if c[0] == "set_temperature"]

    def test_start_on_open_while_away_writes_no_cool_below_the_setback(self):
        engine = _blower_engine(away=True)
        _open(engine)
        assert engine._natural_vent_active is True
        assert _below_away_setback(_climate_calls(engine)) == []
        assert engine._test_state.state == "off"

    def test_reactivation_after_pause_while_away_writes_no_cool_below_the_setback(self):
        engine = _paused_blower_engine(away=True)
        _tick(engine, 0)
        assert engine._natural_vent_active is True, "precondition: ventilation resumed"
        assert _below_away_setback(_climate_calls(engine)) == []
        assert engine._test_state.state == "off"

    def test_reactivation_after_pause_at_home_leaves_thermostat_off(self):
        engine = _paused_blower_engine(away=False)
        _tick(engine, 0)
        assert engine._natural_vent_active is True
        assert engine._test_state.state == "off"

    def test_grace_expiry_reactivation_while_away_writes_no_cool_below_the_setback(self):
        engine = _paused_blower_engine(away=True)
        _at(lambda: engine._re_pause_for_open_sensor())
        assert engine._natural_vent_active is True, "precondition: ventilation resumed at grace expiry"
        assert _below_away_setback(_climate_calls(engine)) == []
        assert engine._test_state.state == "off"

    def test_thermostat_cycle_does_not_rearm_the_band_during_ventilation(self):
        engine = _blower_engine()
        _open(engine)
        assert engine._natural_vent_active is True
        _at(lambda: engine.apply_classification(_classification("mild")), 1800)
        assert engine._test_state.state == "off", "the 30-minute cycle must not re-arm cooling through the window"


# ---------------------------------------------------------------------------
# D8 -- away-ceiling exit with a window open
# ---------------------------------------------------------------------------


@_XF_D8
class TestD8AwayCeilingExitWithWindowOpen:
    @pytest.mark.parametrize("fan_mode", [FAN_MODE_WHOLE_HOUSE, FAN_MODE_HVAC])
    def test_no_cool_while_the_window_is_open(self, fan_mode):
        engine = _blower_engine(away=True, **{CONF_FAN_MODE: fan_mode})
        _open(engine)
        assert engine._natural_vent_active is True, "precondition"
        engine._test_state.attributes["current_temperature"] = 75.0
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._natural_vent_active is False, "precondition: the away-ceiling exit ran"
        assert engine._test_state.state == "off", "rule 3: no cool at the comfort setpoint with the window open"
        assert engine._paused_by_door is True, "the same pause the outdoor-rise exit records"

    def test_away_setback_lands_when_the_window_closes(self):
        engine = _blower_engine(away=True, **{CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE})
        _open(engine)
        engine._test_state.attributes["current_temperature"] = 75.0
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._test_state.state == "off", "precondition"
        before = len(_climate_calls(engine))
        _close(engine)
        calls = _climate_calls(engine, before)
        assert _below_away_setback(calls) == []
        assert any(s == "set_temperature" and d.get("temperature", 0) >= _AWAY_SETBACK_COOL for s, d in calls), calls
