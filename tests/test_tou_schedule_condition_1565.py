"""Issue #1565: optional "only when this entity is on" condition for TOU schedules.

Occupant first: a household on a seasonal rate (for example Georgia Power's Overnight Advantage, on peak 2 to 7 PM
on summer weekdays only) configures one Mon to Fri 14:00 to 19:00 schedule with a peak-season sensor as its
condition. Out of season the schedule must not precool or preheat, and the Status card and Activity Record must
say why nothing happened. In season, and whenever the condition entity is broken, behavior is exactly as before.

Coordinator cases drive the REAL production chain (_resolve_tou_schedule_state() ->
_maybe_emit_tou_active_window_event() -> _emit_event(), and _compute_automation_status()), following
tests/test_tou_live_instance_visibility.py.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.dashboard_api import build_settings_extra  # noqa: E402
from custom_components.climate_advisor.scheduler import Schedule, apply_schedule_conditions  # noqa: E402

COND = "binary_sensor.peak_season"
# Monday 15:00, inside a Mon 14:00-19:00 window.
MONDAY_3PM = datetime(2026, 8, 31, 15, 0)


def _sched(sid: str, condition: str | None = None) -> Schedule:
    return Schedule(
        id=sid,
        name=sid,
        days=("mon",),
        start="14:00:00",
        end="19:00:00",
        cost_tag="high",
        condition_entity=condition,
    )


# ---------------------------------------------------------------------------
# apply_schedule_conditions() -- the pure rule
# ---------------------------------------------------------------------------


class TestApplyScheduleConditions:
    def test_schedule_without_condition_always_kept(self) -> None:
        result = apply_schedule_conditions([_sched("a")], lambda _eid: "off")
        assert [s.id for s in result.kept] == ["a"]
        assert result.skipped == ()
        assert result.failed_open_ids == ()

    def test_condition_on_keeps_schedule(self) -> None:
        result = apply_schedule_conditions([_sched("a", COND)], {COND: "on"}.get)
        assert [s.id for s in result.kept] == ["a"]
        assert result.skipped == ()

    def test_condition_off_skips_schedule(self) -> None:
        result = apply_schedule_conditions([_sched("a", COND)], {COND: "off"}.get)
        assert result.kept == ()
        assert [s.id for s in result.skipped] == ["a"]
        assert result.failed_open_ids == ()

    def test_unexpected_state_counts_as_not_on(self) -> None:
        """Only "on" applies; any other definite state (here a switch reporting something odd) skips."""
        result = apply_schedule_conditions([_sched("a", COND)], {COND: "idle"}.get)
        assert [s.id for s in result.skipped] == ["a"]

    def test_missing_unknown_unavailable_fail_open(self) -> None:
        """A broken rate-calendar helper must not silently disable pre-conditioning: keep, and report it."""
        states = {"binary_sensor.u1": "unknown", "binary_sensor.u2": "unavailable"}
        schedules = [
            _sched("missing", "binary_sensor.gone"),
            _sched("u1", "binary_sensor.u1"),
            _sched("u2", "binary_sensor.u2"),
        ]
        result = apply_schedule_conditions(schedules, states.get)
        assert [s.id for s in result.kept] == ["missing", "u1", "u2"]
        assert result.failed_open_ids == ("missing", "u1", "u2")
        assert result.skipped == ()

    def test_order_preserved_within_kept_and_skipped(self) -> None:
        states = {"binary_sensor.x": "off", "binary_sensor.y": "on"}
        schedules = [
            _sched("a"),
            _sched("b", "binary_sensor.x"),
            _sched("c", "binary_sensor.y"),
            _sched("d", "binary_sensor.x"),
        ]
        result = apply_schedule_conditions(schedules, states.get)
        assert [s.id for s in result.kept] == ["a", "c"]
        assert [s.id for s in result.skipped] == ["b", "d"]

    def test_stored_schedule_without_key_loads(self) -> None:
        """Schedules saved before #1565 have no condition_entity key; Schedule(**stored) must still load them."""
        stored = {"id": "a", "name": "A", "days": ["mon"], "start": "14:00:00", "end": "19:00:00", "cost_tag": "high"}
        assert Schedule(**stored).condition_entity is None


# ---------------------------------------------------------------------------
# Coordinator: real _resolve_tou_schedule_state() / status / Activity Record chain
# ---------------------------------------------------------------------------


def _make_classification(hvac_mode: str) -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "cool",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 55,
            "today_low": 40,
            "tomorrow_high": 55,
            "tomorrow_low": 40,
            "hvac_mode": hvac_mode,
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
    return c


def _build_coordinator(condition_state: str | None, occupancy: str = "home") -> ClimateAdvisorCoordinator:
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {
        "schedules": [
            {
                "id": "s1",
                "name": "Summer peak",
                "days": ("mon",),
                "start": "14:00:00",
                "end": "19:00:00",
                "cost_tag": "high",
                "condition_entity": COND,
            }
        ],
        "comfort_heat": 68.0,
        "comfort_cool": 76.0,
    }
    hass = MagicMock()
    if condition_state is None:
        hass.states.get.return_value = None
    else:
        state = MagicMock()
        state.state = condition_state
        hass.states.get.side_effect = lambda eid: state if eid == COND else None
    coord.hass = hass
    coord._current_classification = _make_classification("heat")
    coord._event_log = []
    coord._tou_phase_resolution = None
    coord._tou_active_cost_resolution = None
    coord._tou_active_window_notified = False
    coord._tou_condition_skipped_resolution = None
    coord._tou_condition_entities = {}
    coord._tou_condition_logged = ((), ())
    coord._automation_enabled = True
    coord._startup_coalesce_active = False
    coord._occupancy_mode = occupancy
    coord._get_indoor_temp = MagicMock(return_value=67.0)
    coord._last_outdoor_temp = 45.0
    ae = MagicMock()
    ae.is_paused_by_door = False
    ae.natural_vent_active = False
    ae._grace_active = False
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._grace_end_time = None
    ae._is_within_planned_window_period = MagicMock(return_value=False)
    coord.automation_engine = ae
    coord._any_sensor_open = MagicMock(return_value=False)
    for name in (
        "_resolve_tou_schedule_state",
        "_maybe_emit_tou_active_window_event",
        "_emit_event",
        "_compute_automation_status",
        "_tou_condition_state",
        "_log_tou_condition_changes",
    ):
        setattr(coord, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), coord))
    return coord


def _run(coord: ClimateAdvisorCoordinator) -> str:
    with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=MONDAY_3PM):
        coord._resolve_tou_schedule_state()
        return coord._compute_automation_status()


def _tou_events(coord: ClimateAdvisorCoordinator) -> list[dict]:
    return [e for e in coord._event_log if e.get("type") == "tou_schedule_window_active"]


class TestCoordinatorConditionOff:
    def test_off_condition_never_preconditions(self) -> None:
        coord = _build_coordinator("off")
        _run(coord)
        assert coord._tou_phase_resolution is None
        assert coord._tou_active_cost_resolution is None

    def test_off_condition_explained_on_status_card(self) -> None:
        status = _run(_build_coordinator("off"))
        assert "TOU high-cost period skipped" in status
        assert "condition is off" in status
        assert "pre-heating" not in status and "pre-cooling" not in status

    def test_skipped_text_never_hides_away(self) -> None:
        """Lowest priority: an away household still reads "active (away)"."""
        status = _run(_build_coordinator("off", occupancy="away"))
        assert status == "active (away)"

    def test_off_condition_records_one_skipped_event(self) -> None:
        coord = _build_coordinator("off")
        _run(coord)
        _run(coord)  # a second cycle inside the same window must not repeat the event
        events = _tou_events(coord)
        assert len(events) == 1
        assert events[0]["skipped_by_condition"] is True
        assert events[0]["preconditioned"] is False
        assert events[0]["condition_entities"] == [COND]
        assert events[0]["active_schedule_ids"] == ["s1"]


class TestCoordinatorConditionOnOrBroken:
    def test_on_condition_behaves_like_no_condition(self) -> None:
        coord = _build_coordinator("on")
        status = _run(coord)
        assert coord._tou_active_cost_resolution is not None
        assert coord._tou_condition_skipped_resolution is None
        assert status.startswith("TOU high-cost period active")
        events = _tou_events(coord)
        assert len(events) == 1
        assert "skipped_by_condition" not in events[0]
        assert events[0]["preconditioned"] is True

    def test_missing_condition_entity_fails_open(self) -> None:
        coord = _build_coordinator(None)
        status = _run(coord)
        assert coord._tou_active_cost_resolution is not None
        assert status.startswith("TOU high-cost period active")

    def test_unavailable_condition_entity_fails_open(self) -> None:
        coord = _build_coordinator("unavailable")
        _run(coord)
        assert coord._tou_active_cost_resolution is not None


# ---------------------------------------------------------------------------
# Activity Record renderer and dashboard settings payload
# ---------------------------------------------------------------------------


class TestSurfaces:
    def test_renderer_explains_skipped_window(self) -> None:
        label, settings = EVENT_RENDERERS["tou_schedule_window_active"](
            {
                "active_schedule_ids": ["s1"],
                "preconditioned": False,
                "hvac_mode": "heat",
                "skipped_by_condition": True,
                "condition_entities": [COND],
            },
            "fahrenheit",
        )
        assert label == "TOU high-cost period active"
        assert settings == f"skipped: {COND} is off"

    def test_renderer_unchanged_for_normal_window(self) -> None:
        _label, settings = EVENT_RENDERERS["tou_schedule_window_active"](
            {"active_schedule_ids": ["s1"], "preconditioned": True, "hvac_mode": "cool"}, "fahrenheit"
        )
        assert settings == "coincided with active cool operation"

    def test_dashboard_settings_carry_condition_entity(self) -> None:
        extra = build_settings_extra(
            {
                "schedules": [
                    {
                        "id": "a",
                        "name": "A",
                        "days": ["mon"],
                        "start": "14:00",
                        "end": "19:00",
                        "cost_tag": "high",
                        "condition_entity": COND,
                    },
                    {"id": "b", "name": "B", "days": ["tue"], "start": "14:00", "end": "19:00", "cost_tag": "high"},
                ]
            }
        )
        assert extra["schedules"][0]["condition_entity"] == COND
        assert "condition_entity" not in extra["schedules"][1]
