"""Issues #1165 and #1157: closing a window never overrides what the occupant did during the pause.

Owner rules (authoritative): 1. HVAC stays off while a window is open unless a manual override turned it on.
2. The whole-house fan never runs on a sealed house. 3. WHF and HVAC never run together, newest action wins.
Occupant first: the occupant's own latest choice, including Off, wins.

* #1165  A nat-vent exit left the window open and a pause recorded. The occupant switched the thermostat Off. When
  they closed the window Climate Advisor turned cooling/heating back on over their Off, minutes before their manual
  pause ended (seed 1149005000058). The Off had also been recorded as the pre-pause mode, as if it were
  Climate Advisor's own pause Off.
* #1157  The same kind of pause, but the thermostat was Off and the occupant only moved the setpoint. Closing the
  window wrote nothing, so HVAC stayed off until their override ended (bedtime or wake-up with Manual Change
  Pause = 0).

Real ``AutomationEngine`` + the fake thermostat from ``tests.test_restore_resolved_band``. The pause is recorded by
the real ``_exit_nat_vent()`` and the occupant's change goes through the real override entry points.
"""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE
from tests.test_protect_manual_change import (
    _climate_writes,
    _close_window,
    _engine,
    timers,  # noqa: F401  (pytest fixture, re-exported)
)
from tests.test_restore_resolved_band import _run


def _nat_vent_exit_pause(*, live_mode: str, day: str, indoor: float, confirm_s: int, grace_s: int = 1800):
    """A nat-vent session ends with the window still open: the real exit records the pause and its (absent) mode."""
    engine = _engine(
        live_mode,
        indoor=indoor,
        outdoor=62.0,
        setpoint=72.0,
        day=day,
        window_open=True,
        manual_grace_seconds=grace_s,
        override_confirm_seconds=confirm_s,
    )
    engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
    engine.config[CONF_FAN_ENTITY] = "switch.whf"
    engine._natural_vent_active = True
    engine._pre_fan_hvac_mode = live_mode if live_mode != "off" else None
    _run(engine._exit_nat_vent(reason="indoor reached the comfort floor"))
    assert engine._paused_by_door and engine._paused_entity == "nat-vent-exit"
    return engine


def _occupant_sets_off(engine, old_mode: str) -> None:
    engine._test_state.state = "off"
    engine.handle_manual_override(
        source="normal",
        old_mode=old_mode,
        new_mode="off",
        classification_mode=engine._current_classification.hvac_mode,
    )


def _occupant_moves_setpoint(engine, to_f: float) -> None:
    old = engine._test_state.attributes["temperature"]
    engine._test_state.attributes["temperature"] = to_f
    engine.handle_manual_override(
        source="setpoint",
        old_mode="off",
        new_mode="off",
        classification_mode=engine._current_classification.hvac_mode,
        old_setpoint_f=old,
        new_setpoint_f=to_f,
    )


def _mode_or_setpoint_writes(engine) -> list:
    return [w for w in _climate_writes(engine) if w[0] in ("set_hvac_mode", "set_temperature")]


# ---------------------------------------------------------------------------
# #1165: the occupant's own Off survives the window closing
# ---------------------------------------------------------------------------


class TestOccupantsOffSurvivesTheWindowClosing:
    @pytest.mark.parametrize("pre_mode,day", [("cool", "hot"), ("heat", "cold")], ids=["cool", "heat"])
    @pytest.mark.parametrize("confirm_s", [0, 600], ids=["confirmed", "pending"])
    @pytest.mark.parametrize("grace_s", [1800, 0], ids=["grace", "no_grace"])
    def test_close_writes_nothing_over_the_off(self, timers, pre_mode, day, confirm_s, grace_s):  # noqa: F811
        engine = _nat_vent_exit_pause(live_mode=pre_mode, day=day, indoor=70.0, confirm_s=confirm_s, grace_s=grace_s)
        assert engine._pre_pause_mode == pre_mode
        _occupant_sets_off(engine, pre_mode)
        assert engine._manual_change_protected()
        engine.hass.services.async_call.reset_mock()
        grace_end_before = engine._grace_end_time

        _close_window(engine)

        assert _mode_or_setpoint_writes(engine) == [], "the occupant's Off is the newest word; nothing is written"
        assert engine._test_state.state == "off"
        assert engine._paused_by_door is False and engine._pre_pause_mode is None
        assert engine._grace_end_time == grace_end_before, "no 5-minute automation grace replaces their manual one"

    @pytest.mark.parametrize("pre_mode,day", [("cool", "hot"), ("heat", "cold")], ids=["cool", "heat"])
    def test_the_off_is_recorded_as_off_not_as_the_pre_pause_mode(self, timers, pre_mode, day):  # noqa: F811
        """Confirm time used to read the live Off as Climate Advisor's pause Off and substitute the pre-pause mode."""
        engine = _nat_vent_exit_pause(live_mode=pre_mode, day=day, indoor=70.0, confirm_s=600)
        _occupant_sets_off(engine, pre_mode)
        assert engine._override_confirm_pending and engine._override_confirm_mode == "off"

        timers.fire(600)

        assert engine._manual_override_active and engine._manual_override_mode == "off"

    def test_control_a_setpoint_change_on_a_ca_paused_thermostat_still_resumes_the_pre_pause_mode(self, timers):  # noqa: F811
        """#1131 is unchanged: a SETPOINT change on a thermostat CA paused Off resumes the pre-pause mode."""
        engine = _engine("cool", indoor=76.0, outdoor=95.0, setpoint=72.0, day="hot", manual_grace_seconds=1800)
        engine._window_open = True
        _run(engine.handle_door_window_open("binary_sensor.window"))
        assert engine._test_state.state == "off" and engine._pre_pause_mode == "cool"
        _occupant_moves_setpoint(engine, 70.0)

        _close_window(engine)

        assert engine._test_state.state == "cool"
        assert engine._test_state.attributes["temperature"] == 70.0


# ---------------------------------------------------------------------------
# #1157: a setpoint change after a nat-vent exit left the thermostat Off still resumes HVAC when the window closes
# ---------------------------------------------------------------------------


class TestSetpointChangeAfterANatVentExitResumesHvac:
    @pytest.mark.parametrize(
        "day,indoor,expected",
        [("hot", 76.0, "cool"), ("cold", 66.0, "heat")],
        ids=["hot_ceiling_cool", "cold_floor_heat"],
    )
    @pytest.mark.parametrize("confirm_s", [0, 600], ids=["confirmed", "pending"])
    def test_close_resumes_the_comfort_family_at_the_occupants_setpoint(
        self,
        timers,  # noqa: F811
        day,
        indoor,
        expected,
        confirm_s,
    ):
        engine = _nat_vent_exit_pause(live_mode="off", day=day, indoor=indoor, confirm_s=confirm_s)
        assert engine._pre_pause_mode is None, "the #1068 case: nothing to restore"
        _occupant_moves_setpoint(engine, 71.0)
        assert engine._manual_change_protected()
        engine.hass.services.async_call.reset_mock()
        grace_end_before = engine._grace_end_time

        _close_window(engine)

        assert engine._test_state.state == expected
        assert engine._test_state.attributes["temperature"] == 71.0, "the OCCUPANT's setpoint, not CA's"
        assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"] == []
        assert engine._grace_end_time == grace_end_before

    def test_the_override_is_recorded_as_the_mode_the_close_resumes(self, timers):  # noqa: F811
        """So the household promise (override_respected) sees the resume as the occupant's own mode, not a reversal
        of an 'off'."""
        engine = _nat_vent_exit_pause(live_mode="off", day="hot", indoor=76.0, confirm_s=600)
        emitted = []
        engine._emit_event_callback = lambda name, data: emitted.append((name, data))
        _occupant_moves_setpoint(engine, 71.0)

        assert engine._override_confirm_mode == "cool"
        assert [d["detected_mode"] for n, d in emitted if n == "override_detected"] == ["cool"]

    @pytest.mark.parametrize(
        "indoor,expected",
        [(77.0, "cool"), (60.0, "heat")],
        ids=["mild_hot_indoor_cool", "mild_cold_indoor_heat"],
    )
    @pytest.mark.parametrize("grace_s", [1800, 0], ids=["grace", "no_grace"])
    def test_mild_day_resolves_the_family_from_the_armed_band(self, timers, indoor, expected, grace_s):  # noqa: F811
        """A mild day's classification is 'off' (no family of its own); the armed band's active edge decides, so the
        occupant who nudged the setpoint is not left Off until bedtime/wake (grace 0)."""
        engine = _nat_vent_exit_pause(live_mode="off", day="mild", indoor=indoor, confirm_s=0, grace_s=grace_s)
        _occupant_moves_setpoint(engine, 71.0)
        assert engine._manual_override_mode == expected
        engine.hass.services.async_call.reset_mock()

        _close_window(engine)

        assert engine._test_state.state == expected
        assert engine._test_state.attributes["temperature"] == 71.0
        assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"] == []

    def test_the_setpoint_change_commits_no_selector_state_and_emits_no_family_switch(self, timers):  # noqa: F811
        """Judging the occupant's change is a pure preview: no phantom comfort_family_switch, no dwell/arming state,
        no HVAC write while the window is still open."""
        engine = _nat_vent_exit_pause(live_mode="off", day="hot", indoor=76.0, confirm_s=0)
        emitted = []
        engine._emit_event_callback = lambda name, data: emitted.append(name)
        before = (
            engine._comfort_mode_family,
            engine._comfort_family_dwell_state,
            engine._last_comfort_switch,
            engine._last_selector_write_attempt,
        )
        engine.hass.services.async_call.reset_mock()

        _occupant_moves_setpoint(engine, 71.0)

        assert engine._manual_override_mode == "cool"
        assert "comfort_family_switch" not in emitted
        assert (
            engine._comfort_mode_family,
            engine._comfort_family_dwell_state,
            engine._last_comfort_switch,
            engine._last_selector_write_attempt,
        ) == before
        assert _mode_or_setpoint_writes(engine) == []

    def test_away_keeps_the_live_off_the_setback_owns_the_close(self, timers):  # noqa: F811
        """Documented asymmetry: away/vacation is not given a family here; the override stays 'off'."""
        engine = _nat_vent_exit_pause(live_mode="off", day="hot", indoor=76.0, confirm_s=0)
        engine._occupancy_mode = "away"
        _occupant_moves_setpoint(engine, 71.0)
        assert engine._manual_override_mode == "off"

    def test_control_a_thermostat_left_off_before_an_ordinary_pause_stays_off(self, timers):  # noqa: F811
        """The #1068 gate stands: only a nat-vent-exit pause resumes; an ordinary pause over Off does not."""
        engine = _engine(
            "off", indoor=76.0, setpoint=72.0, day="hot", manual_grace_seconds=1800, override_confirm_seconds=0
        )
        engine._window_open = True
        _run(engine.handle_door_window_open("binary_sensor.window"))
        assert engine._paused_by_door and engine._pre_pause_mode is None
        assert engine._paused_entity != "nat-vent-exit"
        _occupant_moves_setpoint(engine, 71.0)
        assert engine._manual_override_mode == "off"
        engine.hass.services.async_call.reset_mock()

        _close_window(engine)

        assert _mode_or_setpoint_writes(engine) == []
        assert engine._test_state.state == "off"

    def test_control_without_an_override_the_close_rearms_comfort(self, timers):  # noqa: F811
        engine = _nat_vent_exit_pause(live_mode="off", day="hot", indoor=76.0, confirm_s=0)
        assert not engine._manual_change_protected()
        engine.hass.services.async_call.reset_mock()

        _close_window(engine)

        assert engine._test_state.state == "cool"
        assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"], "CA's own setpoint when nobody spoke"
