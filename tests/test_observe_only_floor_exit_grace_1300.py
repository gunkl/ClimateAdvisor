"""Issue #1300: the slow-loop nat-vent comfort-floor exit starts no automation grace in observe-only.

Occupant story: automation is off (observe-only) and a ventilation session ends because the house cooled to the
comfort floor. Climate Advisor sends no comfort restore, but it used to start a 5-minute "automation" grace as if it
had re-armed the heat, so the occupant's thermostat change in those minutes was read against a grace that should not
exist. With automation on the grace starts exactly as before.

The real-HA tests (``tests_ha/test_observe_only_no_automation_grace_1300.py``) cover the window-close restore and the
nat-vent sensors-closed exit; this one drives the REAL slow-loop entry point (``check_natural_vent_conditions``),
whose comfort-floor branch bypasses ``_exit_nat_vent()`` and is pre-empted by the fast-path floor exit in a live house.
"""

from __future__ import annotations

import pytest

from tests.test_protect_manual_change import _natvent_engine, _slow_loop, timers  # noqa: F401 (fixture)


@pytest.mark.parametrize("dry_run", [True, False], ids=["observe_only", "automation_on"])
def test_slow_loop_comfort_floor_exit_grace_only_when_the_restore_was_sent(timers, dry_run):  # noqa: F811
    engine = _natvent_engine()
    engine._current_classification.hvac_mode = "heat"  # the floor exit re-arms heat
    engine._window_open = False
    engine._last_outdoor_temp = 55.0
    engine.dry_run = dry_run
    assert not engine._manual_change_protected()
    engine.hass.services.async_call.reset_mock()

    _slow_loop(engine, indoor=60.0)

    assert engine._natural_vent_active is False, "precondition: the comfort-floor exit ran"
    automation_grace = bool(engine._grace_active) and engine._last_resume_source == "automation"
    if dry_run:
        assert not automation_grace, (
            f"observe-only started a {engine._last_grace_trigger!r} grace for a restore it never sent"
        )
    else:
        assert automation_grace and engine._last_grace_trigger == "nat_vent_exit_resume", (
            "automation on: the post-restore automation grace must still start"
        )
