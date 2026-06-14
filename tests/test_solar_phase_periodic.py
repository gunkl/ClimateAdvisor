"""Tests for periodic daily solar phase re-fit (Issue #310).

Verifies that _run_solar_phase_chart_log_fit(backfill=False) is called once per
calendar day after the one-shot startup backfill (_solar_phase_backfill=True),
and that _last_solar_phase_fit_date is correctly persisted and restored.

Gate tests call the real _maybe_run_periodic_solar_phase_fit() production method
via types.MethodType — so a regression in the gate logic would cause real failures.
Persistence tests call the real _build_state_dict() production method.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_coord_stub():
    """Minimal stub with the attributes _maybe_run_periodic_solar_phase_fit reads."""
    stub = MagicMock()
    stub._solar_phase_backfill = True
    stub._last_solar_phase_fit_date = None
    return stub


def _call_maybe_periodic(stub, fixed_now: datetime):
    """Bind and call the real _maybe_run_periodic_solar_phase_fit on a stub."""
    cls = _get_coordinator_class()
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    bound = types.MethodType(cls._maybe_run_periodic_solar_phase_fit, stub)
    with patch.object(mod, "dt_util") as mock_dt:
        mock_dt.now.return_value = fixed_now
        fixed_now.date.return_value = fixed_now.date() if not isinstance(fixed_now, MagicMock) else date(2026, 6, 13)
        bound()


# ---------------------------------------------------------------------------
# Gate tests — call the REAL _maybe_run_periodic_solar_phase_fit production method
# ---------------------------------------------------------------------------


class TestSolarPhasePeriodicRefit:
    """Periodic daily re-fit gate logic — exercises real production code."""

    def test_solar_phase_refit_called_on_new_day(self):
        """When _last_solar_phase_fit_date is yesterday, the periodic refit must fire."""
        stub = _make_coord_stub()
        stub._last_solar_phase_fit_date = date(2026, 6, 12)  # yesterday

        cls = _get_coordinator_class()
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        bound = types.MethodType(cls._maybe_run_periodic_solar_phase_fit, stub)

        with patch.object(mod, "dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 6, 13)
            bound()

        stub._run_solar_phase_chart_log_fit.assert_called_once_with(backfill=False)

    def test_solar_phase_refit_called_when_date_is_none(self):
        """When _last_solar_phase_fit_date is None (first periodic run), refit must fire."""
        stub = _make_coord_stub()
        stub._last_solar_phase_fit_date = None

        cls = _get_coordinator_class()
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        bound = types.MethodType(cls._maybe_run_periodic_solar_phase_fit, stub)

        with patch.object(mod, "dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 6, 13)
            bound()

        stub._run_solar_phase_chart_log_fit.assert_called_once_with(backfill=False)

    def test_solar_phase_refit_not_called_same_day(self):
        """When _last_solar_phase_fit_date is already today, the refit must NOT fire."""
        stub = _make_coord_stub()
        stub._last_solar_phase_fit_date = date(2026, 6, 13)  # today

        cls = _get_coordinator_class()
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        bound = types.MethodType(cls._maybe_run_periodic_solar_phase_fit, stub)

        with patch.object(mod, "dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 6, 13)
            bound()

        stub._run_solar_phase_chart_log_fit.assert_not_called()

    def test_solar_phase_refit_skipped_when_backfill_not_done(self):
        """When _solar_phase_backfill=False, periodic must not run."""
        stub = _make_coord_stub()
        stub._solar_phase_backfill = False
        stub._last_solar_phase_fit_date = None

        cls = _get_coordinator_class()
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        bound = types.MethodType(cls._maybe_run_periodic_solar_phase_fit, stub)

        with patch.object(mod, "dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 6, 13)
            bound()

        stub._run_solar_phase_chart_log_fit.assert_not_called()

    def test_solar_phase_fit_date_updated_after_refit(self):
        """After the periodic refit fires, _last_solar_phase_fit_date must be today."""
        stub = _make_coord_stub()
        stub._last_solar_phase_fit_date = date(2026, 6, 12)  # yesterday

        cls = _get_coordinator_class()
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        bound = types.MethodType(cls._maybe_run_periodic_solar_phase_fit, stub)

        with patch.object(mod, "dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 6, 13)
            bound()

        assert stub._last_solar_phase_fit_date == date(2026, 6, 13), (
            f"Expected date updated to 2026-06-13, got {stub._last_solar_phase_fit_date!r}"
        )


# ---------------------------------------------------------------------------
# State persistence / restore — call real _build_state_dict production method
# ---------------------------------------------------------------------------


_PATCH_DT_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"
_STABLE_NOW = datetime(2026, 6, 13, 10, 0, 0)
_STABLE_DATE_STR = "2026-06-13"


def _make_build_state_stub(mod, fit_date):
    """Minimal stub sufficient for _build_state_dict to run."""
    coord = MagicMock(spec=mod.ClimateAdvisorCoordinator)
    coord._solar_phase_backfill = True
    coord._last_solar_phase_fit_date = fit_date
    coord._current_classification = None
    coord._today_record = None
    coord._briefing_sent_today = False
    coord._last_briefing = ""
    coord._last_briefing_short = ""
    coord._briefing_day_type = None
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._occupancy_away_since = None
    coord.claude_client = None
    coord._pred_archive = {}
    coord._passive_k_backfilled = True
    coord._vent_k_backfilled = True
    coord._passive_k_backfill_v2 = True
    coord._vent_k_backfill_v2 = True
    coord._event_log = []
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord.automation_engine = MagicMock()
    coord.automation_engine.get_serializable_state.return_value = {}
    return coord


def _make_restore_coord_stub():
    """Minimal stub for async_restore_state tests (mirrors test_grace_restart_behavior pattern)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    ClimateAdvisorCoordinator = mod.ClimateAdvisorCoordinator

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {"climate_entity": "climate.test", "comfort_heat": 70, "comfort_cool": 75}

    ae = MagicMock()
    ae._natural_vent_active = False
    ae._fan_override_active = False
    coord.automation_engine = ae

    coord._rejection_log = {}
    coord._current_classification = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord._today_record = None
    coord._briefing_sent_today = False
    coord._last_briefing = ""
    coord._last_briefing_short = ""
    coord._briefing_day_type = None
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._occupancy_away_since = None
    coord._pred_archive = {}
    coord.claude_client = None
    coord._event_log = []
    coord._passive_k_backfilled = False
    coord._vent_k_backfilled = False
    coord._passive_k_backfill_v2 = False
    coord._vent_k_backfill_v2 = False
    coord._solar_phase_backfill = False
    coord._last_solar_phase_fit_date = None

    coord._async_save_state = AsyncMock()
    coord._set_occupancy_mode = MagicMock()
    coord._emit_event = MagicMock()

    learning_mock = MagicMock()
    learning_mock._state = MagicMock()
    learning_mock._state.rejection_log = {}
    coord.learning = learning_mock
    coord._state_persistence = MagicMock()

    coord.async_restore_state = types.MethodType(ClimateAdvisorCoordinator.async_restore_state, coord)
    return coord


def _run_restore_with_state_data(state_data: dict) -> object:
    """Run async_restore_state on a stub with given persisted state dict. Returns the coord.

    Patch dt_util.now directly (not the whole dt_util object) so strftime() and .date()
    work correctly — the same-day date comparison at line ~489 of coordinator.py requires
    a real datetime, not a MagicMock.  The state_data must also include "date" matching
    today_str so the coordinator does not early-return.
    """
    coord = _make_restore_coord_stub()
    # Ensure the persisted state looks like a same-day save so restore runs fully
    merged = {"date": _STABLE_DATE_STR, **state_data}
    with (
        patch(_PATCH_DT_NOW, return_value=_STABLE_NOW),
        patch.object(coord.hass, "async_add_executor_job", new_callable=AsyncMock) as mock_exec,
    ):
        mock_exec.side_effect = [
            None,  # learning.load_state()
            merged,  # _state_persistence.load()
        ]
        asyncio.run(coord.async_restore_state())
    return coord


class TestSolarPhasePeriodicPersistence:
    """_last_solar_phase_fit_date must survive HA restarts via state save/restore."""

    def test_last_solar_phase_fit_date_persisted_integration(self):
        """_build_state_dict() must include 'last_solar_phase_fit_date' = ISO string."""
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = _make_build_state_stub(mod, date(2026, 6, 13))

        with patch.object(mod, "dt_util") as mock_dt:
            _fake_now = MagicMock()
            _fake_now.strftime.return_value = "2026-06-13"
            _fake_now.isoformat.return_value = "2026-06-13T10:00:00+00:00"
            mock_dt.now.return_value = _fake_now
            result = mod.ClimateAdvisorCoordinator._build_state_dict(coord)

        assert "last_solar_phase_fit_date" in result, (
            f"Key missing from _build_state_dict(). Keys: {sorted(result.keys())}"
        )
        assert result["last_solar_phase_fit_date"] == "2026-06-13", (
            f"Expected '2026-06-13', got {result['last_solar_phase_fit_date']!r}"
        )

    def test_last_solar_phase_fit_date_persisted_none(self):
        """When _last_solar_phase_fit_date is None, _build_state_dict must store None."""
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = _make_build_state_stub(mod, None)

        with patch.object(mod, "dt_util") as mock_dt:
            _fake_now = MagicMock()
            _fake_now.strftime.return_value = "2026-06-13"
            _fake_now.isoformat.return_value = "2026-06-13T10:00:00+00:00"
            mock_dt.now.return_value = _fake_now
            result = mod.ClimateAdvisorCoordinator._build_state_dict(coord)

        assert result.get("last_solar_phase_fit_date") is None, (
            f"Expected None, got {result.get('last_solar_phase_fit_date')!r}"
        )

    def test_last_solar_phase_fit_date_restored(self):
        """async_restore_state must parse 'last_solar_phase_fit_date' into a date object."""
        coord = _run_restore_with_state_data({"last_solar_phase_fit_date": "2026-06-13"})
        assert coord._last_solar_phase_fit_date == date(2026, 6, 13), (
            f"Expected date(2026, 6, 13), got {coord._last_solar_phase_fit_date!r}"
        )

    def test_last_solar_phase_fit_date_restored_none(self):
        """When key is absent from state, async_restore_state must set _last_solar_phase_fit_date=None."""
        coord = _run_restore_with_state_data({})  # key absent
        assert coord._last_solar_phase_fit_date is None, (
            f"Expected None for absent key, got {coord._last_solar_phase_fit_date!r}"
        )
