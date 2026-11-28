"""Issue #1739: occupancy toggles ignore unavailable/unknown and a change must hold 60 s before it acts.

Occupant view: with vacation mode on, a HomeKit glitch (the toggle dropping to unavailable, or flipping for a second)
used to move the house to home and send "Welcome home!". Now an unavailable toggle keeps its last value and a real
change takes effect only if it is still there 60 seconds later.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.const import (
    CONF_HOME_TOGGLE,
    CONF_VACATION_TOGGLE,
    OCCUPANCY_AWAY,
    OCCUPANCY_HOME,
    OCCUPANCY_TOGGLE_CONFIRM_SECONDS,
    OCCUPANCY_VACATION,
)
from tests.test_occupancy import _make_coordinator, _make_occupancy_event, _make_state

_LOGGER_NAME = "custom_components.climate_advisor.coordinator"
_VAC = "input_boolean.vacation"
_HOME = "input_boolean.home_mode"


class _Clock:
    """Stands in for ``async_call_later``: records timers and fires them on demand."""

    def __init__(self) -> None:
        self.timers: list[dict[str, Any]] = []

    def __call__(self, hass: Any, delay: float, cb: Any) -> Any:
        timer = {"delay": delay, "cb": cb, "cancelled": False}
        self.timers.append(timer)

        def cancel() -> None:
            timer["cancelled"] = True

        return cancel

    def live(self) -> list[dict[str, Any]]:
        return [t for t in self.timers if not t["cancelled"]]

    def fire_confirmation(self, coord: Any) -> None:
        """Fire the one live timer and run the task it schedules."""
        (timer,) = self.live()
        timer["cancelled"] = True
        tasks: list[Any] = []
        coord.hass.async_create_task = tasks.append
        timer["cb"](None)
        for coro in tasks:
            asyncio.run(coro)


def _setup(states: dict[str, str], *, mode: str, config: dict[str, str]) -> tuple[Any, _Clock, dict[str, Any]]:
    coord = _make_coordinator(config_overrides=config, state_map=states)
    live = {eid: _make_state(val) for eid, val in states.items()}
    coord.hass.states.get = live.get
    coord._occupancy_mode = mode
    coord.automation_engine.handle_occupancy_home = AsyncMock()  # the only sender of "Welcome home!"
    coord.automation_engine.handle_occupancy_away = AsyncMock()
    coord.automation_engine.handle_occupancy_vacation = AsyncMock()
    return coord, _Clock(), live


def _event(coord: Any, clock: _Clock, entity: str) -> None:
    with patch("custom_components.climate_advisor.coordinator.async_call_later", side_effect=clock):
        asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event(entity)))


def _vacation_house() -> tuple[Any, _Clock, dict[str, Any]]:
    coord, clock, live = _setup({_VAC: "on"}, mode=OCCUPANCY_VACATION, config={CONF_VACATION_TOGGLE: _VAC})
    coord._compute_occupancy_mode()  # a real "on" reading was seen (as at startup)
    return coord, clock, live


# ── 1. unavailable / unknown are not readings ──────────────────────────────


def test_unavailable_vacation_toggle_keeps_vacation_and_sends_no_welcome():
    coord, clock, live = _vacation_house()
    live[_VAC] = _make_state("unavailable")
    coord.hass.states.get = live.get
    _event(coord, clock, _VAC)
    assert coord._compute_occupancy_mode() == OCCUPANCY_VACATION
    assert clock.live() == []  # nothing even armed
    assert coord._occupancy_mode == OCCUPANCY_VACATION
    coord.automation_engine.handle_occupancy_home.assert_not_called()


def test_unknown_vacation_toggle_keeps_vacation_and_warns(caplog):
    coord, clock, live = _vacation_house()
    live[_VAC] = _make_state("unknown")
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _event(coord, clock, _VAC)
    assert clock.live() == []
    coord.automation_engine.handle_occupancy_home.assert_not_called()
    assert any(_VAC in r.message and "unknown" in r.message for r in caplog.records if r.levelno == logging.WARNING)


def test_unavailable_then_back_on_changes_nothing():
    coord, clock, live = _vacation_house()
    live[_VAC] = _make_state("unavailable")
    _event(coord, clock, _VAC)
    live[_VAC] = _make_state("on")
    _event(coord, clock, _VAC)
    assert clock.live() == []
    assert coord._occupancy_mode == OCCUPANCY_VACATION
    coord.automation_engine.handle_occupancy_home.assert_not_called()
    coord.automation_engine.handle_occupancy_vacation.assert_not_called()


def test_toggle_that_never_reported_and_nothing_restored_counts_as_off():
    coord, _clock, _live = _setup({_VAC: "unavailable"}, mode=OCCUPANCY_HOME, config={CONF_VACATION_TOGGLE: _VAC})
    assert coord._is_toggle_on(_VAC, False) is False
    assert coord._compute_occupancy_mode() == OCCUPANCY_HOME


def test_last_known_value_is_the_effective_one_after_invert():
    coord, _clock, live = _setup({_VAC: "off"}, mode=OCCUPANCY_HOME, config={})
    assert coord._is_toggle_on(_VAC, True) is True  # inverted off = on
    live[_VAC] = _make_state("unavailable")
    assert coord._is_toggle_on(_VAC, True) is True


# ── 2. the 60 s confirmation ───────────────────────────────────────────────


def test_flip_that_reverts_within_the_window_does_nothing_and_logs_one_info(caplog):
    coord, clock, live = _vacation_house()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        live[_VAC] = _make_state("off")
        _event(coord, clock, _VAC)
        assert [t["delay"] for t in clock.live()] == [OCCUPANCY_TOGGLE_CONFIRM_SECONDS]
        live[_VAC] = _make_state("on")
        _event(coord, clock, _VAC)
    assert clock.live() == []  # the confirmation was cancelled, nothing left to fire
    assert coord._occupancy_mode == OCCUPANCY_VACATION
    coord.automation_engine.handle_occupancy_home.assert_not_called()
    reverted = [r for r in caplog.records if "reverted" in r.message]
    assert len(reverted) == 1
    assert reverted[0].levelno == logging.INFO
    assert _VAC in reverted[0].message


def test_change_held_for_the_window_acts_exactly_as_before():
    coord, clock, live = _vacation_house()
    live[_VAC] = _make_state("off")
    _event(coord, clock, _VAC)
    # Nothing has happened yet: the mode only moves when the window ends.
    assert coord._occupancy_mode == OCCUPANCY_VACATION
    coord.automation_engine.handle_occupancy_home.assert_not_called()

    clock.fire_confirmation(coord)

    assert coord._occupancy_mode == OCCUPANCY_HOME
    coord.automation_engine.handle_occupancy_home.assert_called_once()  # the welcome
    coord.automation_engine.set_occupancy_mode.assert_called_with(OCCUPANCY_HOME)
    coord._async_save_state.assert_awaited()


def test_away_timer_starts_only_after_confirmation():
    coord, clock, live = _setup({_HOME: "on"}, mode=OCCUPANCY_HOME, config={CONF_HOME_TOGGLE: _HOME})
    coord._compute_occupancy_mode()
    live[_HOME] = _make_state("off")
    _event(coord, clock, _HOME)
    assert coord._occupancy_away_timer_cancel is None
    with patch("custom_components.climate_advisor.coordinator.async_call_later", side_effect=clock):
        clock.fire_confirmation(coord)
    assert coord._occupancy_mode == OCCUPANCY_AWAY
    assert coord._occupancy_away_timer_cancel is not None  # the 15-minute timer, started after the 60 s
    coord.automation_engine.handle_occupancy_away.assert_not_called()


def test_second_change_within_the_window_re_arms_the_confirmation():
    coord, clock, live = _setup(
        {_VAC: "on", _HOME: "on"},
        mode=OCCUPANCY_VACATION,
        config={CONF_VACATION_TOGGLE: _VAC, CONF_HOME_TOGGLE: _HOME},
    )
    coord._compute_occupancy_mode()
    live[_VAC] = _make_state("off")
    _event(coord, clock, _VAC)
    first = clock.live()[0]
    live[_HOME] = _make_state("off")  # a different change, still not the current mode (vacation -> away)
    _event(coord, clock, _HOME)
    assert first["cancelled"] is True
    assert len(clock.live()) == 1 and clock.live()[0] is not first  # one fresh full-length window
    assert clock.live()[0]["delay"] == OCCUPANCY_TOGGLE_CONFIRM_SECONDS
    coord.automation_engine.handle_occupancy_home.assert_not_called()
    coord.automation_engine.handle_occupancy_away.assert_not_called()


def test_confirmation_recomputes_so_a_change_that_reverted_without_an_event_does_nothing():
    coord, clock, live = _vacation_house()
    live[_VAC] = _make_state("off")
    _event(coord, clock, _VAC)
    live[_VAC] = _make_state("on")  # back before the timer fires
    clock.fire_confirmation(coord)
    assert coord._occupancy_mode == OCCUPANCY_VACATION
    coord.automation_engine.handle_occupancy_home.assert_not_called()


# ── 3. cancellation ────────────────────────────────────────────────────────


def test_pending_confirmation_cancelled_on_shutdown_and_a_late_fire_is_refused(tmp_path):
    coord, clock, live = _vacation_house()
    coord.learning._db_path = tmp_path / "climate_advisor_learning.json"
    live[_VAC] = _make_state("off")
    _event(coord, clock, _VAC)
    (timer,) = clock.live()
    coord.automation_engine.cleanup = MagicMock()
    coord._flush_hvac_runtime = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    from datetime import datetime

    with patch("custom_components.climate_advisor.automation.dt_util.now", return_value=datetime(2026, 1, 1, 12, 0)):
        asyncio.run(coord.async_shutdown())
    assert timer["cancelled"] is True
    assert coord._occupancy_toggle_confirm_cancel is None
    # even a leaked timer cannot move the mode: the zone-scoped wrapper refuses after shutdown
    tasks: list[Any] = []
    coord.hass.async_create_task = tasks.append
    timer["cb"](None)
    assert tasks == []
    coord.automation_engine.handle_occupancy_home.assert_not_called()


def test_pending_confirmation_cancelled_when_home_assistant_stops():
    coord, clock, live = _vacation_house()
    live[_VAC] = _make_state("off")
    _event(coord, clock, _VAC)
    (timer,) = clock.live()
    coord.hass.async_create_task = MagicMock(side_effect=lambda c: c.close())
    coord._async_homeassistant_stop(MagicMock())
    assert timer["cancelled"] is True
    assert coord._occupancy_toggle_confirm_cancel is None


# ── 4. startup ─────────────────────────────────────────────────────────────


def test_startup_unavailable_vacation_toggle_with_restored_vacation_stays_vacation():
    coord, _clock, _live = _setup({_VAC: "unavailable"}, mode=OCCUPANCY_VACATION, config={CONF_VACATION_TOGGLE: _VAC})
    coord._occupancy_mode_restored = True
    coord._seed_unresolved_toggles_from_restored_mode()
    assert coord._compute_occupancy_mode() == OCCUPANCY_VACATION


def test_startup_unavailable_vacation_toggle_with_nothing_restored_uses_old_behaviour():
    coord, _clock, _live = _setup({_VAC: "unavailable"}, mode=OCCUPANCY_HOME, config={CONF_VACATION_TOGGLE: _VAC})
    coord._occupancy_mode_restored = False
    coord._seed_unresolved_toggles_from_restored_mode()
    assert coord._compute_occupancy_mode() == OCCUPANCY_HOME  # off


def test_startup_seed_does_not_override_a_toggle_that_reports():
    coord, _clock, _live = _setup({_VAC: "off"}, mode=OCCUPANCY_VACATION, config={CONF_VACATION_TOGGLE: _VAC})
    coord._occupancy_mode_restored = True
    coord._seed_unresolved_toggles_from_restored_mode()
    assert coord._compute_occupancy_mode() == OCCUPANCY_HOME  # the live reading wins, as before (#1354)


def test_startup_restored_away_with_unavailable_home_toggle_stays_away():
    coord, _clock, _live = _setup({_HOME: "unavailable"}, mode=OCCUPANCY_AWAY, config={CONF_HOME_TOGGLE: _HOME})
    coord._occupancy_mode_restored = True
    coord._seed_unresolved_toggles_from_restored_mode()
    assert coord._compute_occupancy_mode() == OCCUPANCY_AWAY
