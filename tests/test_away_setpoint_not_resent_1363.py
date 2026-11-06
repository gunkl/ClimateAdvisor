"""Issue #1363: Away/Vacation does not re-send the same setpoint every 5 minutes.

Occupant impact: while the house is Away or on Vacation, Climate Advisor sent the same setpoint to the thermostat
about every 5 minutes (12 identical writes an hour; 64 in 5.3 h per zone in production), because the classification
pass's Away/Vacation branch returned before cancelling the 5-minute revisit its own write had scheduled. These tests
drive the real ``apply_classification()`` / ``handle_occupancy_away()``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_HOME, OCCUPANCY_VACATION
from tests.test_warm_day_setback import _make_classification, _make_engine, _temp_calls


def _engine(mode: str):
    engine = _make_engine(current_mode="heat")
    engine._revisit_callback = AsyncMock()
    engine.set_occupancy_mode(mode)
    return engine


@pytest.mark.parametrize("mode", [OCCUPANCY_AWAY, OCCUPANCY_VACATION])
def test_classification_pass_cancels_its_revisit_when_away(mode):
    """Fails on main: the reapply wrote the setback and left its 5-minute revisit armed, so the revisit re-ran the
    pass, which wrote again and armed the next one."""
    engine = _engine(mode)
    asyncio.run(engine.apply_classification(_make_classification(day_type="cold", hvac_mode="heat")))

    assert _temp_calls(engine), "precondition: the away/vacation setback is (re)applied"
    assert engine._revisit_cancel is None, "the pass left its own 5-minute revisit armed"


def test_home_control_cancels_its_revisit():
    """Control: the Home path already cancelled its revisit (Issue #96 Root Cause E)."""
    engine = _engine(OCCUPANCY_HOME)
    asyncio.run(engine.apply_classification(_make_classification(day_type="cold", hvac_mode="heat")))
    assert _temp_calls(engine)
    assert engine._revisit_cancel is None


def test_leaving_keeps_one_follow_up_check():
    """When the occupant leaves (the toggle handler calls handle_occupancy_away() directly, not the classification
    pass), the one 5-minute follow-up after the setback write is kept; only the classification pass cancels it."""
    engine = _engine(OCCUPANCY_HOME)
    engine._current_classification = _make_classification(day_type="cold", hvac_mode="heat")
    asyncio.run(engine.handle_occupancy_away())
    assert _temp_calls(engine), "precondition: leaving applies the setback"
    assert engine._revisit_cancel is not None, "leaving should still schedule its one follow-up check"
