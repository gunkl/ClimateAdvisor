"""Tests for Issue #480: coordinator health observability.

Covers:
- The real occupant-facing problem: when the coordinator update fails, entities
  correctly go unavailable (unchanged HA behavior), but the dashboard used to
  keep showing the frozen pre-failure snapshot with zero indication anything
  was wrong (Issue #478 Finding C). The fix adds a durable side-channel record
  of the failure (coordinator.py) and surfaces it in the status API/dashboard
  (api.py, frontend/index.html).
- `_async_update_data()` (now a thin wrapper around `_async_update_data_impl()`)
  records `last_update_error`/`last_update_error_time`/`consecutive_failure_count`
  on failure, persists them, and still re-raises so HA's own
  `DataUpdateCoordinator.last_update_success`/unavailable handling is unchanged.
- The failure record is cleared and re-persisted on the next successful update.
- `_build_state_dict()`/`async_restore_state()` persist and restore the three
  fields durably — including across a day boundary, so a failure recorded just
  before an overnight restart is still visible afterward (same precedent as
  `ai_stats`, which restores unconditionally for the same reason).
- `ClimateAdvisorStatusView.get()` (api.py) gates on `coordinator.last_update_success`
  and adds `coordinator_healthy`/`last_error`/`stale_since` to the response,
  additive only.

No `tools/simulations/pending/` scenario was written for this issue: the sim
harness's fake coordinator (`tools/sim_harness/ha_stubs.py::_MockDataUpdateCoordinator
.async_config_entry_first_refresh()`) calls `_async_update_data()` with no
try/except and unconditionally sets `last_update_success = True` afterward —
there is no failure-injection mechanism to force `_async_update_data_impl()` to
raise within a scenario. This is confirmed by inspection (no
`inject_failure`/`force_failure`/`simulate_failure`/`raise_on_update`/
`fail_next_update` hook anywhere under `tools/sim_harness/`). Coverage for this
issue is a plain pytest unit test instead, per CLAUDE.md's guidance not to force
a simulation scenario onto a harness capability that doesn't exist.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.learning import LearningState  # noqa: E402

_FIXED_NOW = datetime(2026, 7, 11, 6, 35, 0, tzinfo=UTC)


def _make_coord_stub():
    """Build a minimal coordinator-like object with the real health-tracking
    methods bound, following the established object.__new__()/types.MethodType()
    partial-instantiation pattern (see test_restart_cause.py::_make_coord_stub,
    test_contact_status.py::_make_real_coordinator).
    """
    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

    coord.last_update_error = None
    coord.last_update_error_time = None
    coord.consecutive_failure_count = 0

    coord._async_save_state = AsyncMock()
    coord._async_update_data = types.MethodType(ClimateAdvisorCoordinator._async_update_data, coord)

    return coord


def _make_restore_coord_stub(persisted_state: dict):
    """Minimal coordinator stub with async_restore_state() bound, matching
    test_restart_cause.py's pattern — only the fields async_restore_state()
    actually reads are set.
    """
    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

    coord.config = {"climate_entity": "climate.thermostat"}
    coord.learning = MagicMock()
    coord.learning._state = LearningState()
    coord.learning.load_state = MagicMock()
    coord.claude_client = None

    coord._rejection_log = {}
    coord._state_persistence = MagicMock()
    coord._state_persistence.load = MagicMock(return_value=persisted_state)
    coord._event_log = []
    coord._emit_event = MagicMock()

    coord.last_update_error = None
    coord.last_update_error_time = None
    coord.consecutive_failure_count = 0

    coord.async_restore_state = types.MethodType(ClimateAdvisorCoordinator.async_restore_state, coord)
    return coord


# ---------------------------------------------------------------------------
# _async_update_data() wrapper — failure capture
# ---------------------------------------------------------------------------


class TestUpdateWrapperFailureCapture:
    def test_failure_records_error_and_reraises(self):
        """Occupant impact: the entity must still go unavailable (HA's own
        DataUpdateCoordinator catches this re-raised exception) — but now the
        cause is durably recorded instead of only living in a log line that
        rotates out within days.
        """
        coord = _make_coord_stub()
        coord._async_update_data_impl = AsyncMock(side_effect=RuntimeError("weather entity gone"))

        with (
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now",
                return_value=_FIXED_NOW,
            ),
            pytest.raises(RuntimeError, match="weather entity gone"),
        ):
            asyncio.run(coord._async_update_data())

        assert coord.consecutive_failure_count == 1
        assert coord.last_update_error == "RuntimeError: weather entity gone"
        assert coord.last_update_error_time == _FIXED_NOW.isoformat()
        coord._async_save_state.assert_called_once()

    def test_repeated_failures_increment_count(self):
        coord = _make_coord_stub()
        coord._async_update_data_impl = AsyncMock(side_effect=RuntimeError("boom"))

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=_FIXED_NOW,
        ):
            for _ in range(3):
                with pytest.raises(RuntimeError):
                    asyncio.run(coord._async_update_data())

        assert coord.consecutive_failure_count == 3
        assert coord._async_save_state.call_count == 3

    def test_success_does_not_persist_error_fields(self):
        """No failure ever occurred — the wrapper must not spuriously save state
        or set an error (that would be worse than the bug being fixed)."""
        coord = _make_coord_stub()
        coord._async_update_data_impl = AsyncMock(return_value={"ok": True})

        result = asyncio.run(coord._async_update_data())

        assert result == {"ok": True}
        assert coord.consecutive_failure_count == 0
        assert coord.last_update_error is None
        coord._async_save_state.assert_not_called()


# ---------------------------------------------------------------------------
# _async_update_data() wrapper — recovery clears the durable record
# ---------------------------------------------------------------------------


class TestUpdateWrapperRecovery:
    def test_success_after_failure_clears_and_persists(self):
        coord = _make_coord_stub()
        coord.consecutive_failure_count = 2
        coord.last_update_error = "RuntimeError: boom"
        coord.last_update_error_time = _FIXED_NOW.isoformat()
        coord._async_update_data_impl = AsyncMock(return_value={"ok": True})

        result = asyncio.run(coord._async_update_data())

        assert result == {"ok": True}
        assert coord.consecutive_failure_count == 0
        assert coord.last_update_error is None
        assert coord.last_update_error_time is None
        coord._async_save_state.assert_called_once()


# ---------------------------------------------------------------------------
# _build_state_dict() — durability
# ---------------------------------------------------------------------------


class TestBuildStateDictIncludesHealthFields:
    def test_health_fields_present_in_serialized_state(self):
        coord = _make_coord_stub()
        coord.last_update_error = "RuntimeError: boom"
        coord.last_update_error_time = "2026-07-11T06:35:00+00:00"
        coord.consecutive_failure_count = 2

        # Minimal attributes _build_state_dict() reads besides the health fields.
        coord._current_classification = None
        coord._today_record = None
        coord._outdoor_temp_history = []
        coord._indoor_temp_history = []
        coord.automation_engine = MagicMock()
        coord.automation_engine.get_serializable_state.return_value = {}
        coord._briefing_sent_today = False
        coord._last_briefing = ""
        coord._last_briefing_short = ""
        coord._briefing_day_type = None
        coord._automation_enabled = True
        coord._occupancy_mode = "home"
        coord._occupancy_away_since = None
        coord.claude_client = None
        coord._pred_archive = {}
        coord._passive_k_backfilled = False
        coord._vent_k_backfilled = False
        coord._passive_k_backfill_v2 = False
        coord._vent_k_backfill_v2 = False
        coord._solar_phase_backfill = False
        coord._solar_phase_ac_backfill = False
        coord._last_solar_phase_fit_date = None
        coord._event_log = []

        coord._build_state_dict = types.MethodType(ClimateAdvisorCoordinator._build_state_dict, coord)
        state_dict = coord._build_state_dict()

        assert state_dict["last_update_error"] == "RuntimeError: boom"
        assert state_dict["last_update_error_time"] == "2026-07-11T06:35:00+00:00"
        assert state_dict["consecutive_failure_count"] == 2


# ---------------------------------------------------------------------------
# async_restore_state() — restores regardless of date boundary
# ---------------------------------------------------------------------------


class TestRestoreStateRestoresHealthFields:
    def test_restores_same_day(self):
        today_str = _FIXED_NOW.strftime("%Y-%m-%d")
        coord = _make_restore_coord_stub(
            {
                "date": today_str,
                "last_saved": today_str,
                "classification": None,
                "temp_history": {"outdoor": [], "indoor": []},
                "today_record": None,
                "briefing_state": {},
                "automation_state": {},
                "automation_enabled": True,
                "occupancy_mode": "home",
                "occupancy_away_since": None,
                "ai_stats": {},
                "pred_archive": {},
                "event_log": [],
                "last_update_error": "RuntimeError: weather entity gone",
                "last_update_error_time": "2026-07-11T06:35:00+00:00",
                "consecutive_failure_count": 4,
            }
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            asyncio.run(coord.async_restore_state())

        assert coord.last_update_error == "RuntimeError: weather entity gone"
        assert coord.last_update_error_time == "2026-07-11T06:35:00+00:00"
        assert coord.consecutive_failure_count == 4

    def test_restores_even_when_state_is_from_a_prior_day(self):
        """Occupant impact: an overnight coordinator failure followed by an HA
        restart the next morning must still surface the prior failure — the
        rest of async_restore_state() intentionally skips same-day-only fields
        (classification, automation_state, etc.) once the date has rolled over,
        but the health record must not be silently dropped along with them
        (same reasoning as the existing ai_stats restore, which is also
        unconditional).
        """
        yesterday_str = "2026-07-10"
        coord = _make_restore_coord_stub(
            {
                "date": yesterday_str,
                "last_saved": yesterday_str,
                "last_update_error": "RuntimeError: weather entity gone",
                "last_update_error_time": "2026-07-10T23:59:00+00:00",
                "consecutive_failure_count": 1,
            }
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            asyncio.run(coord.async_restore_state())

        assert coord.last_update_error == "RuntimeError: weather entity gone"
        assert coord.consecutive_failure_count == 1

    def test_missing_fields_default_safely(self):
        """Old persisted state files (pre-Issue #480) won't have these keys."""
        today_str = _FIXED_NOW.strftime("%Y-%m-%d")
        coord = _make_restore_coord_stub(
            {
                "date": today_str,
                "last_saved": today_str,
                "classification": None,
                "temp_history": {"outdoor": [], "indoor": []},
                "today_record": None,
                "briefing_state": {},
                "automation_state": {},
                "automation_enabled": True,
                "occupancy_mode": "home",
                "occupancy_away_since": None,
                "ai_stats": {},
                "pred_archive": {},
                "event_log": [],
            }
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            asyncio.run(coord.async_restore_state())

        assert coord.last_update_error is None
        assert coord.last_update_error_time is None
        assert coord.consecutive_failure_count == 0

    def test_corrupt_failure_count_defaults_to_zero(self):
        today_str = _FIXED_NOW.strftime("%Y-%m-%d")
        coord = _make_restore_coord_stub(
            {
                "date": today_str,
                "last_saved": today_str,
                "classification": None,
                "temp_history": {"outdoor": [], "indoor": []},
                "today_record": None,
                "briefing_state": {},
                "automation_state": {},
                "automation_enabled": True,
                "occupancy_mode": "home",
                "occupancy_away_since": None,
                "ai_stats": {},
                "pred_archive": {},
                "event_log": [],
                "consecutive_failure_count": "not-a-number",
            }
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            asyncio.run(coord.async_restore_state())

        assert coord.consecutive_failure_count == 0


# ---------------------------------------------------------------------------
# api.py — ClimateAdvisorStatusView gating (Issue #480 / precedent: Issue #466)
# ---------------------------------------------------------------------------


def _make_view_request(coordinator, climate_state=None):
    hass = MagicMock()
    from custom_components.climate_advisor.const import DOMAIN

    hass.data = {DOMAIN: {"entry1": coordinator}}
    hass.states.get.return_value = climate_state
    req = MagicMock()
    req.app = {"hass": hass}
    return req


def _simulate_status_get(coordinator, climate_state=None):
    from custom_components.climate_advisor.api import ClimateAdvisorStatusView

    view = ClimateAdvisorStatusView()
    request = _make_view_request(coordinator, climate_state)
    resp = asyncio.run(view.get(request))
    return resp.json_data


def _make_status_coord(healthy: bool, last_error=None, stale_since=None):
    from custom_components.climate_advisor.classifier import DayClassification

    coord = MagicMock()
    coord.config = {"climate_entity": "climate.thermostat", "temp_unit": "fahrenheit"}
    coord.data = {}
    coord._get_indoor_temp.return_value = 70.0
    coord._last_outdoor_temp = 65.0
    coord.automation_enabled = True
    coord._occupancy_mode = "home"
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "mild",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 78,
            "today_low": 58,
            "tomorrow_high": 79,
            "tomorrow_low": 59,
            "hvac_mode": "heat",
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
    )
    coord.current_classification = c
    ae = MagicMock()
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._fan_override_active = False
    ae._pre_condition_achieved = False
    ae._natural_vent_active = False
    ae.is_paused_by_door = False
    coord.automation_engine = ae
    coord._compute_contact_details.return_value = []
    coord.compute_nat_vent_cycling_band.return_value = {
        "nat_vent_target": None,
        "nat_vent_on_threshold": None,
        "nat_vent_off_threshold": None,
    }

    coord.last_update_success = healthy
    coord.last_update_error = last_error
    coord.last_update_error_time = stale_since
    return coord


class TestStatusViewCoordinatorHealth:
    def test_healthy_coordinator_reports_healthy_true_no_error_fields(self):
        coord = _make_status_coord(healthy=True)
        payload = _simulate_status_get(coord)

        assert payload["coordinator_healthy"] is True
        assert "last_error" not in payload
        assert "stale_since" not in payload

    def test_unhealthy_coordinator_reports_healthy_false_with_error_fields(self):
        coord = _make_status_coord(
            healthy=False,
            last_error="RuntimeError: weather entity gone",
            stale_since="2026-07-11T06:35:00+00:00",
        )
        payload = _simulate_status_get(coord)

        assert payload["coordinator_healthy"] is False
        assert payload["last_error"] == "RuntimeError: weather entity gone"
        assert payload["stale_since"] == "2026-07-11T06:35:00+00:00"

    def test_unhealthy_response_still_includes_all_prior_fields(self):
        """Additive-only requirement: existing fields (e.g. fan_status,
        automation_status) must still be present and unmodified even when the
        coordinator is unhealthy — this endpoint keeps serving the last-known
        snapshot from coordinator.data, it just now also flags that it's stale.
        """
        coord = _make_status_coord(healthy=False, last_error="boom", stale_since="2026-07-11T06:35:00+00:00")
        payload = _simulate_status_get(coord)

        for key in ("fan_status", "automation_status", "day_type", "hvac_mode", "version"):
            assert key in payload
