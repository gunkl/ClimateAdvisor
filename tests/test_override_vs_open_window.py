"""Issue #1119: a manual heat/cool override made while a door/window is open must survive
Climate Advisor's own scheduled routines until its grace period ends.

Occupant framing: a door had been open all night. At 05:53 the occupant turned the heat on at
the thermostat; Climate Advisor confirmed it at 06:03 and promised to leave it alone for three
hours. At 06:30 the morning wake-up routine switched the heat off anyway, and at 06:35 the
5-minute revisit announced "automation agrees" (its comfort plan also wanted heat at 68) -- then
cancelled the protection and switched the heat off again in the same instant. The door never
re-opened. The occupant fought the system three times in 45 minutes.

Two defects, both covered here for heat AND cool:

* D1 (regression from #1073, v0.7.98): override adoption compared the user's mode with what the
  comfort-edge selector would arm, ignoring that a live open-window hold forces the real outcome
  to "off". ``_override_decision_mode_and_target()`` now answers "selector does not apply" while
  the hold is live, so the revisit never adopts (never claims agreement, never cancels grace).
* D2 (design gap from #620, v0.6.6): ``_sync_paused_by_door_with_live_sensors()`` -- run at the
  top of wake-up, bedtime, pre-cool, both TOU pre-conditioning paths and apply_classification --
  turned HVAC off before those routines looked at the override. It now leaves the system alone
  while a manual override or its pending confirmation is active. Fan-only grace does NOT block it
  (owner decision D16), and grace expiry still re-pauses (positive controls below).

Real ``AutomationEngine`` + the fake thermostat from ``tests.test_restore_resolved_band``.
Nothing here re-implements production logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.door_window_fsm import (
    DoorWindowFsmEvent,
    DoorWindowFsmEventKind,
    DoorWindowLifecycleState,
    transition,
)
from tests.test_restore_resolved_band import _classification, _make_engine, _run

_AUTOMATION = "custom_components.climate_advisor.automation"
_DAY = datetime(2026, 10, 28, 14, 0, 0, tzinfo=UTC)
_MODES = ["heat", "cool"]
# Each family's band edge on a flat 68/74 day band, and an indoor reading that makes the
# comfort-edge selector arm that same family (so pre-fix adoption "agrees").
_EDGE = {"heat": 68.0, "cool": 74.0}
_INDOOR = {"heat": 69.0, "cool": 73.0}
_DAY_TYPE = {"heat": "mild", "cool": "warm"}


def _engine_with_override(mode: str, *, confirmed: bool = True, grace: bool = True) -> AutomationEngine:
    """Engine whose occupant set ``mode`` at the selector's own edge while a door is open and settled."""
    engine = _make_engine(indoor=_INDOOR[mode], mode=mode)
    engine._test_state.attributes["temperature"] = _EDGE[mode]
    engine._sensor_check_callback = lambda: True  # a monitored door is open
    engine._sensor_debounce_pending_callback = lambda: False  # ... and has been for a while
    engine._paused_by_door = False  # the override cleared the pause (PAUSED --user mode--> GRACE)
    engine._current_classification = _classification(_DAY_TYPE[mode])
    if confirmed:
        engine._manual_override_active = True
        engine._manual_override_mode = mode
        engine._manual_override_source = "pause"
        engine._manual_override_time = _DAY - timedelta(minutes=30)
        engine._grace_active = grace
        engine._grace_protects_override = grace
    else:
        engine._override_confirm_pending = True
        engine._override_confirm_mode = mode
        engine._override_confirm_time = _DAY - timedelta(minutes=2)
    return engine


def _hvac_mode_commands(engine: AutomationEngine) -> list[str]:
    modes = []
    for c in engine.hass.services.async_call.call_args_list:
        if c.args[0] != "climate":
            continue
        if c.args[1] == "set_hvac_mode" or (c.args[1] == "set_temperature" and "hvac_mode" in c.args[2]):
            modes.append(c.args[2]["hvac_mode"])
    return modes


async def _wakeup(engine: AutomationEngine) -> None:
    await engine.handle_morning_wakeup()


async def _bedtime(engine: AutomationEngine) -> None:
    await engine.handle_bedtime()


async def _pre_cool(engine: AutomationEngine) -> None:
    engine._current_classification.tomorrow_high = 105.0  # hot tomorrow -> pre-cool eligible
    await engine.handle_pre_cool(indoor_temp=_INDOOR["cool"], nat_vent_just_closed=False)


async def _tou(engine: AutomationEngine) -> None:
    await engine.apply_tou_precondition(engine._current_classification, 70.0, "tou-1")


async def _tou_away(engine: AutomationEngine) -> None:
    await engine.apply_tou_away_vacation_precondition(
        engine._current_classification, 70.0, "tou-1", engine._test_state.state
    )


async def _apply_classification(engine: AutomationEngine) -> None:
    await engine.apply_classification(engine._current_classification)


_CALLERS = {
    "wakeup": _wakeup,
    "bedtime": _bedtime,
    "pre_cool": _pre_cool,
    "tou": _tou,
    "tou_away": _tou_away,
    "apply_classification": _apply_classification,
}


def _drive(engine: AutomationEngine, caller: str) -> None:
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
        _run(_CALLERS[caller](engine))


# ---------------------------------------------------------------------------
# D1: adoption never "agrees" with an override the open-window hold will undo
# ---------------------------------------------------------------------------


class TestAdoptionRespectsOpenWindowHold:
    @pytest.mark.parametrize("mode", _MODES)
    def test_selector_agrees_but_open_window_hold_does_not_adopt(self, mode):
        engine = _engine_with_override(mode)
        c = engine._current_classification
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            assert engine._override_match_inputs(c) == (None, None)
            assert engine._override_matches_current_decision(c) is False

    @pytest.mark.parametrize("mode", _MODES)
    def test_paused_by_door_is_also_a_live_hold(self, mode):
        engine = _engine_with_override(mode)
        engine._sensor_check_callback = lambda: False  # flag alone, e.g. a stale pause
        engine._paused_by_door = True
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            assert engine._override_matches_current_decision(engine._current_classification) is False

    @pytest.mark.parametrize("mode", _MODES)
    def test_control_door_closed_selector_agreement_still_adopts(self, mode):
        """Positive control: with the door closed the #1073 adoption is unchanged."""
        engine = _engine_with_override(mode)
        engine._sensor_check_callback = lambda: False
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            assert engine._override_match_inputs(engine._current_classification) == (mode, _EDGE[mode])
            assert engine._override_matches_current_decision(engine._current_classification) is True

    @pytest.mark.parametrize("mode", _MODES)
    def test_control_debouncing_blip_is_not_a_hold(self, mode):
        engine = _engine_with_override(mode)
        engine._sensor_debounce_pending_callback = lambda: True
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            assert engine._override_matches_current_decision(engine._current_classification) is True

    @pytest.mark.parametrize("mode", _MODES)
    def test_revisit_keeps_override_grace_and_mode(self, mode):
        """The 06:35 path: apply_classification must not adopt, cancel grace, or switch off."""
        engine = _engine_with_override(mode)
        _drive(engine, "apply_classification")
        assert "off" not in _hvac_mode_commands(engine)
        assert engine._manual_override_active is True
        assert engine._grace_active is True
        assert engine._test_state.state == mode


# ---------------------------------------------------------------------------
# D2: the scheduled open-window re-check leaves an override / pending confirm alone
# ---------------------------------------------------------------------------


class TestScheduledRoutinesLeaveOverrideAlone:
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("caller", list(_CALLERS))
    def test_confirmed_override_is_not_switched_off(self, caller, mode):
        engine = _engine_with_override(mode)
        _drive(engine, caller)
        assert "off" not in _hvac_mode_commands(engine), f"{caller} switched the occupant's {mode} off"
        assert engine._paused_by_door is False
        assert engine._manual_override_active is True
        assert engine._grace_active is True
        assert engine._test_state.state == mode

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("caller", ["tou", "tou_away", "apply_classification"])
    def test_pending_confirmation_is_not_switched_off(self, caller, mode):
        """Callers that defer to a pending confirmation must not reach an "off" either.

        wake-up / bedtime / pre-cool deliberately supersede an UNCONFIRMED change by design
        (``clear_manual_override(reason="morning_wakeup")`` etc.), so they are covered at the
        helper level below rather than end-to-end.
        """
        engine = _engine_with_override(mode, confirmed=False)
        _drive(engine, caller)
        assert "off" not in _hvac_mode_commands(engine)
        assert engine._override_confirm_pending is True
        assert engine._paused_by_door is False

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("confirmed", [True, False], ids=["override", "pending"])
    def test_sync_helper_leaves_override_or_pending_alone(self, mode, confirmed):
        engine = _engine_with_override(mode, confirmed=confirmed, grace=False)
        _drive_sync(engine)
        assert "off" not in _hvac_mode_commands(engine)
        assert engine._paused_by_door is False

    @pytest.mark.parametrize("mode", _MODES)
    def test_override_without_grace_is_still_protected(self, mode):
        """manual_grace_seconds=0: the override itself, not the grace timer, is what is protected."""
        engine = _engine_with_override(mode, grace=False)
        _drive(engine, "wakeup")
        assert "off" not in _hvac_mode_commands(engine)


def _drive_sync(engine: AutomationEngine) -> None:
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
        _run(engine._sync_paused_by_door_with_live_sensors())


# ---------------------------------------------------------------------------
# Positive controls: the open-window protection itself is unchanged
# ---------------------------------------------------------------------------


class TestOpenWindowProtectionStillWorks:
    @pytest.mark.parametrize("mode", _MODES)
    def test_no_override_scheduled_recheck_still_pauses(self, mode):
        engine = _engine_with_override(mode)
        engine._manual_override_active = False
        engine._manual_override_mode = None
        engine._grace_active = False
        engine._grace_protects_override = False
        _drive_sync(engine)
        assert _hvac_mode_commands(engine) == ["off"]
        assert engine._paused_by_door is True

    @pytest.mark.parametrize("mode", _MODES)
    def test_fan_only_grace_does_not_block_the_recheck(self, mode):
        """Owner decision D16: a grace with no manual override (e.g. fan-off grace) is not protected."""
        engine = _engine_with_override(mode)
        engine._manual_override_active = False
        engine._manual_override_mode = None
        engine._grace_active = True
        engine._grace_protects_override = False
        _drive_sync(engine)
        assert _hvac_mode_commands(engine) == ["off"]
        assert engine._paused_by_door is True

    @pytest.mark.parametrize("mode", _MODES)
    def test_grace_expiry_with_sensor_still_open_re_pauses(self, mode):
        """The designed moment to switch off: the override's grace ends with the door still open."""
        engine = _engine_with_override(mode)
        engine._last_resume_source = "manual"
        scheduled = []
        engine.hass.async_create_task.side_effect = scheduled.append
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            engine._on_grace_expired("manual", 10800, False)
            for coro in scheduled:  # _re_pause_for_open_sensor() is scheduled, not awaited
                _run(coro)
        assert _hvac_mode_commands(engine) == ["off"]
        assert engine._paused_by_door is True
        assert engine._manual_override_active is False


# ---------------------------------------------------------------------------
# The door/window FSM's pure copy of the re-check agrees with production
# ---------------------------------------------------------------------------


class TestDoorWindowFsmMirrorsTheGuard:
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("confirmed", [True, False], ids=["override", "pending"])
    def test_sync_reconcile_stays_normal_while_override_or_pending(self, mode, confirmed):
        engine = _engine_with_override(mode, confirmed=confirmed, grace=False)
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            inputs = engine._build_door_window_fsm_inputs(now=_DAY)
        t = transition(
            DoorWindowLifecycleState.NORMAL, DoorWindowFsmEvent(DoorWindowFsmEventKind.SYNC_RECONCILE, inputs)
        )
        assert t.to_state == DoorWindowLifecycleState.NORMAL

    @pytest.mark.parametrize("mode", _MODES)
    def test_sync_reconcile_still_pauses_without_override(self, mode):
        engine = _engine_with_override(mode, grace=False)
        engine._manual_override_active = False
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            inputs = engine._build_door_window_fsm_inputs(now=_DAY)
        t = transition(
            DoorWindowLifecycleState.NORMAL, DoorWindowFsmEvent(DoorWindowFsmEventKind.SYNC_RECONCILE, inputs)
        )
        assert t.to_state == DoorWindowLifecycleState.PAUSED_ACTIVE
