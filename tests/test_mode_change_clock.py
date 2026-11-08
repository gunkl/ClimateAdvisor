"""Issue #1073 (S3 + S4): one mode-change clock, one "last ran" source.

Occupant framing: after the thermostat is switched by ANY writer (a TOU pre-cool, a door-close
restore, the ODE ceiling guard, or the occupant's own change), the comfort-edge selector must not
flip it the other way a few minutes later for a small overshoot (furnace right after the AC), and a
Home Assistant restart must not forget either the mode change or the fact that the AC ran 10
minutes ago. A deep breach still switches immediately (R1).

Real ``AutomationEngine`` against a fake thermostat that follows the engine's commands. Nothing
here re-implements production logic: the lock is read through the real selector preview, the
clock through the real ``get_serializable_state()``/``restore_state()`` path.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

_AUTOMATION = "custom_components.climate_advisor.automation"
_T0 = datetime(2026, 10, 11, 14, 0, 0, tzinfo=UTC)
_BOTH = ["off", "heat", "cool"]


def _consume_coroutine(coro):
    coro.close()


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def _make_engine(*, indoor: float | None, mode: str) -> AutomationEngine:
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    state = MagicMock()
    state.state = mode
    state.attributes = {"hvac_modes": list(_BOTH), "supported_features": 1, "current_temperature": indoor}
    hass.states.get.return_value = state

    async def _call(domain, service, data, **_kw):
        if domain != "climate":
            return None
        if service == "set_hvac_mode":
            state.state = data["hvac_mode"]
        elif service == "set_temperature":
            if "hvac_mode" in data:
                state.state = data["hvac_mode"]
            state.attributes["temperature"] = data["temperature"]
        return None

    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_call)
    config = {
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "setback_heat": 62.0,
        "setback_cool": 80.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    engine.inputs.thermal = {}
    engine._test_state = state
    return engine


def _classification(day_type: str, hvac_mode: str) -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 95.0
    obj.today_low = 70.0
    obj.tomorrow_high = 95.0
    obj.tomorrow_low = 70.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _preview(engine: AutomationEngine, clock: _Clock, indoor: float, day_type: str = "mild"):
    """The real selector preview at ``indoor`` (band 68-74).

    Issue #1073: a margin-zone switch AGAINST the day's grain right after a deliberate command of the
    family being left is HELD by the settling guard (not locked out). The dwell-lock tests therefore use
    the day's NATIVE side (``cold`` -> heat, ``hot`` -> cool), which the guard never delays.
    """
    _w, result = engine._comfort_family_would_switch(
        "cool", floor=68.0, ceiling=74.0, now=clock.now, day_type=day_type, indoor=indoor
    )
    return result


def _reasons(engine: AutomationEngine) -> list[tuple[str, str]]:
    return [(c["to_mode"], c["reason"]) for c in engine.comfort_mode_changes]


# ---------------------------------------------------------------------------
# (1) every writer arms the lock; (2) a breach still switches immediately
# ---------------------------------------------------------------------------


class TestEveryWriterArmsTheLock:
    def test_negative_control_no_prior_mode_change_means_the_switch_is_allowed(self):
        engine = _make_engine(indoor=69.5, mode="cool")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            result = _preview(engine, clock, 69.5)  # heat margin zone, thermostat cooling
        assert result.changed and not result.locked_out
        assert result.to_state.value == "heating"

    def test_tou_precondition_write_locks_a_following_non_breach_switch(self):
        engine = _make_engine(indoor=71.0, mode="off")
        clock = _Clock(_T0)
        c = _classification("hot", "cool")
        engine._current_classification = c
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_tou_precondition(c, 64.0, "s1"))
            assert engine._test_state.state == "cool"
            assert ("cool", "tou") in _reasons(engine)
            clock.advance(minutes=2)
            result = _preview(engine, clock, 69.5, "cold")  # heat is native on a cold day: only the lock applies
        assert result.locked_out is True
        assert result.to_state.value == "cooling"  # held

    def test_restore_write_locks_a_following_non_breach_switch(self):
        engine = _make_engine(indoor=73.0, mode="cool")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            with engine._mode_change_context("restore"):
                _run(engine._set_hvac_mode("heat", reason="door/window closed - restoring"))
            assert ("heat", "restore") in _reasons(engine)
            clock.advance(minutes=3)
            result = _preview(engine, clock, 73.0)  # cool margin zone, thermostat heating
        assert result.locked_out is True
        assert result.to_state.value == "heating"

    def test_ode_guard_direct_write_locks_a_following_non_breach_switch(self):
        engine = _make_engine(indoor=69.5, mode="heat")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            with engine._mode_change_context("ode_guard"):
                _run(engine._set_hvac_mode("cool", reason="ODE ceiling guard"))
            assert ("cool", "ode_guard") in _reasons(engine)
            clock.advance(minutes=3)
            result = _preview(engine, clock, 69.5, "cold")
        assert result.locked_out is True
        assert result.to_state.value == "cooling"

    def test_user_mode_change_locks_a_following_non_breach_switch(self):
        engine = _make_engine(indoor=73.0, mode="cool")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), patch(f"{_AUTOMATION}.async_call_later"):
            engine._test_state.state = "heat"  # the occupant switched the thermostat to heat
            engine.handle_manual_override(old_mode="cool", new_mode="heat", classification_mode="cool")
            assert ("heat", "user") in _reasons(engine)
            clock.advance(minutes=3)
            result = _preview(engine, clock, 73.0, "hot")  # cool is native on a hot day: only the lock applies
        assert result.locked_out is True
        assert result.to_state.value == "heating"

    def test_setpoint_only_override_is_not_a_mode_change(self):
        engine = _make_engine(indoor=70.0, mode="cool")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), patch(f"{_AUTOMATION}.async_call_later"):
            engine.handle_manual_override(source="setpoint", old_mode="cool", new_mode="cool")
        assert engine.comfort_mode_changes == []

    def test_lock_lapses_after_the_dwell_window(self):
        engine = _make_engine(indoor=69.5, mode="off")
        clock = _Clock(_T0)
        c = _classification("hot", "cool")
        engine._current_classification = c
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_tou_precondition(c, 64.0, "s1"))
            clock.advance(minutes=11)  # past the 600 s default
            result = _preview(engine, clock, 69.5, "cold")
        assert result.changed and not result.locked_out

    def test_a_breach_still_switches_immediately_inside_the_lock(self):
        engine = _make_engine(indoor=66.0, mode="off")
        clock = _Clock(_T0)
        c = _classification("hot", "cool")
        engine._current_classification = c
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine.apply_tou_precondition(c, 64.0, "s1"))
            clock.advance(minutes=1)
            # Issue #1073: 62.0 (depth 6 > the hot-day 5 F tolerance). A 66.0 reading (depth 2) right after
            # the cool bank is the overshoot the settling guard now HOLDS; a DEEP breach still ignores the lock.
            result = _preview(engine, clock, 62.0)
        assert result.decision.breach is True
        assert result.locked_out is False
        assert result.changed and result.to_state.value == "heating"


# ---------------------------------------------------------------------------
# (3) the lock survives serialize + restore of the engine state
# ---------------------------------------------------------------------------


class TestLockSurvivesRestart:
    def test_lock_persists_through_the_real_state_round_trip(self):
        engine = _make_engine(indoor=73.0, mode="cool")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context("restore"):
            _run(engine._set_hvac_mode("heat", reason="restore"))
        saved = json.loads(json.dumps(engine.get_serializable_state()))

        fresh = _make_engine(indoor=73.0, mode="heat")  # thermostat already in heat after the restart
        fresh.restore_state(saved)
        assert fresh._comfort_family_dwell_state is None  # in-memory selector clock is gone
        clock2 = _Clock(_T0 + timedelta(minutes=4))
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock2):
            locked = _preview(fresh, clock2, 73.0)
        assert locked.locked_out is True

        clock3 = _Clock(_T0 + timedelta(minutes=12))
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock3):
            free = _preview(fresh, clock3, 73.0)
        assert free.changed and not free.locked_out

    def test_without_the_restored_buffer_there_is_no_lock(self):
        fresh = _make_engine(indoor=73.0, mode="heat")
        clock = _Clock(_T0 + timedelta(minutes=4))
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            result = _preview(fresh, clock, 73.0)
        assert result.changed and not result.locked_out


# ---------------------------------------------------------------------------
# (4) one "last ran" source, event grain, survives restart
# ---------------------------------------------------------------------------


def _coordinator_class():
    return importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator


def _bare_coordinator(engine: AutomationEngine):
    import types

    cls = _coordinator_class()
    coord = object.__new__(cls)
    coord.automation_engine = engine
    coord._note_equipment_activity = types.MethodType(cls._note_equipment_activity, coord)
    return coord


class TestOneLastRanSource:
    def test_event_stamp_feeds_the_engine_and_beats_the_cycle_grain_value(self):
        engine = _make_engine(indoor=70.0, mode="heat")
        engine._last_hvac_heating_active = (_T0 - timedelta(minutes=60)).isoformat()  # stale 30-min-cycle stamp
        coord = _bare_coordinator(engine)
        assert engine.minutes_since_equipment_ran("heat", _T0) == 60.0

        coord._note_equipment_activity("heating", "idle", now=_T0)

        assert engine.minutes_since_equipment_ran("heat", _T0 + timedelta(minutes=10)) == 10.0
        assert engine.minutes_since_equipment_ran("heating", _T0 + timedelta(minutes=10)) == 10.0
        assert engine.minutes_since_equipment_ran("cool", _T0) is None
        assert engine._minutes_since_heating_ended(now=_T0 + timedelta(minutes=10)) == 10.0

    def test_cooling_direction_is_independent(self):
        engine = _make_engine(indoor=70.0, mode="cool")
        coord = _bare_coordinator(engine)
        coord._note_equipment_activity("cooling", now=_T0)
        assert engine.minutes_since_equipment_ran("cool", _T0 + timedelta(minutes=5)) == 5.0
        assert engine.minutes_since_equipment_ran("heat", _T0) is None

    def test_idle_action_stamps_nothing(self):
        engine = _make_engine(indoor=70.0, mode="heat")
        coord = _bare_coordinator(engine)
        coord._note_equipment_activity("idle", "fan", None, now=_T0)
        assert engine.minutes_since_equipment_ran("heat", _T0) is None
        assert engine.minutes_since_equipment_ran("cool", _T0) is None

    def test_value_survives_a_restart(self):
        engine = _make_engine(indoor=70.0, mode="heat")
        _bare_coordinator(engine)._note_equipment_activity("heating", now=_T0)
        saved = json.loads(json.dumps(engine.get_serializable_state()))

        fresh = _make_engine(indoor=70.0, mode="heat")
        assert fresh.minutes_since_equipment_ran("heat", _T0) is None
        fresh.restore_state(saved)
        assert fresh.minutes_since_equipment_ran("heat", _T0 + timedelta(minutes=10)) == 10.0

    def test_legacy_heating_ended_reader_delegates_to_the_shared_helper(self):
        engine = _make_engine(indoor=70.0, mode="heat")
        _bare_coordinator(engine)._note_equipment_activity("heating", now=_T0)
        with patch.object(engine, "minutes_since_equipment_ran", wraps=engine.minutes_since_equipment_ran) as shared:
            assert engine._minutes_since_heating_ended(now=_T0 + timedelta(minutes=1)) == 1.0
        shared.assert_called_once()


# ---------------------------------------------------------------------------
# (5) re-affirm does not record; minutes_since_mode_set reads the buffer
# ---------------------------------------------------------------------------


class TestReaffirmAndModeSetHelper:
    def test_reaffirming_the_live_mode_records_nothing(self):
        engine = _make_engine(indoor=70.0, mode="heat")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine._set_hvac_mode("heat", reason="re-affirm"))
            _run(engine._set_temperature(68.0, reason="re-affirm setpoint", mode="heat"))
        assert engine.comfort_mode_changes == []
        assert engine.minutes_since_mode_set("heat", _T0) is None

    def test_a_real_change_records_once_even_across_repeated_writes(self):
        engine = _make_engine(indoor=70.0, mode="heat")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            _run(engine._set_hvac_mode("cool", reason="real change"))
            clock.advance(seconds=5)
            _run(engine._set_hvac_mode("cool", reason="repeat"))
            _run(engine._set_temperature(74.0, reason="setpoint", mode="cool"))
        assert [c["to_mode"] for c in engine.comfort_mode_changes] == ["cool"]
        assert len(engine.comfort_mode_changes) == 1

    def test_minutes_since_mode_set_is_per_family_and_any_writer(self):
        engine = _make_engine(indoor=70.0, mode="off")
        clock = _Clock(_T0)
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            with engine._mode_change_context("restore"):
                _run(engine._set_hvac_mode("heat", reason="restore"))
            clock.advance(minutes=30)
            with engine._mode_change_context("tou"):
                _run(engine._set_hvac_mode("cool", reason="bank"))
            clock.advance(minutes=5)
            assert engine.minutes_since_mode_set("cool", clock.now) == 5.0
            assert engine.minutes_since_mode_set("cooling", clock.now) == 5.0
            assert engine.minutes_since_mode_set("heat", clock.now) == 35.0
            assert engine.minutes_since_mode_set("auto", clock.now) is None

    def test_buffer_cap_is_unchanged(self):
        from custom_components.climate_advisor.const import COMFORT_MODE_CHANGE_LOG_CAP

        assert COMFORT_MODE_CHANGE_LOG_CAP == 200
