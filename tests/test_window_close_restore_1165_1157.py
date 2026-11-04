"""Issues #1165 and #1157: closing a window never overrides what the occupant did during the pause.

Owner rules (authoritative): 1. HVAC stays off while a window is open unless a manual override turned it on.
2. The whole-house fan never runs on a sealed house. 3. WHF and HVAC never run together, newest action wins.
Occupant first: the occupant's own latest choice, including Off, wins.

* #1165  A nat-vent exit left the window open and a pause recorded. The occupant switched the thermostat Off. When
  they closed the window Climate Advisor turned cooling/heating back on over their Off, minutes before their manual
  pause ended (seed 1149005000058). The Off had also been recorded as the pre-pause mode, as if it were
  Climate Advisor's own pause Off.
* #1157  (retired by Issue #1325: a setpoint-only change while the thermostat is Off is not an override, so there
  is nothing to resume; the coordinator ignores it.)

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
        # Issue #1142: the exit leaves the thermostat Off at once, so nothing is left to restore (the #1068 path
        # re-arms through the selector). A real whole-house-fan session had it Off already.
        assert engine._pre_pause_mode is None
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


class TestNatVentExitCloseWithoutAnOverride:
    def test_control_without_an_override_the_close_rearms_comfort(self, timers):  # noqa: F811
        engine = _nat_vent_exit_pause(live_mode="off", day="hot", indoor=76.0, confirm_s=0)
        assert not engine._manual_change_protected()
        engine.hass.services.async_call.reset_mock()

        _close_window(engine)

        assert engine._test_state.state == "cool"
        assert [w for w in _climate_writes(engine) if w[0] == "set_temperature"], "CA's own setpoint when nobody spoke"
