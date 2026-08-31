"""Phase 3d / Investigation D — direct reproduction of the live-instance finding.

David configured a real TOU schedule (days=["mon"], 09:40:00-10:00:00, cost_tag="high")
and saw nothing in the Activity Record or anywhere else. Investigation confirmed
resolve_tou_phase() was CORRECT (no thermal direction to bank toward on an
hvac_mode="off" day) but nothing anywhere told the occupant that. This test reproduces
the exact scenario end-to-end — a real schedules config, a real classification with
hvac_mode="off", covering `now` — through the actual production methods
(_resolve_tou_schedule_state(), _maybe_emit_tou_active_window_event(),
_compute_automation_status()), not a synthetic unit case that doesn't match what
actually happened.
"""

from __future__ import annotations

import sys
import types
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
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 78,
        "today_low": 60,
        "tomorrow_high": 78,
        "tomorrow_low": 60,
        "hvac_mode": "off",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": True,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _build_coordinator(hvac_mode: str) -> ClimateAdvisorCoordinator:
    """Build a coordinator with just enough real state/bound methods to exercise the
    real _resolve_tou_schedule_state() -> _maybe_emit_tou_active_window_event() ->
    _emit_event() chain, plus _compute_automation_status()."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {
        "schedules": [
            {
                "id": "s1",
                "name": "Monday high-cost",
                "days": ("mon",),
                "start": "09:40:00",
                "end": "10:00:00",
                "cost_tag": "high",
            }
        ],
        "comfort_heat": 68.0,
        "comfort_cool": 76.0,
    }
    coord._current_classification = _make_classification(hvac_mode=hvac_mode)
    coord._event_log = []
    coord._tou_phase_resolution = None
    coord._tou_active_cost_resolution = None
    coord._tou_active_window_notified = False
    coord._automation_enabled = True
    coord._startup_coalesce_active = False
    coord._occupancy_mode = "home"
    coord._get_indoor_temp = MagicMock(return_value=70.0)
    coord._last_outdoor_temp = 65.0
    ae = MagicMock()
    ae.is_paused_by_door = False
    ae.natural_vent_active = False
    ae._grace_active = False
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._grace_end_time = None
    ae._thermal_model = {}
    ae._is_within_planned_window_period = MagicMock(return_value=False)
    coord.automation_engine = ae
    coord._any_sensor_open = MagicMock(return_value=False)

    for name in (
        "_resolve_tou_schedule_state",
        "_maybe_emit_tou_active_window_event",
        "_emit_event",
        "_compute_automation_status",
    ):
        setattr(coord, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), coord))
    return coord


class TestLiveInstanceReproduction:
    def test_hvac_off_day_produces_status_text_and_event(self) -> None:
        """The exact scenario David hit: a real schedule covering `now`, hvac_mode='off'.
        Must now produce BOTH a non-empty, informative Status-card string AND a
        tou_schedule_window_active event — the direct empirical proof this is fixed."""
        coord = _build_coordinator(hvac_mode="off")
        # Monday 09:50 -- inside the configured 09:40-10:00 window.
        now = datetime(2026, 8, 31, 9, 50)  # confirmed Monday
        assert now.weekday() == 0

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=now,
        ):
            coord._resolve_tou_schedule_state()
            status = coord._compute_automation_status()

        # Status card: non-empty, and specifically distinguishes "evaluated and found
        # inapplicable" from silence.
        assert status
        assert "TOU high-cost period active" in status
        assert "no pre-conditioning needed today" in status

        # Activity Record: exactly one tou_schedule_window_active event, not silence.
        tou_events = [e for e in coord._event_log if e.get("type") == "tou_schedule_window_active"]
        assert len(tou_events) == 1
        assert tou_events[0]["preconditioned"] is False
        assert tou_events[0]["hvac_mode"] == "off"
        assert tou_events[0]["active_schedule_ids"] == ["s1"]

    def test_hvac_heat_day_marks_preconditioned_true(self) -> None:
        """Control case: same window, but hvac_mode allows heat -> event/status reflect
        that the window coincided with real HVAC operation."""
        coord = _build_coordinator(hvac_mode="heat")
        now = datetime(2026, 8, 31, 9, 50)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=now,
        ):
            coord._resolve_tou_schedule_state()
            status = coord._compute_automation_status()

        assert "TOU high-cost period active" in status
        assert "no pre-conditioning needed today" not in status
        tou_events = [e for e in coord._event_log if e.get("type") == "tou_schedule_window_active"]
        assert len(tou_events) == 1
        assert tou_events[0]["preconditioned"] is True

    def test_event_does_not_re_fire_every_cycle_while_window_stays_active(self) -> None:
        """Dedup guard: calling _resolve_tou_schedule_state() twice while still inside
        the same active window must emit only ONE event, not one per cycle."""
        coord = _build_coordinator(hvac_mode="off")
        now = datetime(2026, 8, 31, 9, 50)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=now,
        ):
            coord._resolve_tou_schedule_state()
            coord._resolve_tou_schedule_state()

        tou_events = [e for e in coord._event_log if e.get("type") == "tou_schedule_window_active"]
        assert len(tou_events) == 1

    def test_event_fires_again_after_window_closes_and_reopens(self) -> None:
        """After the window closes (guard resets) and a later cycle re-enters an active
        high-cost window, the event fires again — a fresh transition, not permanently
        suppressed."""
        coord = _build_coordinator(hvac_mode="off")
        inside = datetime(2026, 8, 31, 9, 50)
        outside = datetime(2026, 8, 31, 10, 30)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=inside,
        ):
            coord._resolve_tou_schedule_state()
        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=outside,
        ):
            coord._resolve_tou_schedule_state()
        assert coord._tou_active_window_notified is False
        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=inside,
        ):
            coord._resolve_tou_schedule_state()

        tou_events = [e for e in coord._event_log if e.get("type") == "tou_schedule_window_active"]
        assert len(tou_events) == 2

    def test_no_covering_schedule_emits_nothing(self) -> None:
        """Outside any configured window -> no event, guard stays False."""
        coord = _build_coordinator(hvac_mode="off")
        outside = datetime(2026, 8, 31, 11, 0)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=outside,
        ):
            coord._resolve_tou_schedule_state()

        tou_events = [e for e in coord._event_log if e.get("type") == "tou_schedule_window_active"]
        assert tou_events == []
        assert coord._tou_active_window_notified is False
