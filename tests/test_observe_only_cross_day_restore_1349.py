"""Issue #1349: observe-only mode survives a restart on a later calendar day.

Occupant impact: the owner switched Automation off so Climate Advisor only watches.
Home Assistant went down before midnight and came back after it. Before the fix the
switch came back on with no notice and, once the 5-minute startup window ended,
Climate Advisor started commanding the thermostat (an Off thermostat was switched to
heat 70 F in the reproduction). These tests drive the real ``async_restore_state()``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.learning import LearningState  # noqa: E402

_NOW = datetime(2026, 1, 16, 0, 35, 0, tzinfo=UTC)
_TODAY = "2026-01-16"


def _coord(persisted_state: dict):
    """Coordinator stub with the real async_restore_state() bound (test_coordinator_health.py pattern)."""
    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.is_running = False
    coord._executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    coord.async_load_investigation_reports = AsyncMock()  # Issue #1415: restore awaits the per-zone loader
    coord.config = {"climate_entity": "climate.thermostat"}
    coord.learning = MagicMock()
    coord.learning._state = LearningState()
    coord.claude_client = None
    coord._rejection_log = {}
    coord._state_persistence = MagicMock()
    coord._state_persistence.load = MagicMock(return_value=persisted_state)
    coord._event_log = []
    coord._emit_event = MagicMock()
    coord.last_update_error = None
    coord.last_update_error_time = None
    coord.consecutive_failure_count = 0
    # The pre-restore defaults from __init__: automation on, engine live.
    coord._automation_enabled = True
    coord.automation_engine = MagicMock()
    coord.automation_engine.dry_run = False
    coord.async_restore_state = types.MethodType(ClimateAdvisorCoordinator.async_restore_state, coord)
    return coord


def _restore(state: dict):
    coord = _coord(state)
    with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_NOW):
        asyncio.run(coord.async_restore_state())
    return coord


@pytest.mark.parametrize(
    "saved_date",
    [
        pytest.param("2026-01-15", id="yesterday"),
        pytest.param("2026-01-12", id="four_days_old"),
        pytest.param("2025-12-31", id="previous_year"),
    ],
)
def test_observe_only_survives_restart_on_a_later_day(saved_date, caplog):
    """Fails on main: the restore sat below the ``state_date != today_str`` return."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor.coordinator")
    coord = _restore({"date": saved_date, "last_saved": f"{saved_date}T23:30:00", "automation_enabled": False})

    assert coord._automation_enabled is False
    assert coord.automation_engine.dry_run is True
    assert "Observe-only mode restored (automation off)" in caplog.text
    # Everything else stays same-day only: the engine's automation_state is not restored across days.
    coord.automation_engine.restore_state.assert_not_called()


def test_observe_only_survives_cross_day_restart_with_unit_change():
    """A unit change does not touch observe-only (the #1015 rule keeps automation settings)."""
    coord = _restore(
        {"date": "2026-01-15", "temp_unit": "celsius", "automation_enabled": False, "last_saved": "x"},
    )
    assert coord.automation_engine.dry_run is True


def test_same_day_observe_only_control():
    """Control: the same-day path keeps working (it already did before the fix)."""
    coord = _restore({"date": _TODAY, "last_saved": f"{_TODAY}T00:20:00", "automation_enabled": False})
    assert coord._automation_enabled is False
    assert coord.automation_engine.dry_run is True


@pytest.mark.parametrize("saved_date", [_TODAY, "2026-01-15"], ids=["same_day", "yesterday"])
def test_automation_on_control(saved_date, caplog):
    """Control: automation on stays on, on both paths, and logs no observe-only line."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor.coordinator")
    coord = _restore({"date": saved_date, "last_saved": "x", "automation_enabled": True})
    assert coord._automation_enabled is True
    assert coord.automation_engine.dry_run is False
    assert "Observe-only mode restored" not in caplog.text


def test_missing_key_defaults_to_automation_on():
    """A state file written before observe-only existed keeps the old default (on)."""
    coord = _restore({"date": "2026-01-15", "last_saved": "x"})
    assert coord._automation_enabled is True
    assert coord.automation_engine.dry_run is False


def test_no_state_file_leaves_defaults():
    """No state file: nothing to restore, the __init__ default (on) stands."""
    coord = _restore({})
    assert coord._automation_enabled is True
    assert coord.automation_engine.dry_run is False
