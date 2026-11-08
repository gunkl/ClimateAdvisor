"""Issue #1433: the away setback waits for the 15-minute away timer, whoever asks for it.

Occupant story (production, 2026-11-07, Zone 1): you left at 11:18:46 and the 30-minute cycle at 11:23:12 logged
"Away mode -- reapplying setback instead of comfort temps", 4.5 minutes into the 15-minute grace. The windows were
open, so nothing reached the thermostat that day; on a heating day the heat is set back to the away value while the
house is still inside its grace (a short errand, or a phone dropping off the network, sets the heat back) and the
chart draws the home band for that timer (Issue #1425). Origin: Issue #85 (commit 0959140) synced the engine's
occupancy at once and made the cycle reapply the Away setback, without knowing the coordinator's timer was pending.

The engine now holds ``_away_setback_pending`` while the coordinator's timer runs. ``handle_occupancy_away()`` (every
caller: the cycle's reapply, bedtime, pre-cool, a time-of-use start) and the Away time-of-use bank send nothing until
the timer clears it. Vacation (no timer), Guest and Home are unaffected. Real-HA timeline:
``tests_ha/test_away_grace_periodic_1433.py``; Tier A: ``tools/simulations/pending/issue_1433_*``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from tests.test_occupancy_restore_hold import _classification, _engine


def _leaving_engine():
    """Engine that has just gone Away (the timer is about to start); nothing sent yet."""
    eng, events, call = _engine()
    eng.set_occupancy_mode("home")
    eng.set_occupancy_mode("away")
    eng._away_setback_sent = False
    call.reset_mock()
    return eng, events, call


class TestEngineWaitsForTheAwayTimer:
    def test_handler_sends_nothing_while_the_timer_runs(self, caplog):
        eng, events, call = _leaving_engine()
        eng.set_away_setback_pending(True)
        with caplog.at_level("INFO"):
            asyncio.run(eng.handle_occupancy_away())
        assert call.await_count == 0, "#1433: the setback was sent inside the away timer"
        assert "occupancy_setback" not in [e for e, _ in events]
        assert eng._away_setback_sent is False
        assert "away setback pending" in caplog.text

    def test_the_timer_clearing_the_flag_lets_the_setback_through(self):
        eng, _events, call = _leaving_engine()
        eng.set_away_setback_pending(True)
        eng.set_away_setback_pending(False)  # what the coordinator's timer does before it calls the handler
        asyncio.run(eng.handle_occupancy_away())
        assert call.await_count >= 1, "the timer's own setback must still be sent"
        assert eng._away_setback_sent is True

    def test_the_periodic_cycle_writes_nothing_while_the_timer_runs(self, caplog):
        eng, _events, call = _leaving_engine()
        eng._sync_paused_by_door_with_live_sensors = AsyncMock()
        eng.set_away_setback_pending(True)
        with caplog.at_level("INFO"):
            asyncio.run(eng.apply_classification(_classification("heat")))
        writes = [c for c in call.await_args_list if c.args[:2] == ("climate", "set_temperature")]
        assert not writes, f"#1433: the 30-minute cycle sent the setback inside the away timer: {writes}"
        assert "Away mode — reapplying setback" not in caplog.text
        assert "apply_classification: away setback pending" in caplog.text

    def test_the_periodic_cycle_reapplies_the_setback_once_really_away(self):
        """Leaving mirror: after the timer the cycle keeps Away (it is not undone)."""
        eng, _events, call = _leaving_engine()
        eng._sync_paused_by_door_with_live_sensors = AsyncMock()
        asyncio.run(eng.apply_classification(_classification("heat")))
        writes = [c for c in call.await_args_list if c.args[:2] == ("climate", "set_temperature")]
        assert writes, "the cycle while really away must still reapply the setback (Issue #505)"

    def test_the_away_time_of_use_bank_waits_for_the_timer(self):
        eng, _events, call = _leaving_engine()
        eng._sync_paused_by_door_with_live_sensors = AsyncMock()
        eng.set_away_setback_pending(True)
        asyncio.run(eng.apply_tou_away_vacation_precondition(_classification("heat"), 63.0, "peak1", "heat"))
        assert call.await_count == 0, "#1433: the Away bank was written inside the away timer"


class TestOnlyAnAwayWaits:
    @pytest.mark.parametrize("mode", ["home", "guest", "vacation"])
    def test_a_change_out_of_away_clears_the_pending_timer(self, mode):
        eng, _events, _call = _leaving_engine()
        eng.set_away_setback_pending(True)
        eng.set_occupancy_mode(mode)
        assert eng._away_setback_pending is False

    @pytest.mark.parametrize("mode", ["home", "guest", "vacation"])
    def test_pending_is_ignored_unless_away(self, mode):
        eng, _events, _call = _engine()
        eng.set_occupancy_mode(mode)
        eng.set_away_setback_pending(True)
        assert eng._away_setback_pending is False

    def test_vacation_is_applied_at_once(self):
        """Vacation mirror: no timer, so a stale pending flag from an Away never delays the deep setback."""
        eng, _events, call = _leaving_engine()
        eng.set_away_setback_pending(True)
        eng.set_occupancy_mode("vacation")
        asyncio.run(eng.handle_occupancy_vacation())
        assert call.await_count >= 1


class TestCoordinatorTimerDrivesTheFlag:
    def test_cancelling_the_timer_clears_the_flag(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._occupancy_away_timer_cancel = MagicMock()
        engine = SimpleNamespace(set_away_setback_pending=MagicMock())
        coord.automation_engine = engine
        coord._cancel_occupancy_away_timer()
        engine.set_away_setback_pending.assert_called_once_with(False)
        assert coord._occupancy_away_timer_cancel is None

    def test_cancel_without_an_engine_is_safe(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._occupancy_away_timer_cancel = None
        coord._cancel_occupancy_away_timer()  # partially built coordinator: must not raise
