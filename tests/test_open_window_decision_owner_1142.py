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

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    OCCUPANCY_AWAY,
)
from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState
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
    engine.inputs.hourly_forecast = []
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
    engine.inputs.today_peak = 85.0
    engine.inputs.today_sample_count = 20
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
    """Owner decision (rule 3): a grace Climate Advisor started itself never holds an open window. Inside it the
    window gets the same answer as outside: start when the nat-vent FSM says so, otherwise pause."""

    def _automation_grace(self, engine):
        engine._grace_active = True
        engine._last_grace_trigger = "automation"
        engine._last_resume_source = "automation"

    def test_forecast_veto_inside_automation_grace_pauses(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _forecast_peak_ahead(engine)
        self._automation_grace(engine)
        notify = _notes(engine)
        _open(engine)
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"
        assert notify.called, "a pause that stays is announced"

    def test_gate_failure_inside_automation_grace_pauses(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=85.0)
        self._automation_grace(engine)
        _open(engine)
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_a_start_inside_automation_grace_still_starts(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        self._automation_grace(engine)
        notify = _notes(engine)
        _open(engine)
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False
        assert not notify.called

    def test_control_manual_grace_with_a_gate_failure_still_holds(self):
        """The occupant-side grace keeps its behaviour: a dashboard-resume manual grace holds the pause."""
        engine = _engine(mode="cool", indoor=72.0, outdoor=85.0)
        engine._grace_active = True
        engine._last_grace_trigger = "manual"
        engine._last_resume_source = "manual"
        _open(engine)
        assert engine._paused_by_door is False
        assert engine._test_state.state == "cool"

    def test_grace_expiry_recheck_agrees_with_the_handler(self):
        """The mirror: with the same forecast veto, the grace-expiry re-check ends in the same pause."""
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _forecast_peak_ahead(engine)
        self._automation_grace(engine)
        _open(engine)
        handler_state = (engine._paused_by_door, engine._test_state.state)
        engine2 = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _forecast_peak_ahead(engine2)
        engine2._grace_active = False
        _at(lambda: engine2._re_pause_for_open_sensor())
        assert (engine2._paused_by_door, engine2._test_state.state) == handler_state == (True, "off")


# ---------------------------------------------------------------------------
# D4 -- blower: ventilation ends with a window open -> thermostat Off now, no flap on close
# ---------------------------------------------------------------------------


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
        engine = _blower_engine(day="hot")  # a cool-classified day: closing the window re-arms cooling
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
        engine = _blower_engine(mode="heat", indoor=72.5, outdoor=56.0, day="cold")  # a heat-classified day
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


# ---------------------------------------------------------------------------
# Step 2 -- soft start at the handler and the grace-expiry re-check: edges around D1
# ---------------------------------------------------------------------------


def _record(engine) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda name, data: events.append((name, data))
    return events


class TestSoftStartEdges:
    def test_grace_expiry_recheck_starts_a_soft_start_session(self):
        engine = _whf_soft_start_engine()
        engine._paused_by_door = True
        engine._paused_with_hvac_already_off = False
        engine._pre_pause_mode = "cool"
        events = _record(engine)
        _at(lambda: engine._re_pause_for_open_sensor())
        assert engine._natural_vent_active is True and engine._nat_vent_soft_start is True
        assert engine._paused_by_door is False
        assert "nat_vent_soft_start_entered" in [name for name, _ in events]

    def test_handler_reports_the_soft_start_before_the_window_event(self):
        engine = _whf_soft_start_engine()
        events = _record(engine)
        _open(engine)
        names = [name for name, _ in events]
        assert names.index("nat_vent_soft_start_entered") < names.index("sensor_opened")
        assert events[-1][0] == "sensor_opened"

    def test_second_window_does_not_demote_a_live_soft_session(self):
        engine = _whf_soft_start_engine()
        _open(engine)
        assert engine._nat_vent_soft_start is True, "precondition"
        engine._last_outdoor_temp = 70.0  # the full gate would now qualify too
        engine._test_state.attributes["current_temperature"] = 80.0
        _open(engine)
        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True, "a second window is not a start decision"

    def test_control_blower_install_has_no_soft_start(self):
        engine = _blower_engine(indoor=80.0, outdoor=79.0)
        engine.inputs.today_peak = 85.0
        engine.inputs.today_sample_count = 20
        _open(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True

    def test_soft_start_is_still_vetoed_by_the_forecast_guard(self):
        engine = _whf_soft_start_engine()
        _forecast_peak_ahead(engine)
        notify = _notes(engine)
        _open(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert notify.called, "a pause that stays is announced"


# ---------------------------------------------------------------------------
# Step 3 -- the grace-expiry prediction is the nat-vent FSM's own answer (flags right after _on_grace_expired,
# BEFORE the re-check task runs)
# ---------------------------------------------------------------------------


def _expire_grace(engine) -> None:
    engine._grace_active = True
    engine._last_grace_trigger = "automation"
    engine._last_resume_source = "automation"
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        engine._on_grace_expired("automation", 300, False)


class TestGraceExpiryPredictionIsTheFsmsAnswer:
    def test_plain_gate_predicts_no_pause(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _expire_grace(engine)
        assert engine._paused_by_door is False

    def test_soft_start_predicts_no_pause(self):
        engine = _whf_soft_start_engine()
        _expire_grace(engine)
        assert engine._paused_by_door is False

    def test_forecast_veto_predicts_the_pause(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _forecast_peak_ahead(engine)
        _expire_grace(engine)
        assert engine._paused_by_door is True, "a vetoed start is a pause from the first instant"

    def test_no_working_fan_predicts_the_pause(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        engine.config.pop(CONF_FAN_MODE)
        _expire_grace(engine)
        assert engine._paused_by_door is True

    def test_manual_override_being_cleared_does_not_block_the_prediction(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        engine._manual_override_active = True
        engine._manual_override_mode = "cool"
        _expire_grace(engine)
        assert engine._paused_by_door is False, "the re-check runs after the override is cleared"


# ---------------------------------------------------------------------------
# Step 4 -- the quiet pause: symmetry (close restores) and the cases that must stay loud
# ---------------------------------------------------------------------------


class TestQuietPauseEdges:
    def test_closing_the_window_after_a_quiet_pause_restores_the_mode(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is True and engine._test_state.state == "off", "precondition"
        _close(engine)
        assert engine._paused_by_door is False
        assert engine._test_state.state == "cool", "the quiet pause restores exactly like a loud one"

    def test_a_locked_out_start_is_not_quiet(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        engine._nat_vent_outdoor_exit_time = _DAY  # an exit just now: the reactivation lockout holds
        notify = _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is True
        assert notify.called, "no fan session will take over inside the lockout, so the pause is announced"

    def test_no_fan_install_pause_is_announced(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        engine.config.pop(CONF_FAN_MODE)
        notify = _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert notify.called

    def test_the_quiet_pause_still_reports_the_window_event(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        events = _record(engine)
        _notes(engine)
        _at(lambda: engine._sync_paused_by_door_with_live_sensors())
        assert (
            "sensor_opened",
            {"entity": "monitored door/window", "result": "paused", "hvac_mode_change": "cool→off"},
        ) in events


# ---------------------------------------------------------------------------
# Step 5 -- blower mode edges: guidance, savings mode, the occupant's own change, the whole-house-fan mirror
# ---------------------------------------------------------------------------


def _warm_up(engine, temp: float = 76.0) -> None:
    engine._test_state.attributes["current_temperature"] = temp


class TestCloseTheWindowsGuidance:
    def test_blower_session_above_the_ceiling_asks_once_in_plain_words(self):
        engine = _blower_engine()
        notify = _notes(engine)
        events = _record(engine)
        _open(engine)
        assert not notify.called, "precondition: 72F is under the 74F ceiling, nothing to say yet"
        _warm_up(engine)
        for seconds in (60, 120, 180, 240):
            _tick(engine, seconds)
        assert notify.await_count == 1, "one message per ventilation session, not one per check"
        message, title = notify.call_args.args[0], notify.call_args.args[1]
        assert "close the windows" in message.lower() and "close the windows" in title.lower()
        assert "76" in message and "74" in message
        assert engine._test_state.state == "off", "no air conditioning while the window is open"
        assert [name for name, _ in events].count("close_windows_advised") == 1
        assert notify.call_args.kwargs.get("email") is False, "push only, no email"

    def test_below_the_ceiling_says_nothing(self):
        engine = _blower_engine()
        notify = _notes(engine)
        _open(engine)
        _warm_up(engine, 73.5)
        _tick(engine, 60)
        assert not notify.called

    def test_whole_house_fan_never_gets_this_advice(self):
        engine = _blower_engine(**{CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE})
        notify = _notes(engine)
        _open(engine)
        _warm_up(engine)
        _tick(engine, 60)
        assert not notify.called, "the fan keeps the house; the blower-mode advice is not for it"

    def test_a_new_session_may_advise_again(self):
        engine = _blower_engine()
        notify = _notes(engine)
        _open(engine)
        _warm_up(engine)
        _tick(engine, 60)
        assert notify.await_count == 1
        engine._end_nat_vent_session(FanCommandResult.EXECUTED)
        engine._apply_nat_vent_fsm_state(NatVentLifecycleState.ACTIVE_FULL_GATE)
        _tick(engine, 120)
        assert notify.await_count == 2


class TestBlowerEdges:
    def test_savings_mode_also_holds_the_thermostat_off(self):
        engine = _blower_engine(aggressive_savings=True)
        _open(engine)
        assert engine._natural_vent_active is True
        assert engine._test_state.state == "off"

    def test_the_occupants_own_thermostat_change_is_not_overwritten(self):
        engine = _blower_engine()
        _open(engine)
        assert engine._test_state.state == "off", "precondition"
        engine._manual_override_active = True
        engine._manual_override_mode = "cool"
        engine._test_state.state = "cool"
        before = len(_climate_calls(engine))
        _at(lambda: engine._apply_nat_vent_hvac_state())
        assert engine._test_state.state == "cool"
        assert _climate_calls(engine, before) == []

    def test_exit_with_the_window_open_while_the_thermostat_was_re_armed_is_switched_off(self):
        """Defence in depth: whatever armed the thermostat during the session, the exit that records the window
        pause leaves it Off at that moment."""
        engine = _blower_engine()
        _open(engine)
        engine._test_state.state = "cool"  # something re-armed it
        engine._last_outdoor_temp = 80.0
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_whole_house_fan_floor_exit_with_the_window_open_pauses_instead_of_restoring_cool(self):
        """Mirror found while proving D4: the slow-loop comfort-floor exit wrote `cool` into the open window and
        recorded no pause (also for the whole-house fan)."""
        engine = _blower_engine(indoor=72.0, outdoor=60.0, **{CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE})
        _open(engine)
        assert engine._natural_vent_active is True, "precondition"
        _warm_up(engine, 66.0)
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_floor_exit_with_the_window_closed_still_restores_comfort(self):
        """Control: only an OPEN window changes the exit."""
        engine = _blower_engine(mode="heat", indoor=72.5, outdoor=56.0, day="cold")
        _open(engine)
        engine._sensor_check_callback = lambda: False
        _warm_up(engine, 66.0)
        for seconds in (60, 200, 400):
            _tick(engine, seconds)
        assert engine._paused_by_door is False
        assert engine._test_state.state == "heat"


# ---------------------------------------------------------------------------
# #1205 x #1142: the occupant's fan-off hold beats every start, soft start included
# ---------------------------------------------------------------------------


class TestFanOffHoldStillBeatsSoftStart:
    def _held(self, engine):
        engine._fan_off_hold_until = _DAY + timedelta(minutes=30)
        return engine

    def test_handler_does_not_soft_start_over_the_hold(self):
        engine = self._held(_whf_soft_start_engine())
        _notes(engine)
        _open(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True

    def test_grace_recheck_does_not_soft_start_over_the_hold(self):
        engine = self._held(_whf_soft_start_engine())
        engine._paused_by_door = True
        engine._paused_with_hvac_already_off = False
        engine._pre_pause_mode = "cool"
        _at(lambda: engine._re_pause_for_open_sensor())
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True

    def test_paused_reactivation_does_not_soft_start_over_the_hold(self):
        engine = self._held(_whf_soft_start_engine())
        engine._paused_by_door = True
        engine._paused_with_hvac_already_off = False
        engine._pre_pause_mode = "cool"
        _tick(engine, 0)
        assert engine._natural_vent_active is False


# ---------------------------------------------------------------------------
# Away / vacation setback must not re-arm heat or cool through an open window during blower ventilation
# ---------------------------------------------------------------------------


class TestAwayCycleKeepsTheBlowerSessionOff:
    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    @pytest.mark.parametrize("day", ["hot", "cold"], ids=["cool_day", "heat_day"])
    def test_30_minute_cycle_does_not_arm_a_setback_with_the_window_open(self, occupancy, day):
        engine = _blower_engine(mode="cool" if day == "hot" else "heat", day=day)
        engine._occupancy_mode = occupancy
        _open(engine)
        assert engine._natural_vent_active is True, "precondition"
        assert engine._test_state.state == "off", "precondition"
        before = len(_climate_calls(engine))
        _at(lambda: engine.apply_classification(_classification(day)), 1800)
        assert engine._test_state.state == "off", "no setback band (cool 80 / heat 62) with the window open"
        assert _mode_writes(_climate_calls(engine, before)) == []

    def test_away_setback_lands_when_the_window_closes(self):
        engine = _blower_engine(day="hot")
        engine._occupancy_mode = "away"
        _open(engine)
        _at(lambda: engine.apply_classification(_classification("hot")), 1800)
        assert engine._test_state.state == "off", "precondition"
        before = len(_climate_calls(engine))
        _close(engine)
        calls = _climate_calls(engine, before)
        assert any(s == "set_temperature" and d.get("temperature") >= _AWAY_SETBACK_COOL for s, d in calls), calls
        assert _below_away_setback(calls) == []

    def test_a_session_with_every_window_closed_still_arms_the_setback(self):
        """Control: the guard needs a window holding open."""
        engine = _blower_engine(day="hot")
        engine._occupancy_mode = "away"
        _open(engine)
        engine._sensor_check_callback = lambda: False
        _at(lambda: engine.apply_classification(_classification("hot")), 1800)
        assert engine._test_state.state != "off" or not engine._natural_vent_active


class TestBlowerActivePauseBranchStaysReachable:
    """#1205 x #1142: the blower's thermostat is Off while it ventilates, so after a SESSION the pause is the idle
    shape. The ACTIVE pause (a running cycle interrupted, restore target recorded) is still reached when the window
    opens over an armed thermostat and a start is refused -- here the occupant's fan-off hold -- and its paused-branch
    reactivation must still start the blower once the hold ends."""

    def test_window_over_an_armed_thermostat_with_the_fan_off_hold_is_an_active_pause_then_reactivates(self):
        engine = _blower_engine(mode="cool")
        engine._fan_off_hold_until = _DAY + timedelta(minutes=30)
        _notes(engine)
        _open(engine)
        assert engine._paused_by_door is True
        assert engine._paused_with_hvac_already_off is False, "active pause: HVAC was running"
        assert engine._pre_pause_mode == "cool"
        assert engine._test_state.state == "off"

        engine._fan_off_hold_until = None  # the hold ends with the window still open
        _tick(engine, 1900)
        assert engine._natural_vent_active is True, "the paused-branch reactivation starts the blower"
        assert engine._paused_by_door is False
        assert engine._test_state.state == "off", "and holds the thermostat Off"
