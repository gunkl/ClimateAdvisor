"""Tests for status pane improvements (Issue #18b / #23).

Tests for:
- _compute_automation_status logic
- _compute_next_automation_action logic
- ClimateAdvisorNextActionSensor name rename
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "hot",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 90,
        "today_low": 70,
        "tomorrow_high": 88,
        "tomorrow_low": 68,
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


def _make_automation_engine(
    *,
    is_paused_by_door: bool = False,
    natural_vent_active: bool = False,
    grace_active: bool = False,
    last_resume_source: str | None = None,
    last_grace_trigger: str | None = None,
    grace_end_time: str | None = None,
    grace_duration_seconds: float | None = None,
    last_action_reason: str | None = None,
) -> MagicMock:
    """Create a mock AutomationEngine with given state flags.

    Also defaults the newer gates _compute_automation_status() checks
    (override-confirm pending, stuck-grace detection, resumed-from-pause) to
    inactive so callers exercising the original 4-flag surface see the same
    "active"/"disabled"/"paused"/"grace period" outcomes as before.
    """
    ae = MagicMock()
    ae.is_paused_by_door = is_paused_by_door
    ae.natural_vent_active = natural_vent_active
    ae._grace_active = grace_active
    ae._last_resume_source = last_resume_source
    ae._last_grace_trigger = last_grace_trigger
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._grace_end_time = grace_end_time
    ae._grace_duration_seconds = grace_duration_seconds
    # Issue #625: _last_action_reason must no longer influence _compute_automation_status()'s
    # grace text — set it to a value that would fail a test if it leaked through, so any
    # regression that starts reading it again is caught immediately.
    ae._last_action_reason = last_action_reason or "REGRESSION: _last_action_reason leaked onto Status"
    ae._resumed_from_pause = False
    ae._is_within_planned_window_period = MagicMock(return_value=False)
    return ae


def _make_real_coordinator(
    automation_enabled: bool,
    automation_engine,
    occupancy_mode: str = "home",
    tou_phase_resolution=None,
    tou_active_cost_resolution=None,
    current_classification=None,
):
    """Build a bare ClimateAdvisorCoordinator bound to the real status-computation methods.

    Uses object.__new__() + types.MethodType() (the established coordinator
    partial-instantiation pattern — see test_daily_record_accuracy.py) rather than
    replicating the method bodies, so these tests exercise the real
    ClimateAdvisorCoordinator._compute_automation_status/_compute_next_automation_action.

    tou_active_cost_resolution/current_classification (Phase 3d): threaded through so
    tests can exercise _compute_automation_status()'s new "TOU high-cost window active"
    branch, which reads self._tou_active_cost_resolution and
    self._current_classification.hvac_mode.
    """
    import types

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._automation_enabled = automation_enabled
    coord._startup_coalesce_active = False
    coord._startup_coalesce_expiry = None
    coord._startup_timer_fired = False
    coord._current_classification = current_classification
    coord._occupancy_mode = occupancy_mode
    coord.automation_engine = automation_engine
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._door_open_timers = {}
    coord._door_open_timer_expiry = {}
    coord._pre_cool_trigger_dt = None
    coord._pre_cool_target = None
    coord._tou_phase_resolution = tou_phase_resolution
    coord._tou_active_cost_resolution = tou_active_cost_resolution
    coord.config = {}
    coord._compute_automation_status = types.MethodType(ClimateAdvisorCoordinator._compute_automation_status, coord)
    coord._compute_next_automation_action = types.MethodType(
        ClimateAdvisorCoordinator._compute_next_automation_action, coord
    )
    return coord


def _compute_automation_status(
    automation_enabled: bool,
    automation_engine,
    now_dt: datetime | None = None,
    tou_phase_resolution=None,
    tou_active_cost_resolution=None,
    current_classification=None,
) -> str:
    """Call the real ClimateAdvisorCoordinator._compute_automation_status().

    now_dt: when given, patches dt_util.now/parse_datetime/as_local so
    _format_grace_remaining() (called internally for the grace-active branch) resolves
    against a fixed, real datetime instead of the default MagicMock stubs.
    """
    from custom_components.climate_advisor import coordinator as _coord_mod

    coord = _make_real_coordinator(
        automation_enabled,
        automation_engine,
        tou_phase_resolution=tou_phase_resolution,
        tou_active_cost_resolution=tou_active_cost_resolution,
        current_classification=current_classification,
    )
    if now_dt is None:
        return coord._compute_automation_status()
    with (
        patch.object(_coord_mod.dt_util, "now", return_value=now_dt),
        patch.object(_coord_mod.dt_util, "as_local", side_effect=lambda x: x),
        patch.object(_coord_mod.dt_util, "parse_datetime", side_effect=lambda s: datetime.fromisoformat(s)),
    ):
        return coord._compute_automation_status()


def _format_grace_remaining(grace_end_time: str | None, grace_duration_seconds, now_dt: datetime) -> str:
    """Call the real ClimateAdvisorCoordinator._format_grace_remaining() directly."""
    import types

    from custom_components.climate_advisor import coordinator as _coord_mod
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._format_grace_remaining = types.MethodType(ClimateAdvisorCoordinator._format_grace_remaining, coord)
    ae = MagicMock()
    ae._grace_end_time = grace_end_time
    ae._grace_duration_seconds = grace_duration_seconds
    with (
        patch.object(_coord_mod.dt_util, "now", return_value=now_dt),
        patch.object(_coord_mod.dt_util, "as_local", side_effect=lambda x: x),
        patch.object(_coord_mod.dt_util, "parse_datetime", side_effect=lambda s: datetime.fromisoformat(s)),
    ):
        return coord._format_grace_remaining(ae)


def _compute_next_automation_action(
    c,
    automation_engine,
    config: dict,
    now_time: time,
    tou_phase_resolution=None,
    pre_cool_trigger_dt=None,
    pre_cool_target=None,
) -> tuple[str, str]:
    """Call the real ClimateAdvisorCoordinator._compute_next_automation_action().

    now_time (a plain time-of-day) is combined with a fixed date and patched in
    as dt_util.now()/as_local() — the real method now works in full datetimes
    (to correctly order cross-midnight events like pre-cool), not bare times.

    tou_phase_resolution (Phase 3e test-harness prerequisite): threaded through the
    same way the Status-card wrapper (_compute_automation_status above) already does
    — this wrapper previously did not, which would have silently no-op'd any TOU
    candidate test against _compute_next_automation_action().

    pre_cool_trigger_dt/pre_cool_target: overrides for the tie-break test against the
    existing pre-cool-ceiling candidate (_make_real_coordinator always defaults these
    to None otherwise, which would silently exclude that candidate from any test).
    """
    from datetime import date, datetime
    from unittest.mock import patch

    from custom_components.climate_advisor import coordinator as _coord_mod

    coord = _make_real_coordinator(True, automation_engine, tou_phase_resolution=tou_phase_resolution)
    coord.config = config
    if pre_cool_trigger_dt is not None:
        coord._pre_cool_trigger_dt = pre_cool_trigger_dt
        coord._pre_cool_target = pre_cool_target

    now_dt = datetime.combine(date(2026, 7, 10), now_time)
    with (
        patch.object(_coord_mod.dt_util, "now", return_value=now_dt),
        patch.object(_coord_mod.dt_util, "as_local", side_effect=lambda x: x),
    ):
        return coord._compute_next_automation_action(c)


# ---------------------------------------------------------------------------
# Tests: _compute_automation_status
# ---------------------------------------------------------------------------


class TestComputeAutomationStatus:
    """Tests for _compute_automation_status logic."""

    def test_automation_status_active(self):
        """No pause, no grace → 'active'."""
        ae = _make_automation_engine()
        result = _compute_automation_status(True, ae)
        assert result == "active"

    def test_automation_status_paused_by_door(self):
        """paused_by_door=True → 'paused — door/window open'."""
        ae = _make_automation_engine(is_paused_by_door=True)
        result = _compute_automation_status(True, ae)
        assert result == "paused — door/window open"

    def test_automation_status_grace_period(self):
        """grace_active=True → contains 'grace period'."""
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        result = _compute_automation_status(True, ae)
        assert "grace period" in result
        assert "manual" in result

    def test_automation_status_grace_period_no_source(self):
        """grace_active=True with no resume source → defaults to 'automation'."""
        ae = _make_automation_engine(grace_active=True, last_resume_source=None)
        result = _compute_automation_status(True, ae)
        assert "grace period" in result
        assert "automation" in result

    def test_automation_status_disabled(self):
        """_automation_enabled=False → 'disabled'."""
        ae = _make_automation_engine()
        result = _compute_automation_status(False, ae)
        assert result == "disabled"

    def test_disabled_takes_priority_over_paused(self):
        """Disabled overrides paused state."""
        ae = _make_automation_engine(is_paused_by_door=True)
        result = _compute_automation_status(False, ae)
        assert result == "disabled"

    def test_tou_preconditioning_cool_mode(self):
        """TOUPhase.PRECONDITIONING with mode='cool' -> 'pre-cooling — ...' (Issue #786)."""
        from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

        resolution = TOUPhaseResolution(
            phase=TOUPhase.PRECONDITIONING,
            target=68.0,
            mode="cool",
            schedule_id="s1",
            schedule_start=datetime(2026, 1, 5, 16, 0),
            precondition_start=datetime(2026, 1, 5, 13, 0),
        )
        ae = _make_automation_engine()
        result = _compute_automation_status(
            True, ae, now_dt=datetime(2026, 1, 5, 13, 30), tou_phase_resolution=resolution
        )
        assert result.startswith("pre-cooling — TOU high-cost period")
        assert "4:00 PM" in result

    def test_tou_preconditioning_heat_mode(self):
        """TOUPhase.PRECONDITIONING with mode='heat' -> 'pre-heating — ...' (Issue #786)."""
        from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

        resolution = TOUPhaseResolution(
            phase=TOUPhase.PRECONDITIONING,
            target=76.0,
            mode="heat",
            schedule_id="s2",
            schedule_start=datetime(2026, 1, 5, 16, 0),
            precondition_start=datetime(2026, 1, 5, 13, 0),
        )
        ae = _make_automation_engine()
        result = _compute_automation_status(
            True, ae, now_dt=datetime(2026, 1, 5, 13, 30), tou_phase_resolution=resolution
        )
        assert result.startswith("pre-heating — TOU high-cost period")

    def test_tou_none_phase_does_not_affect_status(self):
        """TOUPhase.NONE (window fields populated but not currently preconditioning,
        per the chart-rendering use case) must NOT change the status string."""
        from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

        resolution = TOUPhaseResolution(
            phase=TOUPhase.NONE,
            target=68.0,
            mode="cool",
            schedule_id="s1",
            schedule_start=datetime(2026, 1, 5, 16, 0),
            precondition_start=datetime(2026, 1, 5, 14, 0),
        )
        ae = _make_automation_engine()
        result = _compute_automation_status(True, ae, tou_phase_resolution=resolution)
        assert result == "active"

    def test_grace_period_takes_priority_over_tou_preconditioning(self):
        """A door/window grace period is a higher-priority mechanism reason than TOU
        pre-conditioning — the Status card shows only one reason at a time."""
        from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

        resolution = TOUPhaseResolution(
            phase=TOUPhase.PRECONDITIONING,
            target=68.0,
            mode="cool",
            schedule_id="s1",
            schedule_start=datetime(2026, 1, 5, 16, 0),
            precondition_start=datetime(2026, 1, 5, 13, 0),
        )
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        result = _compute_automation_status(True, ae, tou_phase_resolution=resolution)
        assert "grace period" in result
        assert "pre-cooling" not in result


class TestTouActiveWindowStatus:
    """Phase 3d / Investigation D: the active cost_period window itself (not just the
    PRECONDITIONING lead-time before it) must be visible on the Status card — the exact
    live-instance finding David hit (a configured schedule covering `now`, but
    hvac_mode="off" that day, produced zero Status-card/Activity-Record trace).
    """

    def _resolution(self, *, cost_tag="high", schedule_end=None):
        from custom_components.climate_advisor.scheduler import ScheduleResolution

        return ScheduleResolution(
            cost_tag=cost_tag,
            active_schedule_ids=("s1",),
            schedule_end=schedule_end or datetime(2026, 8, 31, 10, 0),
        )

    def test_active_high_window_with_hvac_off_shows_no_preconditioning_needed(self):
        """The exact reproduction: hvac_mode='off' -> distinct 'no pre-conditioning
        needed today' wording, not a blank/unrelated status."""
        c = _make_classification(hvac_mode="off")
        ae = _make_automation_engine()
        result = _compute_automation_status(
            True,
            ae,
            now_dt=datetime(2026, 8, 31, 9, 45),
            tou_active_cost_resolution=self._resolution(),
            current_classification=c,
        )
        assert result != ""
        assert "TOU high-cost period active" in result
        assert "no pre-conditioning needed today" in result

    def test_active_high_window_with_real_heating_shows_plain_active_text(self):
        """hvac_mode='heat' (or 'cool') -> the window coincided with real HVAC
        operation, so the 'no pre-conditioning needed' caveat must NOT appear."""
        c = _make_classification(hvac_mode="heat")
        ae = _make_automation_engine()
        result = _compute_automation_status(
            True,
            ae,
            now_dt=datetime(2026, 8, 31, 9, 45),
            tou_active_cost_resolution=self._resolution(),
            current_classification=c,
        )
        assert "TOU high-cost period active" in result
        assert "no pre-conditioning needed today" not in result

    def test_no_covering_schedule_does_not_affect_status(self):
        """cost_tag=None (no covering schedule) must not trigger the new branch."""
        from custom_components.climate_advisor.scheduler import ScheduleResolution

        c = _make_classification(hvac_mode="off")
        ae = _make_automation_engine()
        resolution = ScheduleResolution(cost_tag=None, active_schedule_ids=(), schedule_end=None)
        result = _compute_automation_status(True, ae, tou_active_cost_resolution=resolution, current_classification=c)
        assert result == "active"

    def test_low_cost_tag_does_not_affect_status(self):
        """COST_TAG_LOW has zero live behavioral consumers (Investigation A) — must not
        trigger this branch either, matching that established design decision."""
        c = _make_classification(hvac_mode="off")
        ae = _make_automation_engine()
        result = _compute_automation_status(
            True,
            ae,
            tou_active_cost_resolution=self._resolution(cost_tag="low"),
            current_classification=c,
        )
        assert result == "active"

    def test_grace_period_takes_priority_over_active_tou_window(self):
        """Grace is a higher-priority mechanism reason (Status Card Ontology) than the
        TOU active-window text."""
        c = _make_classification(hvac_mode="off")
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        result = _compute_automation_status(
            True, ae, tou_active_cost_resolution=self._resolution(), current_classification=c
        )
        assert "grace period" in result
        assert "TOU high-cost period active" not in result

    def test_preconditioning_phase_takes_priority_over_active_window_branch(self):
        """PRECONDITIONING is checked first in the function's branch order (and by
        construction never overlaps with the active-window time range anyway, since
        precondition_start < schedule_start), but this pins the ordering explicitly."""
        from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

        c = _make_classification(hvac_mode="cool")
        ae = _make_automation_engine()
        phase_resolution = TOUPhaseResolution(
            phase=TOUPhase.PRECONDITIONING,
            target=68.0,
            mode="cool",
            schedule_id="s1",
            schedule_start=datetime(2026, 8, 31, 16, 0),
            precondition_start=datetime(2026, 8, 31, 13, 0),
        )
        result = _compute_automation_status(
            True,
            ae,
            now_dt=datetime(2026, 8, 31, 13, 30),
            tou_phase_resolution=phase_resolution,
            tou_active_cost_resolution=self._resolution(),
            current_classification=c,
        )
        assert result.startswith("pre-cooling")


# ---------------------------------------------------------------------------
# Tests: grace-period text no longer duplicates the Fan (WHF) card (Issue #625)
# ---------------------------------------------------------------------------


class TestGraceStatusNoLongerLeaksLastActionReason:
    """_compute_automation_status()'s grace branch must never surface
    _last_action_reason — that duplicated the Fan (WHF) card for fan-triggered grace
    periods and was blank/stale for manual thermostat overrides (Issue #625)."""

    NOW = datetime(2026, 8, 11, 19, 39)  # 7:39 PM

    def test_whf_manual_override_grace_shows_trigger_label_not_reason(self):
        """The exact screenshot scenario: fan_manual_override trigger → 'WHF override'
        label, never the old free-text sentence."""
        end = (self.NOW + timedelta(hours=12)).isoformat()
        ae = _make_automation_engine(
            grace_active=True,
            last_resume_source="manual",
            last_grace_trigger="fan_manual_override",
            grace_end_time=end,
            grace_duration_seconds=12 * 3600,
            last_action_reason="Set HVAC to off — whole-house fan manually turned on — "
            "suppressing HVAC to prevent AC/fan fighting",
        )
        result = _compute_automation_status(True, ae, now_dt=self.NOW)
        assert result == "grace period (manual) — WHF override — 12h (ends 7:39 AM)"
        assert "suppressing HVAC" not in result
        assert "AC/fan fighting" not in result

    def test_manual_thermostat_override_grace_shows_trigger_label(self):
        """override_confirmed trigger (manual thermostat change, no fan involved) →
        'thermostat override' label — this is the gap case where _last_action_reason
        was never populated at all (_confirm_override() doesn't call _record_action())."""
        end = (self.NOW + timedelta(minutes=30)).isoformat()
        ae = _make_automation_engine(
            grace_active=True,
            last_resume_source="manual",
            last_grace_trigger="override_confirmed",
            grace_end_time=end,
            grace_duration_seconds=1800,
            last_action_reason=None,  # the real gap: never populated
        )
        result = _compute_automation_status(True, ae, now_dt=self.NOW)
        assert result == "grace period (manual) — thermostat override — 30 min (ends 8:09 PM)"

    def test_unknown_trigger_omits_cause_segment_never_leaks_raw_string(self):
        """A trigger with no entry in _GRACE_TRIGGER_LABELS must not leak its raw
        internal string onto the UI — just fall back to source + timing."""
        end = (self.NOW + timedelta(minutes=15)).isoformat()
        ae = _make_automation_engine(
            grace_active=True,
            last_resume_source="automation",
            last_grace_trigger="some_future_trigger_nobody_mapped_yet",
            grace_end_time=end,
            grace_duration_seconds=900,
        )
        result = _compute_automation_status(True, ae, now_dt=self.NOW)
        assert result == "grace period (automation) — 15 min (ends 7:54 PM)"
        assert "some_future_trigger_nobody_mapped_yet" not in result

    def test_no_trigger_stored_omits_cause_segment(self):
        """last_grace_trigger=None (e.g. an older/legacy grace state) → no cause segment."""
        end = (self.NOW + timedelta(minutes=15)).isoformat()
        ae = _make_automation_engine(
            grace_active=True,
            last_resume_source="automation",
            last_grace_trigger=None,
            grace_end_time=end,
            grace_duration_seconds=900,
        )
        result = _compute_automation_status(True, ae, now_dt=self.NOW)
        assert result == "grace period (automation) — 15 min (ends 7:54 PM)"

    def test_resumed_from_pause_branch_unaffected(self):
        """The early-return resumed-from-pause branch never looks at the trigger map."""
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        ae._resumed_from_pause = True
        result = _compute_automation_status(True, ae)
        assert result == "resumed — door/window override"


class TestFormatGraceRemaining:
    """Unit tests for _format_grace_remaining()'s duration+end-time formatting (Issue #625)."""

    NOW = datetime(2026, 8, 11, 19, 39)  # 7:39 PM

    def test_sub_hour_duration(self):
        end = (self.NOW + timedelta(minutes=30)).isoformat()
        result = _format_grace_remaining(end, 1800, self.NOW)
        assert result == " — 30 min (ends 8:09 PM)"

    def test_whole_hour_duration(self):
        end = (self.NOW + timedelta(minutes=120)).isoformat()
        result = _format_grace_remaining(end, 7200, self.NOW)
        assert result == " — 2h (ends 9:39 PM)"

    def test_non_whole_hour_duration_over_an_hour(self):
        """150 min (the screenshot's applied duration) → '2.5h', not '150 min'."""
        end = (self.NOW + timedelta(minutes=150)).isoformat()
        result = _format_grace_remaining(end, 150 * 60, self.NOW)
        assert result == " — 2.5h (ends 10:09 PM)"

    def test_twelve_hour_rf_remote_timer(self):
        """The screenshot's actual WHF RF-remote timer duration."""
        end = (self.NOW + timedelta(hours=12)).isoformat()
        result = _format_grace_remaining(end, 12 * 3600, self.NOW)
        assert result == " — 12h (ends 7:39 AM)"

    def test_missing_end_time_returns_empty(self):
        assert _format_grace_remaining(None, 1800, self.NOW) == ""

    def test_missing_duration_returns_empty(self):
        end = (self.NOW + timedelta(minutes=30)).isoformat()
        assert _format_grace_remaining(end, None, self.NOW) == ""

    def test_past_end_time_returns_empty(self):
        end = (self.NOW - timedelta(minutes=5)).isoformat()
        assert _format_grace_remaining(end, 1800, self.NOW) == ""


# ---------------------------------------------------------------------------
# Tests: _compute_next_automation_action
# ---------------------------------------------------------------------------


class TestComputeNextAutomationAction:
    """Tests for _compute_next_automation_action logic."""

    def test_no_classification_returns_waiting(self):
        """When classification is None → 'Waiting for classification...'"""
        ae = _make_automation_engine()
        action, t = _compute_next_automation_action(None, ae, {}, time(8, 0))
        assert action == "Waiting for classification..."
        assert t == ""

    def test_paused_by_door_still_shows_real_next_step(self):
        """When paused_by_door → still shows the real next scheduled step, not mechanism text (Issue #527).

        The Status card is the only place "paused" belongs (_compute_automation_status()).
        Next Automation answers "what's the plan," which holds regardless of the pause —
        it's simply deferred until the pause clears.
        """
        ae = _make_automation_engine(is_paused_by_door=True)
        c = _make_classification(hvac_mode="cool")
        action, t = _compute_next_automation_action(c, ae, {}, time(8, 0))
        assert "paused" not in action.lower()
        assert "Bedtime" in action

    def test_grace_period_active_still_shows_real_next_step(self):
        """When grace period active → still shows the real next scheduled step, not mechanism text (Issue #527)."""
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        c = _make_classification(hvac_mode="cool")
        action, t = _compute_next_automation_action(c, ae, {}, time(8, 0))
        assert "grace period" not in action.lower()
        assert "Bedtime" in action

    def test_before_briefing_time_returns_briefing_event(self):
        """Time before briefing_time → first event is 'Send daily briefing'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "06:00:00", "wake_time": "06:30:00", "sleep_time": "22:30:00"}
        # Current time is 05:00 — before briefing at 06:00
        action, t = _compute_next_automation_action(c, ae, config, time(5, 0))
        assert action == "Send daily briefing"
        assert t == "6:00 AM"

    def test_before_bedtime_cool_day_returns_cool_setback(self):
        """Time after wakeup but before bedtime on cool day → bedtime cool setback."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
            "comfort_cool": 75,
            "sleep_cool": 72,
        }
        # Current time is 14:00 — after briefing and wakeup, before sleep
        action, t = _compute_next_automation_action(c, ae, config, time(14, 0))
        assert "Bedtime" in action
        assert "cool setback" in action
        # The real method reads CONF_SLEEP_COOL directly (not a comfort_cool+delta
        # formula) — matches select_comfort_band(in_sleep_window=True), see
        # coordinator.py's _compute_next_automation_action() comment.
        assert "72°F" in action

    def test_before_bedtime_heat_day_returns_heat_setback(self):
        """Time before bedtime on heat day → bedtime heat setback with correct temp."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="heat", setback_modifier=2.0)
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
            "comfort_heat": 70,
            "sleep_heat": 64,
        }
        # Current time is 20:00 — before sleep at 22:30
        action, t = _compute_next_automation_action(c, ae, config, time(20, 0))
        assert "Bedtime" in action
        assert "heat setback" in action
        # The real method reads CONF_SLEEP_HEAT directly, not a comfort_heat-4+modifier
        # formula (that formula predates the sleep_heat/sleep_cool config keys).
        assert "64°F" in action

    def test_after_all_events_returns_no_more_actions(self):
        """After all scheduled events have passed → 'No more actions today'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        # Current time is 23:00 — after all events
        action, t = _compute_next_automation_action(c, ae, config, time(23, 0))
        assert action == "No more actions today"
        assert t == ""

    def test_wakeup_event_for_heat_mode(self):
        """Before wakeup time in heat mode → morning wake-up heat comfort."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="heat")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        # Current time is 06:05 — between briefing and wake_time
        action, t = _compute_next_automation_action(c, ae, config, time(6, 5))
        assert "Morning wake-up" in action
        assert "heat" in action

    def test_off_mode_wakeup_returns_check(self):
        """Before wakeup in off mode → 'Morning wake-up check'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="off")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        action, t = _compute_next_automation_action(c, ae, config, time(6, 5))
        assert action == "Morning wake-up check"

    def test_off_mode_bedtime_returns_check(self):
        """Before bedtime in off mode → 'Bedtime check'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="off")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        action, t = _compute_next_automation_action(c, ae, config, time(20, 0))
        assert action == "Bedtime check"


# ---------------------------------------------------------------------------
# Tests: Issue #528 forecast-derived Next Automation candidates
# ---------------------------------------------------------------------------


def _curve(temps: list[float], start_hour: int, ts_key: str = "ts", temp_key: str = "temp") -> list[dict]:
    """Build a naive-datetime curve matching the (ts_key, temp_key) shape a given
    consumer expects — {"ts","temp"} for predicted-indoor curves, {"datetime","temperature"}
    for raw self._hourly_forecast_temps entries."""
    base = datetime(2026, 7, 10, start_hour, 0, 0)
    return [{ts_key: (base + timedelta(hours=i)).isoformat(), temp_key: t} for i, t in enumerate(temps)]


def _compute_next_automation_action_with_forecast(
    c,
    automation_engine,
    config: dict,
    now_time: time,
    predicted_indoor: list[dict] | None,
    hourly_forecast_temps: list[dict] | None,
):
    """Like _compute_next_automation_action(), but also wires forecast-curve state
    (self._last_predicted_indoor / self._hourly_forecast_temps) needed by the
    Issue #528 candidates — not part of the base wrapper since most existing tests
    don't need it."""
    from custom_components.climate_advisor import coordinator as _coord_mod

    coord = _make_real_coordinator(True, automation_engine)
    coord.config = config
    coord._last_predicted_indoor = predicted_indoor
    coord._hourly_forecast_temps = hourly_forecast_temps or []

    now_dt = datetime.combine(date(2026, 7, 10), now_time)
    with (
        patch.object(_coord_mod.dt_util, "now", return_value=now_dt),
        patch.object(_coord_mod.dt_util, "as_local", side_effect=lambda x: x),
    ):
        return coord._compute_next_automation_action(c)


class TestTouNextAutomationCandidate:
    """Phase 3e: an upcoming TOU precondition_start must appear as a
    _compute_next_automation_action() candidate — previously entirely absent
    (confirmed zero references via grep, per the plan's investigation)."""

    def _resolution(self, *, mode="cool", target=68.0, precondition_start=None, schedule_start=None):
        from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

        return TOUPhaseResolution(
            phase=TOUPhase.NONE,
            target=target,
            mode=mode,
            schedule_id="s1",
            schedule_start=schedule_start or datetime(2026, 7, 10, 16, 0),
            precondition_start=precondition_start or datetime(2026, 7, 10, 13, 0),
        )

    def test_upcoming_tou_precondition_is_a_candidate(self):
        """precondition_start in the future -> appears as the next action when it's the
        earliest candidate."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "00:00:00", "wake_time": "00:00:01", "sleep_time": "23:59:00"}
        resolution = self._resolution(mode="cool", target=68.0, precondition_start=datetime(2026, 7, 10, 13, 0))
        action, t = _compute_next_automation_action(c, ae, config, time(12, 30), tou_phase_resolution=resolution)
        assert "Pre-cool for TOU schedule" in action
        assert "68" in action
        assert t == "1:00 PM"

    def test_upcoming_tou_precondition_heat_mode_wording(self):
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="heat")
        config = {"briefing_time": "00:00:00", "wake_time": "00:00:01", "sleep_time": "23:59:00"}
        resolution = self._resolution(mode="heat", target=76.0, precondition_start=datetime(2026, 7, 10, 13, 0))
        action, _t = _compute_next_automation_action(c, ae, config, time(12, 30), tou_phase_resolution=resolution)
        assert "Pre-heat for TOU schedule" in action

    def test_already_active_preconditioning_is_excluded(self):
        """precondition_start <= now (already inside the PRECONDITIONING window) must
        NOT be re-offered as a future candidate — the guard alone (precondition_start >
        now) correctly excludes this, no separate phase check needed."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "00:00:00", "wake_time": "00:00:01", "sleep_time": "23:59:00"}
        resolution = self._resolution(mode="cool", target=68.0, precondition_start=datetime(2026, 7, 10, 11, 0))
        action, _t = _compute_next_automation_action(c, ae, config, time(12, 30), tou_phase_resolution=resolution)
        assert "TOU schedule" not in action

    def test_no_tou_resolution_produces_no_tou_candidate(self):
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "00:00:00", "wake_time": "00:00:01", "sleep_time": "23:59:00"}
        action, _t = _compute_next_automation_action(c, ae, config, time(12, 30), tou_phase_resolution=None)
        assert "TOU schedule" not in action

    def test_tie_break_earliest_wins_against_pre_cool_ceiling_tou_earlier(self):
        """Both a TOU candidate and the pre-cool-ceiling candidate are upcoming — earliest
        time wins, no TOU-specific precedence added."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "00:00:00", "wake_time": "00:00:01", "sleep_time": "23:59:00"}
        resolution = self._resolution(mode="cool", target=68.0, precondition_start=datetime(2026, 7, 10, 13, 0))
        action, t = _compute_next_automation_action(
            c,
            ae,
            config,
            time(12, 30),
            tou_phase_resolution=resolution,
            pre_cool_trigger_dt=datetime(2026, 7, 10, 14, 0),
            pre_cool_target=70.0,
        )
        assert "Pre-cool for TOU schedule" in action
        assert t == "1:00 PM"

    def test_tie_break_earliest_wins_against_pre_cool_ceiling_precool_earlier(self):
        """Same as above but the pre-cool-ceiling candidate is earlier — it must win,
        proving there's no TOU-specific precedence either direction."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "00:00:00", "wake_time": "00:00:01", "sleep_time": "23:59:00"}
        resolution = self._resolution(mode="cool", target=68.0, precondition_start=datetime(2026, 7, 10, 15, 0))
        action, t = _compute_next_automation_action(
            c,
            ae,
            config,
            time(12, 30),
            tou_phase_resolution=resolution,
            pre_cool_trigger_dt=datetime(2026, 7, 10, 14, 0),
            pre_cool_target=70.0,
        )
        assert "Pre-cool ceiling" in action
        assert "TOU schedule" not in action
        assert t == "2:00 PM"


class TestNextActionNeverShowsTou:
    """Regression: 'Next User Action' (_compute_next_action(), a DIFFERENT function
    from _compute_next_automation_action() tested above) must never surface TOU state
    — CLAUDE.md's Issue #527 boundary excludes automation-mechanism state from this
    card entirely. TOU pre-conditioning is mechanism state (an autonomous setpoint
    action), not something requiring occupant action, so it belongs on Status and
    Next Automation only.
    """

    def test_next_action_ignores_tou_phase_resolution_entirely(self):
        import types

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
        from custom_components.climate_advisor.scheduler import TOUPhase, TOUPhaseResolution

        c = _make_classification(hvac_mode="cool")
        ae = _make_automation_engine()
        coord = _make_real_coordinator(True, ae)
        coord._compute_next_action = types.MethodType(ClimateAdvisorCoordinator._compute_next_action, coord)
        coord._tou_phase_resolution = TOUPhaseResolution(
            phase=TOUPhase.PRECONDITIONING,
            target=68.0,
            mode="cool",
            schedule_id="s1",
            schedule_start=datetime(2026, 7, 10, 16, 0),
            precondition_start=datetime(2026, 7, 10, 13, 0),
        )
        result = coord._compute_next_action(c)
        # _compute_next_action() has no TOU-reading code path at all — this asserts the
        # absence of any TOU-related wording, proving the boundary holds even when a
        # PRECONDITIONING resolution is present on the coordinator.
        assert "TOU" not in result
        assert "pre-cool" not in result.lower()


class TestHotDayWindowOpportunityCandidates:
    """HOT-day window-cooling opportunity candidates (Issue #528)."""

    def test_morning_opportunity_present_before_start(self):
        ae = _make_automation_engine()
        c = _make_classification(
            day_type="hot",
            window_opportunity_morning=True,
            window_opportunity_morning_start=time(6, 0),
        )
        # briefing_time defaults to 06:00:00 too — push it out of the way so this test
        # isolates the window-opportunity candidate instead of a same-time tiebreak.
        action, t = _compute_next_automation_action(c, ae, {"briefing_time": "23:00:00"}, time(5, 0))
        assert action == "Morning window cooling opportunity"
        assert t == "6:00 AM"

    def test_morning_opportunity_absent_once_past(self):
        ae = _make_automation_engine()
        c = _make_classification(
            day_type="hot",
            window_opportunity_morning=True,
            window_opportunity_morning_start=time(6, 0),
        )
        action, _t = _compute_next_automation_action(c, ae, {}, time(7, 0))
        assert action != "Morning window cooling opportunity"

    def test_morning_opportunity_absent_when_flag_false(self):
        ae = _make_automation_engine()
        c = _make_classification(day_type="hot", window_opportunity_morning=False)
        action, _t = _compute_next_automation_action(c, ae, {}, time(5, 0))
        assert action != "Morning window cooling opportunity"

    def test_evening_opportunity_present_before_start(self):
        ae = _make_automation_engine()
        c = _make_classification(
            day_type="hot",
            window_opportunity_evening=True,
            window_opportunity_evening_start=time(17, 0),
        )
        action, t = _compute_next_automation_action(c, ae, {}, time(16, 0))
        assert action == "Evening window cooling opportunity"
        assert t == "5:00 PM"


class TestNatVentStartCandidate:
    """Nat-vent/WHF start prediction candidate (Issue #528)."""

    def test_predicted_when_gate_crosses_and_window_open(self):
        """Uses the real decide_nat_vent_gate() semantics, not the cycling-band formula."""
        ae = _make_automation_engine(is_paused_by_door=True)
        ae._natural_vent_active = False
        c = _make_classification(day_type="warm", hvac_mode="off")
        config = {"fan_mode": "whole_house_fan"}
        indoor = _curve([80.0, 80.0, 80.0, 80.0], start_hour=13)
        outdoor = _curve([85.0, 82.0, 70.0, 65.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action == "Natural ventilation"
        assert t == "3:00 PM"  # hour 15: outdoor 70 < indoor 80 - 1 = 79, indoor > comfort_heat, outdoor < 77

    def test_absent_when_nothing_open(self):
        """Nat-vent cannot start with everything closed — no candidate, even if the
        temperature gate alone would pass."""
        ae = _make_automation_engine(is_paused_by_door=False)
        ae._natural_vent_active = False
        c = _make_classification(day_type="warm", hvac_mode="off")
        config = {"fan_mode": "whole_house_fan"}
        indoor = _curve([80.0, 80.0, 80.0, 80.0], start_hour=13)
        outdoor = _curve([85.0, 82.0, 70.0, 65.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, _t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action != "Natural ventilation"

    def test_absent_when_fan_disabled(self):
        ae = _make_automation_engine(is_paused_by_door=True)
        ae._natural_vent_active = False
        c = _make_classification(day_type="warm", hvac_mode="off")
        config = {"fan_mode": "disabled"}
        indoor = _curve([80.0, 80.0, 80.0, 80.0], start_hour=13)
        outdoor = _curve([85.0, 82.0, 70.0, 65.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, _t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action != "Natural ventilation"

    def test_uses_real_gate_ceiling_margin_not_naive_midpoint(self):
        """Proves the candidate is sourced from decide_nat_vent_gate() — which has a
        fan-mode/aggressive-savings-aware ceiling check — not the cycling-band midpoint
        formula, which has no such concept at all. With fan_mode=hvac_fan and
        aggressive_savings=True, the real gate's ceiling_threshold is comfort_cool(74) + 2 = 76;
        indoor held at 80 (above that ceiling) must block the candidate even though the
        temperature-only condition (outdoor dropping well below indoor) is satisfied —
        a naive midpoint-only formula would have no ceiling check to block on.
        """
        ae = _make_automation_engine(is_paused_by_door=True)
        ae._natural_vent_active = False
        c = _make_classification(day_type="warm", hvac_mode="off")
        config = {"fan_mode": "hvac_fan", "aggressive_savings": True}
        indoor = _curve([80.0, 80.0, 80.0, 80.0], start_hour=13)
        outdoor = _curve([85.0, 82.0, 70.0, 65.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, _t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action != "Natural ventilation"

    def test_absent_when_already_active(self):
        ae = _make_automation_engine(is_paused_by_door=True)
        ae._natural_vent_active = True
        c = _make_classification(day_type="warm", hvac_mode="off")
        config = {"fan_mode": "whole_house_fan"}
        indoor = _curve([80.0, 80.0, 80.0, 80.0], start_hour=13)
        outdoor = _curve([85.0, 82.0, 70.0, 65.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, _t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action != "Natural ventilation"


class TestWarmDayForecastEventCandidates:
    """WARM/MILD-day forecast-derived events surfaced as Next Automation candidates (Issue #528)."""

    def test_ceiling_breach_candidate_present(self):
        ae = _make_automation_engine()
        ae._natural_vent_active = False
        c = _make_classification(day_type="warm", hvac_mode="off", windows_recommended=True)
        config = {"comfort_cool": 75.0}
        indoor = _curve([70.0, 72.0, 76.0, 78.0], start_hour=13)
        outdoor = _curve([65.0, 66.0, 67.0, 68.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action == "AC turns on to hold the ceiling"
        assert t == "3:00 PM"  # hour 15: indoor 76 > comfort_cool 75

    def test_close_windows_candidate_present(self):
        ae = _make_automation_engine()
        ae._natural_vent_active = False
        c = _make_classification(day_type="warm", hvac_mode="off", windows_recommended=True)
        config = {"comfort_cool": 100.0}  # keep ceiling breach out of the way
        indoor = _curve([70.0, 71.0, 72.0, 73.0], start_hour=13)
        outdoor = _curve([60.0, 62.0, 71.5, 74.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action == "Outdoor will stop helping around 3:00 PM — close windows"
        assert t == "3:00 PM"  # hour 15: outdoor 71.5 >= indoor 72 - 1

    def test_absent_when_windows_not_recommended(self):
        ae = _make_automation_engine()
        ae._natural_vent_active = False
        c = _make_classification(day_type="mild", hvac_mode="off", windows_recommended=False)
        config = {"comfort_cool": 75.0}
        indoor = _curve([70.0, 72.0, 76.0, 78.0], start_hour=13)
        outdoor = _curve([65.0, 66.0, 67.0, 68.0], start_hour=13, ts_key="datetime", temp_key="temperature")
        action, _t = _compute_next_automation_action_with_forecast(c, ae, config, time(12, 0), indoor, outdoor)
        assert action != "AC turns on to hold the ceiling"


# ---------------------------------------------------------------------------
# Tests: Sensor name rename
# ---------------------------------------------------------------------------


class TestNextActionSensorRename:
    """Verify sensor names via source inspection.

    Sensor classes cannot be instantiated without a real HA runtime (metaclass
    conflict from MagicMock stubs), so we verify the source code directly.
    """

    @pytest.fixture(autouse=True)
    def _read_sensor_source(self):
        """Read sensor.py source once for all tests in this class."""
        import pathlib

        sensor_path = (
            pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "sensor.py"
        )
        self.source = sensor_path.read_text()

    def test_next_action_sensor_name_is_your_next_action(self):
        """Sensor display name should be 'Your Next Action'."""
        assert '"Your Next Action"' in self.source

    def test_new_automation_action_sensor_name(self):
        """Next Automation Action sensor class exists with correct name."""
        assert "ClimateAdvisorNextAutomationSensor" in self.source
        assert '"Next Automation Action"' in self.source

    def test_new_automation_time_sensor_name(self):
        """Next Automation Time sensor class exists with correct name."""
        assert "ClimateAdvisorNextAutomationTimeSensor" in self.source
        assert '"Next Automation Time"' in self.source


# ---------------------------------------------------------------------------
# Tests: New constants exist
# ---------------------------------------------------------------------------


class TestNewConstants:
    """Verify the new attribute constants were added to const.py."""

    def test_attr_next_automation_action_constant(self):
        """ATTR_NEXT_AUTOMATION_ACTION should equal 'next_automation_action'."""
        assert ATTR_NEXT_AUTOMATION_ACTION == "next_automation_action"

    def test_attr_next_automation_time_constant(self):
        """ATTR_NEXT_AUTOMATION_TIME should equal 'next_automation_time'."""
        assert ATTR_NEXT_AUTOMATION_TIME == "next_automation_time"


# ---------------------------------------------------------------------------
# Phase 5G: Compliance sensor thermal attributes
# ---------------------------------------------------------------------------


def _compliance_sensor_extra_state_attributes_with_thermal(coordinator):
    """Return the real ClimateAdvisorComplianceSensor.extra_state_attributes for `coordinator`.

    coordinator is already a real ClimateAdvisorCoordinator instance (constructed
    via _make_coordinator_with_learning), so the sensor can be instantiated directly —
    no replication needed.
    """
    from unittest.mock import MagicMock

    from custom_components.climate_advisor.sensor import ClimateAdvisorComplianceSensor

    entry = MagicMock()
    entry.entry_id = "test_entry"
    sensor = ClimateAdvisorComplianceSensor(coordinator, entry)
    return sensor.extra_state_attributes


def _make_coordinator_with_learning(tmp_path):
    """Build a minimal coordinator with a real LearningEngine for thermal attribute tests."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
    from custom_components.climate_advisor.learning import LearningEngine

    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.states.get = MagicMock(return_value=None)

    config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "notify_service": "notify.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "temp_unit": "fahrenheit",
    }

    coordinator = ClimateAdvisorCoordinator(hass, config)
    coordinator.data = {}
    coordinator.learning = LearningEngine(Path(tmp_path))
    coordinator.learning.load_state()
    coordinator.automation_engine = MagicMock()
    return coordinator


def _make_thermal_obs(mode: str = "heat", rate: float = 2.0) -> dict:
    """Build a v2 ThermalObservation dict. rate is used as k_active."""
    return {
        "event_id": "test-status-obs",
        "timestamp": "2026-03-27T10:00:00",
        "date": "2026-03-27",
        "hvac_mode": mode,
        "session_minutes": 8.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 68.0,
        "peak_indoor_f": 68.0,
        "start_outdoor_f": 40.0,
        "avg_outdoor_f": 40.0,
        "delta_t_avg": 26.0,
        "k_passive": -0.05,
        "k_active": rate,  # used as k_active for legacy heating_rate_f_per_hour compat
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.75,
        "r_squared_active": 0.72,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": "low",
        "schema_version": 2,
    }


def _inject_thermal_obs(learning, obs: dict) -> None:
    """Inject a v2 observation via record_thermal_observation with dt_util patched."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.date.return_value = date(2026, 3, 27)
    mock_dt.now.return_value.isoformat.return_value = "2026-03-27T12:00:00"
    with patch("custom_components.climate_advisor.learning.dt_util", mock_dt):
        learning.record_thermal_observation(obs)


def _make_bias_record(i: int, forecast_high: float, observed_high: float) -> dict:
    return {
        "date": f"2026-03-{i + 1:02d}",
        "day_type": "mild",
        "trend_direction": "stable",
        "forecast_high_f": forecast_high,
        "observed_high_f": observed_high,
        "forecast_low_f": 50.0,
        "observed_low_f": 51.0,
    }


class TestComplianceSensorThermalAttributes:
    """Tests for compliance sensor thermal attribute helper."""

    def test_thermal_attributes_present_when_model_has_data(self, tmp_path):
        """Inject observations → attrs have non-None rates."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 2.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_HEATING_RATE

        assert attrs[ATTR_THERMAL_HEATING_RATE] is not None

    def test_thermal_attributes_none_when_no_observations(self, tmp_path):
        """Empty learning engine → rates are None, confidence is 'none'."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import (
            ATTR_THERMAL_CONFIDENCE,
            ATTR_THERMAL_COOLING_RATE,
            ATTR_THERMAL_HEATING_RATE,
        )

        assert attrs[ATTR_THERMAL_HEATING_RATE] is None
        assert attrs[ATTR_THERMAL_COOLING_RATE] is None
        assert attrs[ATTR_THERMAL_CONFIDENCE] == "none"

    def test_thermal_confidence_exposed(self, tmp_path):
        """Inject 5 observations → confidence == 'low'."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 2.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_CONFIDENCE

        assert attrs[ATTR_THERMAL_CONFIDENCE] == "low"

    def test_thermal_rate_converted_to_celsius_when_unit_is_celsius(self, tmp_path):
        """With temp_unit='celsius', rate is scaled by 5/9."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        coordinator.config["temp_unit"] = "celsius"
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 9.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_HEATING_RATE

        # 9°F/hr × 5/9 = 5.0°C/hr
        assert attrs[ATTR_THERMAL_HEATING_RATE] == pytest.approx(5.0, abs=0.01)

    def test_thermal_rate_unchanged_when_unit_is_fahrenheit(self, tmp_path):
        """With temp_unit='fahrenheit', rate is not scaled."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        coordinator.config["temp_unit"] = "fahrenheit"
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 3.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_HEATING_RATE

        assert attrs[ATTR_THERMAL_HEATING_RATE] == pytest.approx(3.0, abs=0.01)

    def test_forecast_bias_converted_to_celsius_when_unit_is_celsius(self, tmp_path):
        """With celsius unit, forecast bias is scaled."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        coordinator.config["temp_unit"] = "celsius"
        # Add 7 records with 9°F high bias → 5°C after conversion
        for i in range(7):
            coordinator.learning._state.records.append(_make_bias_record(i, 70.0, 79.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_FORECAST_HIGH_BIAS

        # 9°F × 5/9 = 5.0°C
        assert attrs[ATTR_FORECAST_HIGH_BIAS] == pytest.approx(5.0, abs=0.01)

    def test_forecast_bias_zero_when_no_observations(self, tmp_path):
        """No records → bias attrs are 0.0, confidence is 'none'."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import (
            ATTR_FORECAST_BIAS_CONFIDENCE,
            ATTR_FORECAST_HIGH_BIAS,
            ATTR_FORECAST_LOW_BIAS,
        )

        assert attrs[ATTR_FORECAST_HIGH_BIAS] == pytest.approx(0.0)
        assert attrs[ATTR_FORECAST_LOW_BIAS] == pytest.approx(0.0)
        assert attrs[ATTR_FORECAST_BIAS_CONFIDENCE] == "none"
