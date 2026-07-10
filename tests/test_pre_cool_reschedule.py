"""Tests for the pre-cool early-reschedule mechanism (#437 follow-up,
architecture-reset session).

Occupant impact this closes: on a warming-trend night, pre-cool's AC trigger was
scheduled ONCE at classification time (window_close_time + 30min, or a wake-4h
fallback), and never revisited. If nat-vent exited early — the reactivation gate
firing, a sensor closing, outdoor rising, an away/vacation ceiling exit, or a
startup reconcile — the occupant's AC still waited for the STATIC, now-stale
schedule, wasting the gap between the real exit and the original trigger time.
This mechanism detects the real natural_vent_active True->False transition (via
_emit_event(), independent of which of the 6 real exit paths caused it) and pulls
the pending trigger earlier, never later.

Covers:
  - _decide_pre_cool_reschedule() pure function boundaries
  - _maybe_reschedule_pre_cool_on_nat_vent_exit() coordinator wiring
  - _emit_event()'s True->False transition detection (the real production trigger)
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

_NOW = datetime(2026, 7, 16, 23, 0, 0, tzinfo=UTC)


def _get_coordinator_class():
    """Return current ClimateAdvisorCoordinator class — avoids stale __globals__."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.coordinator import _decide_pre_cool_reschedule  # noqa: E402


def _make_classification(setback_modifier: float = -3.0) -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "warm",
            "trend_direction": "warming",
            "trend_magnitude": 3.0,
            "today_high": 88,
            "today_low": 65,
            "tomorrow_high": 96,
            "tomorrow_low": 70,
            "hvac_mode": "cool",
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": setback_modifier,
        }
    )
    return c


# ---------------------------------------------------------------------------
# Pure function: _decide_pre_cool_reschedule()
# ---------------------------------------------------------------------------


class TestDecidePreCoolReschedule:
    def test_pulls_trigger_earlier_when_candidate_precedes_scheduled_time(self):
        """Nat-vent exits at 23:00; scheduled trigger was 01:30 — pull it to 23:30."""
        result = _decide_pre_cool_reschedule(
            current_trigger_at=_NOW + timedelta(hours=2, minutes=30),
            setback_modifier=-3.0,
            nat_vent_close_delay_minutes=30,
            now=_NOW,
        )
        assert result == _NOW + timedelta(minutes=30)

    def test_none_when_no_trigger_pending(self):
        """Mirrors 'already fired today, or never scheduled' — current_trigger_at is None."""
        result = _decide_pre_cool_reschedule(
            current_trigger_at=None,
            setback_modifier=-3.0,
            nat_vent_close_delay_minutes=30,
            now=_NOW,
        )
        assert result is None

    def test_none_when_no_warming_trend(self):
        """setback_modifier >= 0 -> pre-cool wouldn't have been scheduled in the first place."""
        result = _decide_pre_cool_reschedule(
            current_trigger_at=_NOW + timedelta(hours=2),
            setback_modifier=0.0,
            nat_vent_close_delay_minutes=30,
            now=_NOW,
        )
        assert result is None

    def test_none_when_candidate_would_be_later_not_earlier(self):
        """Only ever pulls the trigger EARLIER — a nat-vent exit close to (or after) the
        already-scheduled time must not push pre-cool back."""
        result = _decide_pre_cool_reschedule(
            current_trigger_at=_NOW + timedelta(minutes=10),
            setback_modifier=-3.0,
            nat_vent_close_delay_minutes=30,
            now=_NOW,
        )
        assert result is None

    def test_none_at_exact_equality_boundary(self):
        """candidate == current_trigger_at -> no reschedule (non-strict >= guard)."""
        result = _decide_pre_cool_reschedule(
            current_trigger_at=_NOW + timedelta(minutes=30),
            setback_modifier=-3.0,
            nat_vent_close_delay_minutes=30,
            now=_NOW,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Coordinator wiring: _emit_event() transition detection + reschedule
# ---------------------------------------------------------------------------


def _make_coord_stub():
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    coord.config = {"comfort_heat": 68, "comfort_cool": 74}
    coord._event_log = []
    coord._current_classification = _make_classification(setback_modifier=-3.0)
    coord._pre_cool_trigger_dt = _NOW + timedelta(hours=2, minutes=30)
    coord._pre_cool_trigger_cancel = None
    coord._pre_cool_status = None
    coord._get_indoor_temp = MagicMock(return_value=74.0)
    coord._last_outdoor_temp = 65.0
    coord._nat_vent_was_active = False

    ae = MagicMock()
    ae._natural_vent_active = False
    coord.automation_engine = ae

    coord._emit_event = types.MethodType(ClimateAdvisorCoordinator._emit_event, coord)
    coord._maybe_reschedule_pre_cool_on_nat_vent_exit = types.MethodType(
        ClimateAdvisorCoordinator._maybe_reschedule_pre_cool_on_nat_vent_exit, coord
    )
    coord._async_pre_cool_trigger = MagicMock()
    return coord


class TestMaybeReschedulePreCoolOnNatVentExitLoadBearing:
    """Positive-control style: proves _emit_event()'s transition detection actually
    drives the reschedule, not just that the pure function is correct in isolation."""

    def test_true_to_false_transition_reschedules_the_real_timer(self):
        coord = _make_coord_stub()

        with (
            patch("custom_components.climate_advisor.coordinator.async_track_point_in_time") as mock_track,
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_NOW),
        ):
            mock_track.return_value = "cancel-handle"

            # First emit: nat-vent still active -> no transition, no reschedule.
            coord.automation_engine._natural_vent_active = True
            coord._emit_event("nat_vent_still_active", {})
            assert mock_track.call_count == 0

            # Second emit: nat-vent has exited -> True->False transition detected.
            coord.automation_engine._natural_vent_active = False
            coord._emit_event("nat_vent_comfort_floor_exit", {})

        assert mock_track.call_count == 1, "the real timer must be rescheduled on the exit transition"
        _, _, scheduled_at = mock_track.call_args[0]
        assert scheduled_at == _NOW + timedelta(minutes=30)
        assert coord._pre_cool_trigger_dt == _NOW + timedelta(minutes=30)
        assert coord._pre_cool_trigger_cancel == "cancel-handle"
        assert "rescheduled" in coord._pre_cool_status

    def test_no_reschedule_when_nat_vent_never_transitions(self):
        """Nat-vent inactive on both emits (no session ran at all) -> no false-positive reschedule."""
        coord = _make_coord_stub()

        with patch("custom_components.climate_advisor.coordinator.async_track_point_in_time") as mock_track:
            coord.automation_engine._natural_vent_active = False
            coord._emit_event("classification_applied", {})
            coord._emit_event("bedtime_setback", {})

        assert mock_track.call_count == 0

    def test_no_reschedule_when_no_warming_trend(self):
        """Nat-vent exits, but today has no warming trend -> pre-cool was never pending."""
        coord = _make_coord_stub()
        coord._current_classification = _make_classification(setback_modifier=0.0)

        with (
            patch("custom_components.climate_advisor.coordinator.async_track_point_in_time") as mock_track,
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_NOW),
        ):
            coord.automation_engine._natural_vent_active = True
            coord._emit_event("nat_vent_still_active", {})
            coord.automation_engine._natural_vent_active = False
            coord._emit_event("nat_vent_comfort_floor_exit", {})

        assert mock_track.call_count == 0

    def test_forcing_decide_function_to_return_none_suppresses_the_reschedule(self):
        """Load-bearing: patch _decide_pre_cool_reschedule (the name coordinator.py
        imports/calls at module scope) to always return None; confirm the real
        transition-triggered call site genuinely dispatches on its answer."""
        coord = _make_coord_stub()

        with (
            patch("custom_components.climate_advisor.coordinator.async_track_point_in_time") as mock_track,
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_NOW),
            patch("custom_components.climate_advisor.coordinator._decide_pre_cool_reschedule", return_value=None),
        ):
            coord.automation_engine._natural_vent_active = True
            coord._emit_event("nat_vent_still_active", {})
            coord.automation_engine._natural_vent_active = False
            coord._emit_event("nat_vent_comfort_floor_exit", {})

        assert mock_track.call_count == 0, "forcing None must suppress the reschedule — load-bearing confirmed"
