"""Issue #1159: an occupant's Off on a mild day is a real manual change, kept through the manual grace.

Occupant framing: on a mild day the day label is "off", yet Climate Advisor arms the comfort band
(heat or cool). If you switch the thermostat Off, that is a decision you made -- exactly as on a hot
or cold day. Climate Advisor must confirm it, keep it for the manual-change pause, and only then resume;
it must not switch heating or cooling back on at the next half-hourly refresh. The one exception is
when Climate Advisor itself is holding HVAC off (an open door/window).

Real ``AutomationEngine`` + a fake thermostat that follows the engine's commands (helpers shared with
tests/test_comfort_settling_guard.py); assertions are on what the thermostat is told, the emitted
events and the override flags.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.const import CONF_OVERRIDE_CONFIRM_PERIOD
from custom_components.climate_advisor.override_grace_lifecycle import GraceState
from tests.test_comfort_settling_guard import (
    _AUTOMATION,
    _apply,
    _Clock,
    _make_engine,
    _mode,
    _mode_writes,
)


class _Scene:
    """An engine with timers captured (confirm window, grace) and events recorded."""

    def __init__(self, *, day: str, indoor: float, ca_first: str) -> None:
        self.day = day
        self.indoor = indoor
        self.engine = _make_engine(indoor=indoor, mode="off", **{CONF_OVERRIDE_CONFIRM_PERIOD: 600})
        self.clock = _Clock()
        self.events: list[str] = []
        self.timers: list = []
        self.engine._emit_event_callback = lambda kind, _data: self.events.append(kind)
        _apply(self.engine, self.clock, day, indoor)
        assert _mode(self.engine) == ca_first, "precondition: CA's own first write"

    def _patches(self):
        return (
            patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
            patch(
                f"{_AUTOMATION}.async_call_later",
                side_effect=lambda _h, _d, fn: self.timers.append(fn) or MagicMock(),
            ),
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=self.clock),
        )

    def occupant_sets(self, new_mode: str, *, old_mode: str) -> None:
        """The thermostat changes to ``new_mode`` and the engine starts override confirmation."""
        self.engine._test_state.state = new_mode
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            self.engine.handle_manual_override(
                old_mode=old_mode,
                new_mode=new_mode,
                classification_mode=self.engine._current_classification.hvac_mode,
            )

    def confirm_elapses(self) -> None:
        self.clock.advance(minutes=10)
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            self.timers[-1](None)  # the confirmation-window timer

    def grace_expires(self) -> None:
        self.clock.advance(minutes=60)
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            self.engine._on_grace_expired("manual", 3600, False)

    def refresh(self) -> list[str]:
        """The next half-hourly refresh; returns the mode writes it made."""
        n0 = len(_mode_writes(self.engine))
        self.clock.advance(minutes=20)
        _apply(self.engine, self.clock, self.day, self.indoor)
        return _mode_writes(self.engine)[n0:]


class TestOffOnMildDayIsKept:
    @pytest.mark.parametrize(
        ("indoor", "ca_first", "old_mode"),
        [(72.0, "cool", "cool"), (64.0, "heat", "heat")],
        ids=["ca_in_cool", "ca_in_heat_floor_armed"],
    )
    def test_off_confirmed_kept_through_grace_then_ca_resumes(self, indoor, ca_first, old_mode):
        s = _Scene(day="mild", indoor=indoor, ca_first=ca_first)
        s.occupant_sets("off", old_mode=old_mode)
        s.confirm_elapses()

        assert "override_confirmed" in s.events
        assert "override_self_resolved" not in s.events
        assert s.engine._manual_override_active
        assert s.engine._manual_override_mode == "off"
        assert s.engine._grace_active
        # the override/grace FSM (authoritative for grace) agrees
        assert s.engine.override_grace_lifecycle_state[1] is GraceState.ACTIVE_PROTECTING_OVERRIDE

        # the next half-hourly refresh must not undo the occupant's Off
        assert s.refresh() == []
        assert _mode(s.engine) == "off"
        assert s.engine._manual_override_active
        assert "override_adopted" not in s.events

        # after the manual-change pause, Climate Advisor resumes
        s.grace_expires()
        assert "grace_expired" in s.events
        assert not s.engine._manual_override_active
        s.refresh()
        assert _mode(s.engine) == ca_first


class TestControlsUnchanged:
    def test_hot_day_off_is_still_confirmed_and_kept(self):
        s = _Scene(day="hot", indoor=76.0, ca_first="cool")
        s.occupant_sets("off", old_mode="cool")
        s.confirm_elapses()
        assert "override_confirmed" in s.events
        assert s.engine._manual_override_mode == "off"
        assert s.refresh() == []
        assert _mode(s.engine) == "off"

    def test_hot_day_off_flipped_back_within_window_self_resolves(self):
        s = _Scene(day="hot", indoor=76.0, ca_first="cool")
        s.occupant_sets("off", old_mode="cool")
        s.engine._test_state.state = "cool"  # the occupant changed their mind inside the window
        s.confirm_elapses()
        assert "override_self_resolved" in s.events
        assert "override_confirmed" not in s.events
        assert not s.engine._manual_override_active
        assert _mode(s.engine) == "cool"

    def test_mild_day_off_flipped_back_is_never_kept_off(self):
        """Behavior unchanged by #1159 for the flip back: the thermostat ends in CA's cool and stays there."""
        s = _Scene(day="mild", indoor=72.0, ca_first="cool")
        s.occupant_sets("off", old_mode="cool")
        s.engine._test_state.state = "cool"
        s.confirm_elapses()
        assert _mode(s.engine) == "cool"
        assert "off" not in s.refresh()
        assert _mode(s.engine) == "cool"

    def test_mild_day_occupant_heat_is_still_a_confirmed_override(self):
        s = _Scene(day="mild", indoor=72.0, ca_first="cool")
        s.occupant_sets("heat", old_mode="cool")
        s.confirm_elapses()
        assert "override_confirmed" in s.events
        assert s.engine._manual_override_mode == "heat"
        assert s.engine._grace_active


class TestOffWhileCaHoldsHvacOff:
    """CA's own plan is HVAC off (open-window hold): the occupant's Off agrees with it, so it is not an override."""

    def test_off_while_window_pause_in_force_self_resolves(self):
        s = _Scene(day="mild", indoor=72.0, ca_first="cool")
        s.occupant_sets("off", old_mode="cool")
        s.engine._paused_by_door = True  # a monitored window is open and CA is holding HVAC off
        s.confirm_elapses()
        assert "override_self_resolved" in s.events
        assert "override_confirmed" not in s.events
        assert not s.engine._manual_override_active
        assert not s.engine._grace_active

    def test_confirmed_off_is_adopted_once_ca_itself_holds_hvac_off(self):
        s = _Scene(day="mild", indoor=72.0, ca_first="cool")
        s.occupant_sets("off", old_mode="cool")
        s.confirm_elapses()
        assert s.engine._manual_override_active
        s.engine._paused_by_door = True  # now a window opens: CA's plan is off as well
        s.refresh()
        assert "override_adopted" in s.events
        assert not s.engine._manual_override_active
