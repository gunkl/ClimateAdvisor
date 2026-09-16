"""Coordinator-level tests for the away/vacation TOU T_start guard timer and live
expansion-state push (Issue #899).

Partial-instantiation pattern (``object.__new__()`` + bound methods) established by
``test_tou_live_instance_visibility.py`` — real ``_resolve_tou_schedule_state()`` /
``_sync_tou_away_vacation_state()`` logic, minimal stub state around it.
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402


def _make_classification(**overrides) -> DayClassification:
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "hot",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 95,
        "today_low": 70,
        "tomorrow_high": 95,
        "tomorrow_low": 70,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _build_coordinator(occupancy_mode: str) -> ClimateAdvisorCoordinator:
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    coord.config = {
        "schedules": [
            {
                "id": "s1",
                "name": "Afternoon high-cost",
                "days": ("all",),
                "start": "16:00",
                "end": "20:00",
                "cost_tag": "high",
            }
        ],
        "comfort_heat": 68.0,
        "comfort_cool": 76.0,
        "setback_heat": 63.0,
        "setback_cool": 79.0,
    }
    coord._current_classification = _make_classification()
    coord._event_log = []
    coord._tou_phase_resolution = None
    coord._tou_active_cost_resolution = None
    coord._tou_active_window_notified = False
    coord._occupancy_mode = occupancy_mode
    coord._get_indoor_temp = MagicMock(return_value=76.0)
    coord._last_outdoor_temp = 95.0
    coord._hvac_on_since = None
    coord._today_record = None
    coord._tou_av_stop_timer_cancel = None
    coord._tou_av_stop_timer_schedule_start = None
    coord._tou_av_window_was_active = False

    ae = MagicMock()
    ae._thermal_model = {}
    coord.automation_engine = ae

    for name in (
        "_resolve_tou_schedule_state",
        "_sync_tou_away_vacation_state",
        "_cancel_tou_av_stop_timer",
        "_fire_tou_av_window_start",
        "_maybe_emit_tou_active_window_event",
        "_flush_hvac_runtime",
        "_emit_event",
    ):
        import types

        setattr(coord, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), coord))
    return coord


class TestTouAwayVacationTimer:
    def test_preconditioning_schedules_precise_stop_timer(self) -> None:
        """Away mode, PRECONDITIONING resolution -> a timer is armed for exactly
        (schedule_start - now) seconds, not left to the next 30-min cycle."""
        coord = _build_coordinator("away")
        now = datetime(2026, 1, 5, 15, 30)  # 30 min before the 16:00 schedule start

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
            patch("custom_components.climate_advisor.coordinator.async_call_later") as mock_call_later,
        ):
            mock_call_later.return_value = MagicMock()
            coord._resolve_tou_schedule_state()

        assert mock_call_later.call_count == 1
        _, delay, _callback = mock_call_later.call_args[0]
        assert delay == 1800.0  # 30 minutes

    def test_no_timer_for_home_occupancy(self) -> None:
        coord = _build_coordinator("home")
        now = datetime(2026, 1, 5, 15, 30)

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
            patch("custom_components.climate_advisor.coordinator.async_call_later") as mock_call_later,
        ):
            coord._resolve_tou_schedule_state()

        mock_call_later.assert_not_called()

    def test_repeated_resolution_does_not_reschedule_same_instant(self) -> None:
        """Dedup: two cycles resolving to the SAME schedule_start must only arm one
        timer, not a fresh one every cycle."""
        coord = _build_coordinator("away")
        now = datetime(2026, 1, 5, 15, 30)

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
            patch("custom_components.climate_advisor.coordinator.async_call_later") as mock_call_later,
        ):
            mock_call_later.return_value = MagicMock()
            coord._resolve_tou_schedule_state()
            coord._resolve_tou_schedule_state()

        assert mock_call_later.call_count == 1

    def test_occupancy_leaving_away_cancels_pending_timer(self) -> None:
        coord = _build_coordinator("away")
        now = datetime(2026, 1, 5, 15, 30)
        cancel_fn = MagicMock()

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
            patch("custom_components.climate_advisor.coordinator.async_call_later", return_value=cancel_fn),
        ):
            coord._resolve_tou_schedule_state()
            assert coord._tou_av_stop_timer_cancel is cancel_fn

            coord._occupancy_mode = "home"
            coord._resolve_tou_schedule_state()

        cancel_fn.assert_called_once()
        assert coord._tou_av_stop_timer_cancel is None

    def test_window_active_pushes_expansion_onto_engine(self) -> None:
        """While `now` sits inside the resolved window itself, the engine's live
        expansion-state attributes are populated (Requirement: any call to
        handle_occupancy_away()/vacation() picks up the current window's expansion)."""
        coord = _build_coordinator("away")
        now = datetime(2026, 1, 5, 17, 0)  # inside the 16:00-20:00 window

        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
            coord._resolve_tou_schedule_state()

        assert coord.automation_engine._tou_av_expansion_f > 0.0
        assert coord.automation_engine._tou_av_active_schedule_id == "s1"
        assert coord.automation_engine._tou_av_precondition_target is not None

    def test_window_inactive_zeroes_engine_expansion(self) -> None:
        coord = _build_coordinator("away")
        now = datetime(2026, 1, 5, 21, 0)  # after the 16:00-20:00 window

        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
            coord._resolve_tou_schedule_state()

        assert coord.automation_engine._tou_av_expansion_f == 0.0
        assert coord.automation_engine._tou_av_active_schedule_id is None
