"""Cross-consumer regression tests for Issue #847 — the briefing text and the
"Next Automation" status card must never disagree in wording OR timing about the
same nat_vent_cutoff_reason fact.

This is the 4th round of fixes on this exact bug class (#428/#430, #518, #528,
#535/#788/#814/#817/#818 all touched briefing/Next-Automation nat-vent wording or
timing without leaving behind an enforcement test). Per the approved plan
(use-the-mob-look-joyful-elephant.md, "Tests" + "Observability & Regression-
Proofing" #2/#3), this file is that enforcement mechanism:

1. TestBriefingAutomationSymmetry — one shared nat_vent_plan-shaped dict is fed to
   both _warm_day_plan()/_mild_day_plan() (briefing.py) and
   _compute_next_automation_action() (coordinator.py) and the two outputs must
   agree in REASON FRAMING (comfort_floor vs outdoor_rise), for both WARM and MILD
   day types.
2. TestBriefingStalenessOnCutoffDrift — reproduces the exact reported scenario: a
   frozen "sent" briefing baked in a comfort_floor/8AM cutoff; the live
   self._nat_vent_plan has since moved to outdoor_rise/11AM with no day_type or
   today_high change. _maybe_regenerate_briefing_for_drift() must now return True
   (a third staleness trigger on nat_vent_cutoff/nat_vent_cutoff_reason drift) —
   pre-fix, the function only checks day_type and today_high, so this is expected
   to FAIL until Craftsman-Impl's staleness trigger lands.

Marker note (TestBriefingAutomationSymmetry): Craftsman-Impl landed the shared
helper as `describe_nat_vent_cutoff_reason(reason)` in `nat_vent_plan.py`, exactly
as the plan's Approach §1 proposed:
  "comfort_floor"      -> "to hold the heat in"
  "outdoor_rise"/None  -> "before outdoor air warms past indoor"
Both `_warm_day_plan()`/`_mild_day_plan()` (briefing.py, conversational sentence)
and `_compute_next_automation_action()` (coordinator.py, compact status-card
phrase) call this same function and embed its return value verbatim into their
own sentence shape — so this test imports the real helper and asserts its actual
output string appears in both outputs, rather than guessing a marker. This is
stronger than a loose semantic check: it fails if either consumer ever stops
routing through the shared helper, not just if the wording happens to diverge.
"""

from __future__ import annotations

import types
from datetime import UTC, date, datetime, time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.briefing import _mild_day_plan, _warm_day_plan
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import DAY_TYPE_MILD, DAY_TYPE_WARM
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.nat_vent_plan import describe_nat_vent_cutoff_reason

# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

DEFAULT_WAKE = time(6, 30)
DEFAULT_SLEEP = time(22, 30)
COMFORT_HEAT = 70.0
COMFORT_COOL = 75.0


def _make_classification(day_type: str, today_high: float, today_low: float = 50.0) -> DayClassification:
    return DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=1.0,
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=today_high,
        tomorrow_low=today_low,
    )


def _shared_nat_vent_plan(reason: str, cutoff: datetime) -> dict:
    """Build one nat_vent_plan-shaped dict — the exact shape compute_nat_vent_plan()
    returns (nat_vent_plan.py) — reused identically by both the briefing call sites
    and coord._nat_vent_plan below, so both consumers really are reading the SAME
    fact, not two independently-constructed fixtures that happen to look similar."""
    return {
        "nat_vent_cutoff": cutoff,
        "nat_vent_cutoff_reason": reason,
        "comfort_floor_crossing_time": cutoff if reason == "comfort_floor" else None,
        "ceiling_breach_time": None,
        "precool_start_time": None,
        "any_nat_vent_window": True,
        "nat_vent_recovers": False,
        "recovery_time": None,
    }


def _make_automation_engine() -> MagicMock:
    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._grace_active = False
    ae._natural_vent_active = False
    ae._manual_override_active = False
    return ae


def _make_real_coordinator(nat_vent_plan: dict) -> ClimateAdvisorCoordinator:
    """Bare ClimateAdvisorCoordinator bound to the real
    _compute_next_automation_action(), via the established object.__new__() +
    types.MethodType() partial-instantiation pattern (see
    tests/test_status_sensors.py::_make_real_coordinator /
    tests/test_daily_record_accuracy.py). Adds self._nat_vent_plan, which the
    existing test_status_sensors.py helper doesn't set (getattr(..., None) default
    there is fine for its own tests, but this file specifically needs the
    WARM/MILD-day events branch to fire)."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._automation_enabled = True
    coord._startup_coalesce_active = False
    coord._startup_coalesce_expiry = None
    coord._startup_timer_fired = False
    coord._current_classification = None
    coord._occupancy_mode = "home"
    coord.automation_engine = _make_automation_engine()
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._door_open_timers = {}
    coord._door_open_timer_expiry = {}
    coord._pre_cool_trigger_dt = None
    coord._pre_cool_target = None
    coord._tou_phase_resolution = None
    coord._tou_active_cost_resolution = None
    coord.config = {}
    coord._nat_vent_plan = nat_vent_plan
    coord._last_predicted_indoor = []
    coord._compute_next_automation_action = types.MethodType(
        ClimateAdvisorCoordinator._compute_next_automation_action, coord
    )
    return coord


def _compute_next_automation_action(c: DayClassification, nat_vent_plan: dict, now_time: time) -> str:
    """Call the real ClimateAdvisorCoordinator._compute_next_automation_action()
    and return just the action-description string."""
    from custom_components.climate_advisor import coordinator as _coord_mod

    coord = _make_real_coordinator(nat_vent_plan)
    now_dt = datetime.combine(date(2026, 5, 11), now_time, tzinfo=UTC)
    with (
        patch.object(_coord_mod.dt_util, "now", return_value=now_dt),
        patch.object(_coord_mod.dt_util, "as_local", side_effect=lambda x: x),
    ):
        action, _time_str = coord._compute_next_automation_action(c)
    return action


# ---------------------------------------------------------------------------
# Item 3: cross-consumer symmetry
# ---------------------------------------------------------------------------


class TestBriefingAutomationSymmetry:
    """Issue #847: the briefing's close-sentence phrase and the Next Automation
    card's phrase must agree in REASON FRAMING for the same nat_vent_plan fact,
    across both WARM and MILD day types.

    Pre-fix, ALL FOUR comfort_floor cases below fail: _warm_day_plan() says
    "hold the heat in" while _compute_next_automation_action() unconditionally
    says "Outdoor will stop helping" (coordinator.py:8721-8724, reason-agnostic)
    — the exact contradiction from the reported screenshot. _mild_day_plan() has
    no reason branch at all yet (see TestMildDayPlanFloorWording in
    test_briefing.py), so its comfort_floor cases fail for the same underlying
    reason. The outdoor_rise cases may already coincidentally pass pre-fix
    (Next Automation's unconditional wording already happens to fit
    outdoor_rise) — that's expected; the bug is specifically that comfort_floor
    is never distinguished on the card side.
    """

    @pytest.mark.parametrize("day_type", [DAY_TYPE_WARM, DAY_TYPE_MILD])
    def test_comfort_floor_reason_agrees_between_briefing_and_next_automation(self, day_type):
        c = _make_classification(day_type, today_high=80 if day_type == DAY_TYPE_WARM else 68)
        cutoff = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
        plan = _shared_nat_vent_plan("comfort_floor", cutoff)
        expected_phrase = describe_nat_vent_cutoff_reason("comfort_floor")

        if day_type == DAY_TYPE_WARM:
            lines = _warm_day_plan(c, COMFORT_COOL, DEFAULT_WAKE, DEFAULT_SLEEP, warm_events=plan)
        else:
            lines = _mild_day_plan(c, COMFORT_HEAT, DEFAULT_WAKE, DEFAULT_SLEEP, mild_events=plan)
        briefing_text = "\n".join(lines)

        next_action = _compute_next_automation_action(c, plan, now_time=time(12, 30))

        assert expected_phrase in briefing_text, (
            f"briefing ({day_type}) should use the shared comfort_floor phrase"
            f" {expected_phrase!r} — got: {briefing_text!r}"
        )
        assert expected_phrase in next_action, (
            f"Next Automation card ({day_type}) should use the SAME shared phrase"
            f" {expected_phrase!r} the briefing used — got action string: {next_action!r}"
        )

    @pytest.mark.parametrize("day_type", [DAY_TYPE_WARM, DAY_TYPE_MILD])
    def test_outdoor_rise_reason_agrees_between_briefing_and_next_automation(self, day_type):
        c = _make_classification(day_type, today_high=80 if day_type == DAY_TYPE_WARM else 68)
        cutoff = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
        plan = _shared_nat_vent_plan("outdoor_rise", cutoff)
        expected_phrase = describe_nat_vent_cutoff_reason("outdoor_rise")
        comfort_floor_phrase = describe_nat_vent_cutoff_reason("comfort_floor")

        if day_type == DAY_TYPE_WARM:
            lines = _warm_day_plan(c, COMFORT_COOL, DEFAULT_WAKE, DEFAULT_SLEEP, warm_events=plan)
        else:
            lines = _mild_day_plan(c, COMFORT_HEAT, DEFAULT_WAKE, DEFAULT_SLEEP, mild_events=plan)
        briefing_text = "\n".join(lines)

        next_action = _compute_next_automation_action(c, plan, now_time=time(12, 30))

        assert expected_phrase in briefing_text, (
            f"briefing ({day_type}) should use the shared outdoor_rise phrase"
            f" {expected_phrase!r} — got: {briefing_text!r}"
        )
        assert comfort_floor_phrase not in briefing_text
        assert expected_phrase in next_action, (
            f"Next Automation card ({day_type}) should use the SAME shared phrase"
            f" {expected_phrase!r} the briefing used — got: {next_action!r}"
        )
        assert comfort_floor_phrase not in next_action, (
            f"Next Automation card ({day_type}) must NOT use comfort_floor framing"
            f" for an outdoor_rise reason — got: {next_action!r}"
        )


# ---------------------------------------------------------------------------
# Item 5: #430 live sanity-check fallback
# ---------------------------------------------------------------------------


class TestComfortFloorLiveSanityCheckFallback:
    """Closes #430 (filed alongside #428/#535, never fixed): a comfort_floor reason
    computed from the FORECAST curve must not be asserted as text if LIVE readings
    at render time no longer support it — outdoor has already risen above indoor,
    so the comfort-floor risk the sentence is about to describe is stale. Both
    _warm_day_plan() and _mild_day_plan() now accept current_indoor_temp/
    current_outdoor_temp and cross-check via free_cooling_direction_ok() (the same
    #428 guard, temperature.py) before emitting the comfort_floor phrase — falling
    back to the outdoor_rise phrase instead of stating a claim that's no longer
    live.
    """

    @pytest.mark.parametrize("day_type", [DAY_TYPE_WARM, DAY_TYPE_MILD])
    def test_stale_comfort_floor_falls_back_to_outdoor_rise_wording(self, day_type):
        c = _make_classification(day_type, today_high=80 if day_type == DAY_TYPE_WARM else 68)
        cutoff = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
        plan = _shared_nat_vent_plan("comfort_floor", cutoff)
        comfort_floor_phrase = describe_nat_vent_cutoff_reason("comfort_floor")
        outdoor_rise_phrase = describe_nat_vent_cutoff_reason("outdoor_rise")

        # Live readings: outdoor (80°F) has already risen above indoor (70°F) —
        # free_cooling_direction_ok(80, 70) is False, so the comfort_floor risk
        # this forecast-derived reason describes is no longer live at render time.
        kwargs = {"current_indoor_temp": 70.0, "current_outdoor_temp": 80.0}
        if day_type == DAY_TYPE_WARM:
            lines = _warm_day_plan(c, COMFORT_COOL, DEFAULT_WAKE, DEFAULT_SLEEP, warm_events=plan, **kwargs)
        else:
            lines = _mild_day_plan(c, COMFORT_HEAT, DEFAULT_WAKE, DEFAULT_SLEEP, mild_events=plan, **kwargs)
        text = "\n".join(lines)

        assert comfort_floor_phrase not in text, (
            f"briefing ({day_type}) asserted a stale comfort_floor claim — outdoor"
            f" has already risen above indoor by render time, so this should have"
            f" fallen back to outdoor_rise wording. Got: {text!r}"
        )
        assert outdoor_rise_phrase in text, (
            f"briefing ({day_type}) should fall back to the outdoor_rise phrase"
            f" when the live sanity check fails — got: {text!r}"
        )

    @pytest.mark.parametrize("day_type", [DAY_TYPE_WARM, DAY_TYPE_MILD])
    def test_live_comfort_floor_still_uses_comfort_floor_wording(self, day_type):
        """Control case: when live readings still support the comfort_floor risk
        (outdoor still cooler than indoor), the phrase must NOT be downgraded —
        confirms the fallback in the test above is actually conditional on the live
        check, not a blanket suppression."""
        c = _make_classification(day_type, today_high=80 if day_type == DAY_TYPE_WARM else 68)
        cutoff = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
        plan = _shared_nat_vent_plan("comfort_floor", cutoff)
        comfort_floor_phrase = describe_nat_vent_cutoff_reason("comfort_floor")

        # Live readings: outdoor (55°F) is still well below indoor (70°F) — the
        # comfort_floor risk is still live.
        kwargs = {"current_indoor_temp": 70.0, "current_outdoor_temp": 55.0}
        if day_type == DAY_TYPE_WARM:
            lines = _warm_day_plan(c, COMFORT_COOL, DEFAULT_WAKE, DEFAULT_SLEEP, warm_events=plan, **kwargs)
        else:
            lines = _mild_day_plan(c, COMFORT_HEAT, DEFAULT_WAKE, DEFAULT_SLEEP, mild_events=plan, **kwargs)
        text = "\n".join(lines)

        assert comfort_floor_phrase in text, (
            f"briefing ({day_type}) should still use the comfort_floor phrase when"
            f" the live sanity check passes — got: {text!r}"
        )


# ---------------------------------------------------------------------------
# Item 4: staleness regression — the exact reported scenario
# ---------------------------------------------------------------------------

REGEN_FULL = "Full briefing (regenerated)"
REGEN_SHORT = "TLDR (regenerated)"


def _side_effect_regen(**kwargs):
    if kwargs.get("verbosity") == "tldr_only":
        return REGEN_SHORT
    return REGEN_FULL


class TestBriefingStalenessOnCutoffDrift:
    """Issue #847 bug 1: a briefing generated early in the day can bake in a
    comfort_floor cutoff at 8 AM; by later cycles the live self._nat_vent_plan
    has moved to an outdoor_rise cutoff at 11 AM (the comfort-floor risk
    resolved) — but _maybe_regenerate_briefing_for_drift() pre-fix only checks
    day_type and today_high, neither of which changed, so the frozen briefing
    text never catches up. This reproduces exactly that scenario and asserts
    the function now returns True — expected to FAIL until Craftsman-Impl adds
    the third (nat_vent_cutoff/nat_vent_cutoff_reason) staleness trigger."""

    def _make_coord(self):
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.config = {
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
        }

        coord._briefing_sent_today = True
        coord._briefing_day_type = DAY_TYPE_WARM
        coord._briefing_today_high = 80
        coord._last_briefing = "Old briefing — hold the heat in at 8:00 AM"
        coord._last_briefing_short = "Old TLDR"
        coord._automation_enabled = True

        # Bug-1 setup: bake the comfort_floor/8AM cutoff into the "sent" briefing's
        # frozen fields, per the task's instruction to set the _briefing_* frozen
        # fields directly rather than driving a full _build_briefing_text() call
        # for the baseline. These attributes don't exist on pre-fix
        # ClimateAdvisorCoordinator instances, but setting them directly on this
        # bare instance is harmless pre-fix (the pre-fix method simply never reads
        # them) and becomes load-bearing once Craftsman-Impl's staleness trigger
        # lands.
        coord._briefing_nat_vent_cutoff = datetime(2026, 5, 11, 8, 0, 0, tzinfo=UTC)
        coord._briefing_nat_vent_cutoff_reason = "comfort_floor"

        coord.automation_engine = MagicMock()
        coord.automation_engine._grace_active = False
        coord.automation_engine._last_resume_source = None

        coord.learning = MagicMock()
        coord.learning.generate_suggestions.return_value = []
        coord.learning.get_thermal_model.return_value = {}

        coord._get_indoor_temp = MagicMock(return_value=None)
        coord._occupancy_mode = "home"
        coord._last_predicted_indoor = []
        coord._current_classification = None
        # Issue #847/#430: _build_briefing_text() now reads self.data for the live
        # outdoor-temp sanity check threaded into _warm_day_plan()/_mild_day_plan().
        # A bare object.__new__() instance has no DataUpdateCoordinator.data yet.
        coord.data = {}

        # Live plan has since moved: outdoor_rise/11AM. No day_type or today_high
        # change — this is the crux of the reported bug.
        coord._nat_vent_plan = _shared_nat_vent_plan("outdoor_rise", datetime(2026, 5, 11, 11, 0, 0, tzinfo=UTC))

        coord._build_briefing_text = types.MethodType(ClimateAdvisorCoordinator._build_briefing_text, coord)
        coord._maybe_regenerate_briefing_for_drift = types.MethodType(
            ClimateAdvisorCoordinator._maybe_regenerate_briefing_for_drift, coord
        )
        return coord

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_regen,
    )
    def test_nat_vent_cutoff_reason_drift_triggers_regeneration(self, mock_gen, mock_pred, mock_outdoor):
        coord = self._make_coord()
        # day_type and today_high are UNCHANGED from the frozen briefing — only
        # the nat_vent_plan reason/time moved. Pre-fix this must return False
        # (neither of the two existing triggers fires); post-fix it must return
        # True.
        coord._current_classification = _make_classification(DAY_TYPE_WARM, today_high=80)

        regenerated = coord._maybe_regenerate_briefing_for_drift()

        assert regenerated is True, (
            "Live nat_vent_plan moved from comfort_floor/8AM to outdoor_rise/11AM"
            " with no day_type/today_high change — this must trigger a briefing"
            " regeneration (Issue #847 bug 1) so the displayed text doesn't stay"
            " stale for hours. Pre-fix this correctly returns False, which is the"
            " bug being reproduced here."
        )
