"""Issue #1216: a second thermostat change inside one override grace must win (Rule 2: newest occupant action wins).

Occupant story: the occupant sets the thermostat to Cool (confirmed after the confirmation window, its manual grace
starts), then a few minutes later to Off. The Off must be confirmed as the newest override. It was thrown away two
ways, and Climate Advisor then acted as if the occupant had never switched the thermostat Off:

- the orphaned-grace watchdog: coordinator Fix D (#282) clears the Cool override but keeps its grace running for the
  Off, which is only PENDING confirmation. The next coordinator update (``_check_orphaned_grace``, #508) read that as
  "a grace protecting nothing", cancelled it and resolved the override/grace FSM as an expiry, dropping the Off;
- the Cool grace running out while the Off is still pending (the mirror): the #1156 keep rule kept a pending change
  only under a grace that was not the occupant's own, so the Off was cleared with the Cool override's grace.

Real engine, virtual clock and the engine's real timers (the #1205 harness); the coordinator's watchdog runs on a
stub holding the real engine.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.const import CONF_MANUAL_GRACE_PERIOD, CONF_OVERRIDE_CONFIRM_PERIOD
from tests.test_restore_resolved_band import _classification
from tests.test_stale_fan_marker_1205 import _MODES, _fix_d, _Home, _set_thermostat, clock, sched  # noqa: F401

_CONFIRM_S = 600
_GRACE_S = 1800


def _coordinator_class():
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    return ClimateAdvisorCoordinator


def _watchdog(home: _Home) -> None:
    """One coordinator update cycle's orphaned-grace check, on the real engine."""
    stub = SimpleNamespace(
        automation_engine=home.engine, _emit_event=lambda name, data: home.events.append((name, data))
    )
    _coordinator_class()._check_orphaned_grace(stub)
    home.drain()


def _step(home: _Home, sch, seconds: float, *, watchdog: bool = True) -> None:
    """Advance in 30 s coordinator cycles, firing the engine's due timers and the watchdog each cycle."""
    left = seconds
    while left > 0:
        dt = min(30.0, left)
        home.clock.advance(dt)
        sch.fire_due(home)
        if watchdog:
            _watchdog(home)
        left -= dt


def _cool_confirmed(fan_mode, clock, sched, grace_s: int = _GRACE_S) -> _Home:  # noqa: F811
    home = _Home(fan_mode, clock, **{CONF_OVERRIDE_CONFIRM_PERIOD: _CONFIRM_S, CONF_MANUAL_GRACE_PERIOD: grace_s})
    engine = home.engine
    engine._test_state.state = "off"
    home.window_open = False
    engine._paused_by_door = False
    _set_thermostat(home, "cool")
    _step(home, sched, _CONFIRM_S + 30)
    assert engine._manual_override_active and engine._manual_override_mode == "cool", "precondition: Cool confirmed"
    assert engine._grace_active and engine._grace_protects_override, "precondition: Cool's manual grace runs"
    return home


def _assert_off_won(home: _Home) -> None:
    engine = home.engine
    assert engine._manual_override_active and engine._manual_override_mode == "off", (
        "the occupant's newest change (Off) was dropped: "
        f"override={engine._manual_override_active} mode={engine._manual_override_mode} "
        f"pending={engine._override_confirm_pending}"
    )
    assert engine._grace_active and engine._grace_protects_override, "the Off override's own grace must run"
    assert not [n for n, _ in home.events if n == "stuck_grace_recovered"], "the watchdog reaped the grace"


@pytest.mark.parametrize("fan_mode", _MODES)
def test_control_a_single_change_is_confirmed_and_its_grace_survives_the_watchdog(clock, sched, fan_mode):  # noqa: F811
    """Control: one change, confirmed; the watchdog leaves its grace alone for the whole grace."""
    home = _cool_confirmed(fan_mode, clock, sched)
    _step(home, sched, 300)
    assert home.engine._manual_override_active and home.engine._grace_active


@pytest.mark.parametrize("fan_mode", _MODES)
def test_a_second_change_inside_the_grace_is_confirmed_as_the_newest_override(clock, sched, fan_mode):  # noqa: F811
    """Cool confirmed, Off five minutes into its grace: after the confirmation window the Off is the override."""
    home = _cool_confirmed(fan_mode, clock, sched)
    _step(home, sched, 300)
    _fix_d(home, "off")
    assert home.engine._override_confirm_pending, "precondition: the Off is pending confirmation"
    _step(home, sched, _CONFIRM_S + 30)
    _assert_off_won(home)


@pytest.mark.parametrize("fan_mode", _MODES)
def test_a_second_change_survives_the_first_grace_running_out_before_it_is_confirmed(clock, sched, fan_mode):  # noqa: F811
    """Off made one minute before Cool's grace ends (no watchdog in play): the Cool grace ends, the Off still wins."""
    home = _cool_confirmed(fan_mode, clock, sched, grace_s=900)
    _step(home, sched, 900 - 60, watchdog=False)
    _fix_d(home, "off")
    assert home.engine._override_confirm_pending, "precondition: the Off is pending confirmation"
    _step(home, sched, _CONFIRM_S + 30, watchdog=False)
    _assert_off_won(home)


@pytest.mark.parametrize("fan_mode", _MODES)
@pytest.mark.parametrize("second", ["heat", "off"])
def test_window_open_pending_change_runs_until_its_own_grace_ends(clock, sched, fan_mode, second):  # noqa: F811
    """Owner decision (option A): with a window open, Cool's grace ending does not pause over the occupant's still
    pending second change; it is confirmed, gets its own grace, and Rule 3 pauses heat/cool when THAT grace ends."""
    home = _Home(fan_mode, clock, **{CONF_OVERRIDE_CONFIRM_PERIOD: _CONFIRM_S, CONF_MANUAL_GRACE_PERIOD: 3600})
    engine = home.engine
    engine._last_outdoor_temp = 95.0  # hot outside: no natural ventilation, a plain open-window pause
    engine._test_state.state = "off"
    home.window_open = False
    engine._paused_by_door = False
    _set_thermostat(home, "cool")
    _step(home, sched, _CONFIRM_S + 30, watchdog=False)
    assert engine._manual_override_mode == "cool" and engine._grace_active, "precondition: Cool confirmed"
    home.open_window()
    _step(home, sched, (datetime.fromisoformat(engine._grace_end_time) - home.clock.now).total_seconds() - 90)
    _fix_d(home, second)
    _step(home, sched, 150)  # Cool's grace has ended
    assert engine._override_confirm_pending and engine._test_state.state == second, "the pending change was dropped"
    assert not engine._paused_by_door, "paused over the occupant's pending manual change"
    _step(home, sched, _CONFIRM_S - 120)
    assert engine._manual_override_active and engine._manual_override_mode == second and engine._grace_active
    _step(home, sched, (datetime.fromisoformat(engine._grace_end_time) - home.clock.now).total_seconds() + 60)
    assert engine._paused_by_door and engine._test_state.state == "off", "Rule 3: the window must pause heat/cool"


@pytest.mark.parametrize("fan_mode", _MODES)
def test_a_second_change_back_to_cas_plan_self_resolves_and_the_old_grace_is_reaped(clock, sched, fan_mode):  # noqa: F811
    """PATH B: the second change matches what CA wants, so it is a transient; the superseded grace is then reaped."""
    home = _cool_confirmed(fan_mode, clock, sched)
    engine = home.engine
    engine._current_classification = _classification("cold")  # CA's plan is heat
    _step(home, sched, 300)
    _fix_d(home, "heat")
    _step(home, sched, _CONFIRM_S + 30)
    assert not engine._override_confirm_pending and not engine._manual_override_active
    assert not engine._grace_active, "the superseded grace must not outlive the self-resolved change"


@pytest.mark.parametrize("fan_mode", _MODES)
def test_the_occupants_fan_run_still_ends_at_its_deadline_after_a_self_resolved_second_change(clock, sched, fan_mode):  # noqa: F811
    """A 1 h fan run, Cool confirmed (its grace replaces the run's), then back to Heat (CA's plan) a minute before
    Cool's grace ends. The run is a separate request (#1205 F3): it must end at its own deadline, not run on."""
    home = _Home(fan_mode, clock, **{CONF_OVERRIDE_CONFIRM_PERIOD: _CONFIRM_S, CONF_MANUAL_GRACE_PERIOD: 900})
    engine = home.engine
    engine._current_classification = _classification("cold")
    engine._test_state.state = "heat"
    home.window_open = False
    engine._paused_by_door = False
    engine.handle_fan_manual_override(
        fan_before="off",
        fan_after="on",
        duration_override=3600.0,
        remote_timer_hours=1.0,
        is_remote_event=True,
        hvac_fan=home.hvac_fan,
    )
    home.drain()
    run_end = engine._fan_override_until
    _step(home, sched, 60, watchdog=False)
    _set_thermostat(home, "cool")
    _step(home, sched, _CONFIRM_S + 30, watchdog=False)
    assert engine._manual_override_mode == "cool" and engine._last_grace_trigger == "override_confirmed"
    _step(home, sched, 900 - 60 - 30, watchdog=False)
    _fix_d(home, "heat")
    _step(home, sched, _CONFIRM_S + 30, watchdog=False)
    assert engine._fan_override_active and engine._fan_override_until == run_end, "the run continues to its deadline"
    _step(home, sched, (run_end - home.clock.now).total_seconds() + 120, watchdog=False)
    assert not engine._fan_override_active, "the occupant's fan run was stranded past its deadline"
