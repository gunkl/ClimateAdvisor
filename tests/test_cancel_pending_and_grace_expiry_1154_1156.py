"""Issues #1154 and #1156: Cancel Override ends an unconfirmed change; an unrelated grace never undoes one.

Household rules in play: the occupant's latest choice wins; HVAC stays paused while a window is open UNLESS a
manual override turned it on (a change still inside its confirmation window counts, Issue #1133).

* #1154  The occupant changes the thermostat and presses Cancel Override inside the confirmation window. The
  dashboard answered "No active override or grace period to cancel" and the change stayed.
* #1156  A window closes (5-minute automation grace), reopens, the occupant changes the thermostat, and the
  grace runs out with the window still open: CA dropped the pending change and switched HVAC off over it.

Real ``AutomationEngine`` through the real entry points (``handle_manual_override``, the real grace start, the
real expiry callback body); the fixtures are the ones ``tests/test_protect_manual_change.py`` already uses.
"""

from __future__ import annotations

import pytest

from tests.test_protect_manual_change import (
    _engine,
    _occupant_overrides,
    _off_writes,
    _open_window,
    timers,  # noqa: F401  (fixture)
)
from tests.test_restore_resolved_band import _run


def _open_window_pause(engine) -> None:
    _open_window(engine)
    assert engine._paused_by_door


def _pending_change(engine) -> None:
    _occupant_overrides(engine)
    assert engine._override_confirm_pending and not engine._manual_override_active


# ---------------------------------------------------------------------------
# #1154
# ---------------------------------------------------------------------------


class TestCancelOverrideEndsAnUnconfirmedChange:
    def test_cancel_inside_the_confirmation_window_ends_the_change(self, timers):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=600)
        _pending_change(engine)

        assert engine.cancel_override(reason="user_cancel_override") is True

        assert engine._override_confirm_pending is False
        assert engine._manual_change_protected() is False
        assert engine._override_confirm_cancel is None, "the confirmation timer must be cancelled with it"

    def test_the_same_predicate_drives_the_dashboard_message(self, timers):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=600)
        assert engine._occupant_override_active() is False
        _pending_change(engine)
        assert engine._occupant_override_active() is True

    def test_control_nothing_to_cancel_is_still_a_noop(self, timers):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=600)
        assert engine.cancel_override(reason="user_cancel_override") is False

    @pytest.mark.parametrize("kind", ["confirmed", "fan"])
    def test_control_confirmed_and_fan_overrides_still_cancel(self, timers, kind):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=0, manual_grace_seconds=1800)
        if kind == "confirmed":
            _occupant_overrides(engine)
            assert engine._manual_override_active
        else:
            engine.handle_fan_manual_override(fan_before="auto", fan_after="on")
            assert engine._fan_override_active
        assert engine.cancel_override(reason="user_cancel_override") is True
        assert not engine._manual_change_protected() and not engine._fan_override_active


# ---------------------------------------------------------------------------
# #1156
# ---------------------------------------------------------------------------


def _window_reopened_during_a_window_close_grace(engine) -> None:
    """The 5-minute automation grace a closing window started is still running; the window is open again."""
    engine._start_grace_period("automation", trigger="sensor_closed_resume")
    assert engine._grace_active and not engine._grace_protects_override
    engine._window_open = True  # reopened; the grace is not an override, so nothing paused yet


class TestAnUnrelatedGraceExpiryKeepsThePendingChange:
    def test_pending_change_survives_and_hvac_is_not_paused(self, timers):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=600, automation_grace_seconds=300)
        _window_reopened_during_a_window_close_grace(engine)
        _pending_change(engine)

        engine._on_grace_expired("automation", 300, False)  # the real expiry callback body
        _run(engine._re_pause_for_open_sensor())  # the task a re-pause would schedule

        assert engine._override_confirm_pending is True, "the occupant's pending change must not be dropped"
        assert engine._manual_change_protected()
        assert engine._grace_active is False, "the unrelated grace itself still ends"
        assert engine._paused_by_door is False
        assert _off_writes(engine) == []
        assert engine._test_state.state == "heat"

    def test_the_pending_change_still_confirms_afterwards(self, timers):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=600, automation_grace_seconds=300)
        _window_reopened_during_a_window_close_grace(engine)
        _pending_change(engine)
        engine._on_grace_expired("automation", 300, False)
        assert engine._override_confirm_cancel is not None, "the confirmation timer must still be armed"

        timers.fire(600)  # the confirmation window runs out

        assert engine._manual_override_active is True
        assert engine._grace_active and engine._grace_protects_override

    def test_control_without_a_change_the_unrelated_grace_still_repauses(self, timers):  # noqa: F811
        """Rule 3 stays alive: no occupant change, window open -> the grace ending pauses HVAC."""
        engine = _engine("heat", override_confirm_seconds=600, automation_grace_seconds=300)
        _window_reopened_during_a_window_close_grace(engine)

        engine._on_grace_expired("automation", 300, False)
        _run(engine._re_pause_for_open_sensor())

        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_control_the_occupants_own_grace_expiring_still_repauses(self, timers):  # noqa: F811
        """The designed re-pause (rule 3 / #1133): the occupant's own grace ending with a window open pauses."""
        engine = _engine("heat", override_confirm_seconds=0, manual_grace_seconds=1800)
        _occupant_overrides(engine)
        engine._window_open = True
        assert engine._grace_protects_override

        engine._on_grace_expired("manual", 1800, False)
        _run(engine._re_pause_for_open_sensor())

        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"


class TestRePauseRespectsALiveChange:
    def test_a_change_confirmed_after_the_repause_was_scheduled_is_not_paused_over(self, timers):  # noqa: F811
        """``_on_grace_expired`` schedules the re-pause as a task; the occupant can change the thermostat before
        it runs. Their latest choice wins."""
        engine = _engine("heat", override_confirm_seconds=0, manual_grace_seconds=1800)
        engine._window_open = True
        _occupant_overrides(engine)  # lands after the (here: absent) expiry cleared any earlier override
        assert engine._manual_change_protected()

        _run(engine._re_pause_for_open_sensor())

        assert engine._paused_by_door is False
        assert _off_writes(engine) == []
        assert engine._test_state.state == "heat"


# ---------------------------------------------------------------------------
# #1156 review follow-ups: a CONFIRMED override whose protecting grace was replaced still ends at expiry
# ---------------------------------------------------------------------------


class TestConfirmedOverrideWithAReplacementGraceStillEnds:
    """fan-off and dashboard Resume replace the override's protecting grace with an unprotected one while the
    override stays live. That grace expiring ends the override (and honours an open window, rule 3): keeping the
    override would leave it with no timer and no end."""

    def test_fan_off_grace_expiry_ends_the_override_and_repauses_for_an_open_window(self, timers):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=0, manual_grace_seconds=1800)
        _occupant_overrides(engine)
        assert engine._manual_override_active and engine._grace_protects_override
        engine._window_open = True
        engine._clear_fan_flags_and_start_grace(reason="occupant switched the thermostat fan off")
        assert engine._grace_active and not engine._grace_protects_override
        assert engine._manual_override_active, "fan-off leaves the thermostat override live"

        engine._on_grace_expired("manual", 1800, False)
        _run(engine._re_pause_for_open_sensor())

        assert engine._manual_override_active is False, "the override must not outlive its grace with no timer"
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"

    def test_dashboard_resume_grace_expiry_ends_the_override(self, timers):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=0, manual_grace_seconds=1800)
        _open_window_pause(engine)
        _occupant_overrides(engine, source="setpoint")
        assert engine._manual_override_active and engine._paused_by_door
        _run(engine.resume_from_pause())
        assert engine._grace_active and not engine._grace_protects_override
        assert engine._manual_override_active

        engine._on_grace_expired("manual", 1800, False)

        assert engine._manual_override_active is False, "the override must not outlive its grace with no timer"


class TestRePauseEarlyReturnLeavesNoStalePauseRecord:
    def test_a_change_landing_after_the_repause_flags_were_applied_replaces_the_pause(self, timers):  # noqa: F811
        """``_on_grace_expired`` records the re-pause flags and schedules the task; the occupant's plain
        mode change (not from Off, so the during-pause handler does not run) lands before the task does."""
        engine = _engine("heat", override_confirm_seconds=0, manual_grace_seconds=1800, automation_grace_seconds=300)
        _window_reopened_during_a_window_close_grace(engine)
        engine._on_grace_expired("automation", 300, False)  # window open: RE_PAUSE flags recorded
        assert engine._paused_by_door is True
        assert engine._test_state.state == "heat", "the pause command has not run yet"

        _occupant_overrides(engine)  # plain change while the HVAC is still running
        _run(engine._re_pause_for_open_sensor())

        assert engine._paused_by_door is False, "no pause may stay recorded over the occupant's change"
        assert engine._pre_pause_mode is None
        assert _off_writes(engine) == []
        assert engine._test_state.state == "heat"


class TestSecondPendingChangeBehindAConfirmedOverride:
    """confirmed override -> fan off (protecting grace replaced) -> the occupant changes the thermostat AGAIN
    (pending, override still live) -> the replacement grace expires. The expiry ends the override AND the second
    pending change together; a pending change must never be left without its confirmation timer."""

    @staticmethod
    def _scenario(timers, *, window_open):  # noqa: F811
        engine = _engine("heat", override_confirm_seconds=600, manual_grace_seconds=1800)
        _occupant_overrides(engine)
        timers.fire(600)  # first change confirmed, protecting grace running
        assert engine._manual_override_active and engine._grace_protects_override
        engine._clear_fan_flags_and_start_grace(reason="occupant switched the thermostat fan off")
        assert engine._grace_active and not engine._grace_protects_override
        engine._window_open = window_open
        _occupant_overrides(engine)  # second change while the override is live
        assert engine._override_confirm_pending and engine._manual_override_active
        return engine

    @staticmethod
    def _pending_always_has_its_timer(engine):
        assert not engine._override_confirm_pending or engine._override_confirm_cancel is not None, (
            "a pending change without a confirmation timer is protected forever"
        )

    @pytest.mark.parametrize("window_open", [True, False], ids=["window_open", "window_closed"])
    def test_expiry_ends_both_and_leaves_no_timerless_pending_change(self, timers, window_open):  # noqa: F811
        engine = self._scenario(timers, window_open=window_open)

        engine._on_grace_expired("manual", 1800, False)

        self._pending_always_has_its_timer(engine)
        assert engine._override_confirm_pending is False
        assert engine._manual_override_active is False
        assert not engine._manual_change_protected()

    def test_with_a_window_open_the_house_is_then_paused(self, timers):  # noqa: F811
        engine = self._scenario(timers, window_open=True)

        engine._on_grace_expired("manual", 1800, False)
        _run(engine._re_pause_for_open_sensor())

        assert engine._paused_by_door is True
        assert engine._test_state.state == "off"
