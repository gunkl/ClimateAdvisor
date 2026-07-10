"""Validation tests for the DesiredState schema (architecture-reset Step 3).

Most of these are NOT production integration tests — most of desired_state.py
isn't wired into automation.py (deliberately deferred, see the module
docstring). Each test constructs a DesiredState value from a REAL example
already present in automation.py/const.py (a real grace duration, a real
backstop interval, a real notification message) and asserts the schema
round-trips that information losslessly — proving the design fits actual
mechanisms, not just a plausible-looking shape.

The grace-period mechanism IS now wired for real: TestDecideGraceStart below
tests decide_grace_start() directly, and
tests/test_grace_period_desired_state_integration.py proves
_start_grace_period() actually calls it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.const import (
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    REVISIT_DELAY_SECONDS,
)
from custom_components.climate_advisor.desired_state import (
    DesiredState,
    FanCycleOutcome,
    GraceUntil,
    NotificationRequest,
    OverrideConfirmPending,
    PendingSetpointRetry,
    ScheduledReevaluation,
    SetpointRetryAction,
    SetpointVerify,
    decide_fan_cycle_off,
    decide_fan_cycle_on,
    decide_fan_thermo_backstop,
    decide_grace_start,
    decide_override_confirm,
    decide_revisit,
    decide_scheduled_write_seq_current,
    decide_setpoint_retry_action,
    decide_setpoint_retry_schedule,
)

_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def test_grace_until_represents_automation_grace_start():
    """Mirrors _start_grace_period("automation", ...): real duration
    (DEFAULT_AUTOMATION_GRACE_SECONDS=300s), source, and should_notify."""
    grace = GraceUntil(
        at=_NOW + timedelta(seconds=DEFAULT_AUTOMATION_GRACE_SECONDS),
        source="automation",
        should_notify=True,
    )
    assert grace.at == _NOW + timedelta(seconds=300)
    assert grace.source == "automation"
    assert grace.should_notify is True


def test_grace_until_represents_manual_grace_start():
    """Mirrors _start_grace_period("manual", ...): DEFAULT_MANUAL_GRACE_SECONDS."""
    grace = GraceUntil(
        at=_NOW + timedelta(seconds=DEFAULT_MANUAL_GRACE_SECONDS),
        source="manual",
        should_notify=True,
    )
    assert grace.source == "manual"
    assert (grace.at - _NOW).total_seconds() == DEFAULT_MANUAL_GRACE_SECONDS


def test_scheduled_reevaluation_represents_revisit_after_action():
    """Mirrors _schedule_revisit(): fires REVISIT_DELAY_SECONDS (300s) after any action."""
    revisit = ScheduledReevaluation(at=_NOW + timedelta(seconds=REVISIT_DELAY_SECONDS), reason="revisit")
    assert revisit.reason == "revisit"
    assert (revisit.at - _NOW).total_seconds() == 300


def test_scheduled_reevaluation_represents_fan_thermo_backstop():
    """Mirrors _start_fan_thermo_backstop(): self-rescheduling every 5 minutes
    while a CA-owned fan is active."""
    backstop = ScheduledReevaluation(at=_NOW + timedelta(minutes=5), reason="fan_thermo_backstop")
    assert backstop.reason == "fan_thermo_backstop"
    assert (backstop.at - _NOW).total_seconds() == 300


def test_pending_setpoint_retry_represents_nudge_then_real_target():
    """Mirrors the real 30s nudge-then-retry escalation in _set_temperature():
    a rejected setpoint gets a small nudge immediately, then the real target
    is retried 30s later, gated on write_seq matching (a newer command must
    supersede a stale retry) and reject_streak (how many consecutive
    rejections have occurred so far)."""
    retry = PendingSetpointRetry(
        at=_NOW + timedelta(seconds=30),
        temperature=72.0,
        mode="cool",
        write_seq=5,
        reject_streak=1,
    )
    assert retry.temperature == 72.0
    assert retry.write_seq == 5
    assert retry.reject_streak == 1


def test_override_confirm_pending_represents_detected_mode_change():
    """Mirrors handle_manual_override()'s confirm window: a detected mode
    change is held pending until confirm_seconds elapses, to distinguish a
    transient thermostat blip from a real user override."""
    pending = OverrideConfirmPending(at=_NOW + timedelta(seconds=120), mode="heat")
    assert pending.mode == "heat"


def test_setpoint_verify_represents_post_fan_on_reassert():
    """Mirrors _verify_setpoint_after_fan_on(): re-assert the setpoint 30s
    after a fan command, in case the thermostat reverted to its own program."""
    verify = SetpointVerify(at=_NOW + timedelta(seconds=30), expected_temp=75.0, expected_mode="cool", write_seq=3)
    assert verify.expected_temp == 75.0
    assert verify.write_seq == 3


def test_notification_request_represents_grace_repause_message():
    """Mirrors _re_pause_for_open_sensor()'s real notification text (verified
    against automation.py in the grace-expiry-repause-sensor-still-open golden)."""
    notice = NotificationRequest(
        message="Grace period expired but a door/window is still open. HVAC has been paused again.",
        title="Climate Advisor",
        notification_type="grace_repause",
    )
    assert "still open" in notice.message
    assert notice.notification_type == "grace_repause"


def test_desired_state_combines_instantaneous_and_temporal_fields():
    """A single decision can express BOTH an instantaneous action (deactivate
    the fan, matching automation.py's real _deactivate_fan side effects) AND
    multiple temporal intentions (start grace, request a notification) —
    the exact shape a converted nat-vent-exit decision would need to return."""
    state = DesiredState(
        hvac_mode="cool",
        fan_command="off",
        grace_until=GraceUntil(at=_NOW + timedelta(seconds=300), source="automation", should_notify=True),
        notifications=(
            NotificationRequest(
                message="Nat-vent ended — HVAC restored.",
                title="Climate Advisor",
                notification_type="nat_vent_exit",
            ),
        ),
    )
    assert state.hvac_mode == "cool"
    assert state.fan_command == "off"
    assert state.grace_until is not None
    assert len(state.notifications) == 1
    # Fields not touched by this decision stay unset, not guessed at.
    assert state.scheduled_reevaluations == ()
    assert state.pending_setpoint_retry is None


class TestDecideGraceStart:
    """decide_grace_start() — the first real production wiring of a DesiredState type.

    Mirrors _start_grace_period()'s exact source-based resolution and the
    duration<=0 (disabled) early return.
    """

    def test_manual_source_uses_manual_duration_and_notify(self):
        grace = decide_grace_start(
            source="manual",
            manual_duration_seconds=180,
            manual_should_notify=True,
            automation_duration_seconds=300,
            automation_should_notify=False,
            now=_NOW,
        )
        assert grace is not None
        assert grace.source == "manual"
        assert grace.at == _NOW + timedelta(seconds=180)
        assert grace.should_notify is True

    def test_automation_source_uses_automation_duration_and_notify(self):
        grace = decide_grace_start(
            source="automation",
            manual_duration_seconds=180,
            manual_should_notify=True,
            automation_duration_seconds=300,
            automation_should_notify=False,
            now=_NOW,
        )
        assert grace is not None
        assert grace.source == "automation"
        assert grace.at == _NOW + timedelta(seconds=300)
        assert grace.should_notify is False

    def test_zero_duration_disables_grace(self):
        """Mirrors _start_grace_period()'s `if duration <= 0: return` (grace disabled)."""
        grace = decide_grace_start(
            source="manual",
            manual_duration_seconds=0,
            manual_should_notify=True,
            automation_duration_seconds=300,
            automation_should_notify=True,
            now=_NOW,
        )
        assert grace is None

    def test_negative_duration_disables_grace(self):
        grace = decide_grace_start(
            source="automation",
            manual_duration_seconds=300,
            manual_should_notify=True,
            automation_duration_seconds=-5,
            automation_should_notify=True,
            now=_NOW,
        )
        assert grace is None


class TestDecideRevisit:
    def test_schedules_when_callback_registered(self):
        revisit = decide_revisit(has_revisit_callback=True, delay_seconds=REVISIT_DELAY_SECONDS, now=_NOW)
        assert revisit is not None
        assert revisit.reason == "revisit"
        assert revisit.at == _NOW + timedelta(seconds=REVISIT_DELAY_SECONDS)

    def test_none_when_no_callback_registered(self):
        """Mirrors _schedule_revisit()'s `if not self._revisit_callback: return`."""
        revisit = decide_revisit(has_revisit_callback=False, delay_seconds=REVISIT_DELAY_SECONDS, now=_NOW)
        assert revisit is None


class TestDecideFanThermoBackstop:
    def test_re_arms_when_fan_still_running(self):
        backstop = decide_fan_thermo_backstop(fan_running=True, delay_seconds=300, now=_NOW)
        assert backstop is not None
        assert backstop.reason == "fan_thermo_backstop"
        assert backstop.at == _NOW + timedelta(seconds=300)

    def test_none_when_fan_no_longer_running(self):
        """Mirrors _thermo_backstop_task()'s `if self._fan_running:` re-arm guard."""
        backstop = decide_fan_thermo_backstop(fan_running=False, delay_seconds=300, now=_NOW)
        assert backstop is None


class TestDecideOverrideConfirm:
    def test_schedules_pending_window_when_enabled(self):
        pending = decide_override_confirm(confirm_seconds=600, detected_mode="heat", now=_NOW)
        assert pending is not None
        assert pending.mode == "heat"
        assert pending.at == _NOW + timedelta(seconds=600)

    def test_none_when_confirmation_disabled(self):
        """Mirrors start_override_confirmation()'s `if confirm_seconds <= 0:` immediate-accept branch."""
        pending = decide_override_confirm(confirm_seconds=0, detected_mode="heat", now=_NOW)
        assert pending is None

    def test_none_when_confirm_seconds_negative(self):
        pending = decide_override_confirm(confirm_seconds=-1, detected_mode="cool", now=_NOW)
        assert pending is None


class TestDecideFanCycleOn:
    _BASE = {
        "min_runtime_minutes": 20.0,
        "fan_mode": "whole_house_fan",
        "fan_override_active": False,
        "fan_active": False,
    }

    def _inputs(self, **overrides):
        return {**self._BASE, **overrides}

    def test_disabled_when_min_runtime_zero(self):
        outcome, delay = decide_fan_cycle_on(**self._inputs(min_runtime_minutes=0))
        assert outcome == FanCycleOutcome.DISABLED
        assert delay is None

    def test_disabled_when_fan_mode_disabled(self):
        outcome, delay = decide_fan_cycle_on(**self._inputs(fan_mode="disabled"))
        assert outcome == FanCycleOutcome.DISABLED
        assert delay is None

    def test_override_suspended_when_user_has_control(self):
        outcome, delay = decide_fan_cycle_on(**self._inputs(fan_override_active=True))
        assert outcome == FanCycleOutcome.OVERRIDE_SUSPENDED
        assert delay is None

    def test_activate_always_on_when_min_runtime_at_least_60(self):
        outcome, delay = decide_fan_cycle_on(**self._inputs(min_runtime_minutes=60))
        assert outcome == FanCycleOutcome.ACTIVATE_ALWAYS_ON
        assert delay is None

    def test_activate_with_off_timer_when_min_runtime_under_60(self):
        outcome, delay = decide_fan_cycle_on(**self._inputs(min_runtime_minutes=20))
        assert outcome == FanCycleOutcome.ACTIVATE_WITH_OFF_TIMER
        assert delay == 20 * 60.0

    def test_retry_later_when_fan_already_active(self):
        outcome, delay = decide_fan_cycle_on(**self._inputs(fan_active=True))
        assert outcome == FanCycleOutcome.RETRY_LATER
        assert delay == 60 * 60.0


class TestDecideFanCycleOff:
    def test_deactivates_when_runtime_active(self):
        should_deactivate, wait_seconds = decide_fan_cycle_off(fan_min_runtime_active=True, min_runtime_minutes=20.0)
        assert should_deactivate is True
        assert wait_seconds == (60 - 20) * 60.0

    def test_no_deactivate_when_runtime_not_active(self):
        """Mirrors _fan_cycle_off()'s `if self._fan_min_runtime_active:` guard."""
        should_deactivate, wait_seconds = decide_fan_cycle_off(fan_min_runtime_active=False, min_runtime_minutes=20.0)
        assert should_deactivate is False
        assert wait_seconds > 0  # next "on" phase is always scheduled regardless

    def test_wait_seconds_clamped_to_zero_when_min_runtime_exceeds_60(self):
        """max(0, ...) guard: an (invalid but possible) min_runtime > 60 must not produce
        a negative wait."""
        _, wait_seconds = decide_fan_cycle_off(fan_min_runtime_active=True, min_runtime_minutes=90.0)
        assert wait_seconds == 0.0


class TestDecideSetpointRetrySchedule:
    """The last of the 9 DesiredState mechanisms (architecture-reset session)."""

    def test_backoff_schedule_captures_the_retry_triple(self):
        """Mirrors _check_single_setpoint_accepted()'s REASSERT branch capturing
        _retry_seq/_retry_temp/_retry_mode before async_call_later(hass, 900, ...)."""
        retry = decide_setpoint_retry_schedule(
            temperature=72.0, mode="cool", write_seq=5, reject_streak=1, delay_seconds=900, now=_NOW
        )
        assert retry == PendingSetpointRetry(
            at=_NOW + timedelta(seconds=900), temperature=72.0, mode="cool", write_seq=5, reject_streak=1
        )

    def test_nudge_followup_schedule_uses_30s_delay(self):
        """Mirrors _retry_callback()'s NUDGE_THEN_TARGET branch scheduling
        _send_real_target 30s out via async_call_later(hass, 30, ...)."""
        retry = decide_setpoint_retry_schedule(
            temperature=68.0, mode="heat", write_seq=7, reject_streak=2, delay_seconds=30, now=_NOW
        )
        assert retry.at == _NOW + timedelta(seconds=30)
        assert retry.reject_streak == 2


class TestDecideSetpointRetryAction:
    def test_superseded_when_write_seq_changed(self):
        """Mirrors _retry_callback()'s `if self._write_seq != _retry_seq: return`."""
        action = decide_setpoint_retry_action(current_write_seq=6, retry_write_seq=5, reject_streak=0)
        assert action == SetpointRetryAction.SUPERSEDED

    def test_direct_retry_when_reject_streak_below_two(self):
        """reject_streak=0 or 1 -> immediate retry, no nudge (Issue #411 threshold)."""
        action = decide_setpoint_retry_action(current_write_seq=5, retry_write_seq=5, reject_streak=1)
        assert action == SetpointRetryAction.DIRECT_RETRY

    def test_nudge_then_target_when_reject_streak_at_least_two(self):
        """reject_streak>=2 -> some thermostat integrations dedup an identical repeated
        set_temperature payload, so nudge first (Issue #411)."""
        action = decide_setpoint_retry_action(current_write_seq=5, retry_write_seq=5, reject_streak=2)
        assert action == SetpointRetryAction.NUDGE_THEN_TARGET

    def test_superseded_takes_priority_over_reject_streak(self):
        """Matches real code's if/return order: staleness is checked before nudge branching."""
        action = decide_setpoint_retry_action(current_write_seq=9, retry_write_seq=5, reject_streak=5)
        assert action == SetpointRetryAction.SUPERSEDED


class TestDecideScheduledWriteSeqCurrent:
    def test_true_when_write_seq_unchanged(self):
        """Mirrors _send_real_target()'s `if self._write_seq != _retry_seq: return` guard
        (the True/proceed side)."""
        assert decide_scheduled_write_seq_current(current_write_seq=5, target_write_seq=5) is True

    def test_false_when_a_newer_command_superseded_it(self):
        assert decide_scheduled_write_seq_current(current_write_seq=6, target_write_seq=5) is False
