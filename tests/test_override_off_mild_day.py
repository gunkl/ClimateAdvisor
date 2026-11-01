"""Issue #1159: an occupant's Off on a mild day is a real manual change, kept through the manual grace.

Occupant framing: on a mild day the day label is "off", yet Climate Advisor arms the comfort band
(heat or cool). If you switch the thermostat Off, that is a decision you made -- exactly as on a hot
or cold day. Climate Advisor must confirm it, keep it for the manual-change pause, and only then resume;
it must not switch heating or cooling back on at the next half-hourly refresh. There is no exception.

Real ``AutomationEngine`` + a fake thermostat that follows the engine's commands (helpers shared with
tests/test_comfort_settling_guard.py); assertions are on what the thermostat is told, the emitted
events and the override flags.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
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
    _run,
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

    def _patch_all(self):
        stack = ExitStack()
        for p in self._patches():
            stack.enter_context(p)
        return stack

    def _patches(self):
        return (
            patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
            patch(
                f"{_AUTOMATION}.async_call_later",
                side_effect=lambda _h, _d, fn: self.timers.append(fn) or MagicMock(),
            ),
            patch(f"{_AUTOMATION}.dt_util.now", side_effect=self.clock),
            # a second manual grace replacing a live one parses the first's end time (the stub returns a mock)
            patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
        )

    def occupant_sets(self, new_mode: str, *, old_mode: str) -> None:
        """The thermostat changes to ``new_mode`` and the engine starts override confirmation."""
        self.engine._test_state.state = new_mode
        with self._patch_all():
            self.engine.handle_manual_override(
                old_mode=old_mode,
                new_mode=new_mode,
                classification_mode=self.engine._current_classification.hvac_mode,
            )

    def confirm_elapses(self) -> None:
        self.clock.advance(minutes=10)
        with self._patch_all():
            self.timers[-1](None)  # the confirmation-window timer

    def grace_expires(self) -> None:
        self.clock.advance(minutes=60)
        with self._patch_all():
            self.engine._on_grace_expired("manual", 3600, False)

    def window(self, *, open_: bool) -> None:
        """A monitored door/window is open (debounce settled) or closed, as the engine's live sensor check sees it."""
        self.engine._sensor_check_callback = lambda: open_

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


class TestOffWithOpenWindowIsTreatedAsOnHotDays:
    """An open window alone does not end an occupant's Off: only an actual CA door/window pause does.

    During a manual override CA makes no door/window pause (#1119), so a mild-day Off must behave exactly
    as on a hot or cold day: kept, nothing written, until the manual grace ends. The hot row is the control.
    """

    _DAYS = pytest.mark.parametrize(
        ("day", "indoor", "ca_first"),
        [("hot", 76.0, "cool"), ("mild", 72.0, "cool"), ("mild", 64.0, "heat")],
        ids=["hot_control", "mild_ca_in_cool", "mild_ca_in_heat"],
    )

    @_DAYS
    def test_confirmed_off_survives_window_open_settle_and_close(self, day, indoor, ca_first):
        s = _Scene(day=day, indoor=indoor, ca_first=ca_first)
        s.occupant_sets("off", old_mode=ca_first)
        s.confirm_elapses()
        assert s.engine._manual_override_mode == "off"

        s.window(open_=True)  # opens and settles
        assert s.refresh() == []
        assert not s.engine._paused_by_door  # CA does not pause HVAC inside the occupant's grace
        assert s.engine._manual_override_active
        assert s.engine._grace_active
        assert "override_adopted" not in s.events
        assert _mode(s.engine) == "off"

        s.window(open_=False)
        assert s.refresh() == []  # closing the window does not let CA resume early
        assert s.engine._manual_override_active
        assert _mode(s.engine) == "off"

        s.grace_expires()
        assert not s.engine._manual_override_active
        s.refresh()
        assert _mode(s.engine) == ca_first

    @_DAYS
    def test_off_during_heat_grace_with_window_open_is_confirmed(self, day, indoor, ca_first):
        s = _Scene(day=day, indoor=indoor, ca_first=ca_first)
        s.occupant_sets("heat", old_mode=ca_first)
        s.confirm_elapses()
        assert s.engine._manual_override_mode == "heat"
        s.window(open_=True)

        s.occupant_sets("off", old_mode="heat")
        s.confirm_elapses()

        assert s.events.count("override_confirmed") == 2
        assert "override_self_resolved" not in s.events
        assert s.engine._manual_override_active
        assert s.engine._manual_override_mode == "off"
        assert s.engine._grace_active
        assert s.refresh() == []
        assert _mode(s.engine) == "off"


class TestOffWithCaDoorWindowPauseIsTreatedAsOnHotDays:
    """An occupant's Off is always a manual change, even when a door or window opens around it.

    Two routes: the window opens inside the confirm window, or ``handle_door_window_open`` runs while the
    override is active. Since #1133 a pending or confirmed manual change is protected, so CA does not pause
    over it in either route (``_paused_by_door`` stays False). In both the Off is kept, never adopted, and
    nothing is written; the hot row is the control.
    """

    _DAYS = TestOffWithOpenWindowIsTreatedAsOnHotDays._DAYS

    def _open_window(self, s: _Scene) -> None:
        e = s.engine
        e._last_outdoor_temp = 65.0
        e.config["fan_entity"] = "fan.whf"
        s.window(open_=True)
        with (
            s._patch_all(),
            patch(f"{_AUTOMATION}.is_reactivation_locked_out", return_value=True),
            patch.object(type(e), "_nat_vent_may_reactivate", return_value=True),
        ):
            _run(e.handle_door_window_open("binary_sensor.window"))

    @_DAYS
    def test_window_opened_before_confirm_keeps_off_until_grace_expiry(self, day, indoor, ca_first):
        s = _Scene(day=day, indoor=indoor, ca_first=ca_first)
        s.occupant_sets("off", old_mode=ca_first)  # 12:00
        s.clock.advance(minutes=5)
        self._open_window(s)  # 12:05: the unconfirmed Off is protected (#1133), so CA does not pause over it
        assert not s.engine._paused_by_door
        s.clock.advance(minutes=5)
        s.confirm_elapses()
        assert "override_confirmed" in s.events
        assert "override_self_resolved" not in s.events
        assert s.engine._manual_override_active
        assert s.engine._manual_override_mode == "off"

        s.window(open_=False)  # 12:25
        with s._patch_all():
            _run(s.engine.handle_all_doors_windows_closed())
        assert s.refresh() == []  # 12:30-ish refresh
        assert s.engine._manual_override_active
        assert "override_adopted" not in s.events
        assert _mode(s.engine) == "off"

        s.grace_expires()
        assert not s.engine._manual_override_active
        s.refresh()
        assert _mode(s.engine) == ca_first

    @_DAYS
    def test_pause_during_grace_does_not_adopt_the_off(self, day, indoor, ca_first):
        s = _Scene(day=day, indoor=indoor, ca_first=ca_first)
        s.occupant_sets("off", old_mode=ca_first)
        s.confirm_elapses()
        n0 = len(_mode_writes(s.engine))
        self._open_window(s)  # handle_door_window_open while the override is active
        assert s.refresh() == []
        s.window(open_=False)
        with s._patch_all():
            _run(s.engine.handle_all_doors_windows_closed())
        assert s.refresh() == []
        assert len(_mode_writes(s.engine)) == n0
        assert s.engine._manual_override_active
        assert s.engine._manual_override_mode == "off"
        assert "override_adopted" not in s.events
        assert "override_cleared" not in s.events
        assert _mode(s.engine) == "off"
