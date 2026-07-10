"""DesiredState — explicit temporal-intention schema (architecture-reset Step 3).

Per the plan's design correction: a pure `decide()` core can't just express
instantaneous HVAC/fan state — today's automation.py schedules 9 distinct
timer mechanisms (revisit, fan min-runtime cycling, override confirm, setpoint
accept-check, setpoint retry/nudge, grace period + restart restore, fan
thermostat backstop, post-fan-on/off setpoint verify) as scattered
`async_call_later` callbacks with their own cancel-flag bookkeeping. Under the
functional-core model, these become DATA the pure core returns — "please
re-evaluate at T", "please retry this setpoint at T", "please hold grace until
T", "please send this notification" — and the shell (still `automation.py`,
still scheduling real timers) becomes a uniform interpreter of that data
instead of 9 bespoke schedulers.

This module started as a DESIGN deliverable (Step 3), not a production
migration — every field traces to one of the 9 real mechanisms enumerated in
the Step-3 status report, nothing invented. All 9 now have their real
BRANCHING decision wired: grace (`decide_grace_start`), revisit-after-action
(`decide_revisit`), the fan thermostat backstop re-arm
(`decide_fan_thermo_backstop`), the override confirm disabled/pending branch
(`decide_override_confirm`), fan min-runtime cycling
(`decide_fan_cycle_on`/`decide_fan_cycle_off`), and — the last of the 9 — the
setpoint accept-check/retry/nudge/backoff family
(`decide_setpoint_retry_action`/`decide_scheduled_write_seq_current`). The
post-fan setpoint verify pair is wired too, via the sibling
`setpoint_verify_decision.py` module (same DATA-not-side-effect pattern, kept
separate since it isn't itself a temporal-schedule type). Scheduling the real
timer, cancelling prior ones, and emitting events always stay the shell's job
— only the branching DECISION moves into pure data.

One deliberate exception: `decide_setpoint_retry_schedule()` (constructing a
`PendingSetpointRetry` with a resolved `at: datetime`) is schema-validated by
`TestDecideSetpointRetrySchedule` but NOT called from automation.py's actual
scheduling. The real retry/nudge delays (900s / 30s) have never depended on
`dt_util.now()` — they are fixed constants, unlike grace/revisit which already
read the wall clock in the pre-refactor code. Deriving the delay via
`(at - now).total_seconds()` here would introduce a new `dt_util.now()` call
into a path that never had one, repeating the exact `decide_fan_cycle_on`/
`decide_fan_cycle_off` pitfall found earlier this session: `dt_util` is a bare,
unpatched `MagicMock` in the tests covering this exact retry/nudge path, so
mocked datetime arithmetic silently returns a `MagicMock`, not a real float,
breaking `async_call_later`'s delay argument. `PendingSetpointRetry` remains a
validated, correctly-shaped schema for this intention; the shell simply keeps
using its own literal delay constants rather than round-tripping them through
a datetime it doesn't otherwise need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


@dataclass(frozen=True)
class ScheduledReevaluation:
    """ "Please call me back at this time" — covers revisit, fan min-runtime
    cycling, and the fan thermostat backstop (self-rescheduling: the shell
    re-issues a new ScheduledReevaluation each time it handles one).

    Field-by-field correspondence:
      at     -> the real code's `async_call_later(hass, delay_seconds, cb)` delay,
                already resolved to an absolute time by the pure core (which has
                no wall-clock access) — the shell computes `delay = at - now()`.
      reason -> a stable tag identifying WHICH mechanism this is
                ("revisit" | "fan_min_runtime_on" | "fan_min_runtime_off" |
                "fan_min_runtime_retry" | "fan_thermo_backstop"), so the shell
                knows which handler to invoke when it fires — replaces today's
                9 separately-named callback closures with one data-driven dispatch.
    """

    at: datetime
    reason: str


@dataclass(frozen=True)
class PendingSetpointRetry:
    """ "Please verify/retry this setpoint" — covers the setpoint accept-check
    (10s), the nudge-then-retry escalation (30s), and the repeated-rejection
    backoff (900s). All three are really the same underlying intention at
    different delays/attempt counts, currently implemented as 3 separately
    scheduled closures sharing `_write_seq`/`_setpoint_reject_streak` state.

      at              -> resolved absolute retry time
      temperature      -> the setpoint to (re)assert
      mode             -> the hvac_mode to (re)assert it under
      write_seq        -> replaces `self._write_seq` — the shell still needs to
                           know "is this retry stale" (a newer command superseded
                           it), but that comparison becomes a data check against
                           the DesiredState's own sequence number, not an
                           instance-attribute a closure captured at schedule time.
      reject_streak    -> replaces `self._setpoint_reject_streak` — how many
                           consecutive rejections have occurred, so the shell can
                           decide nudge-vs-immediate-retry-vs-give-up without its
                           own separate counter.
    """

    at: datetime
    temperature: float
    mode: str
    write_seq: int
    reject_streak: int


@dataclass(frozen=True)
class GraceUntil:
    """ "Please hold grace until this time, then re-check." Covers both grace
    starting fresh (_start_grace_period) and restoring after an HA restart
    (_reschedule_grace_timer) — the restart case is just this same value
    reconstructed from persisted state, not a different mechanism.

      at             -> absolute grace-expiry time (the real code stores
                         self._grace_end_time as an ISO string for the SAME
                         purpose — this is that value, typed as datetime)
      source         -> "manual" | "automation" — which grace budget/notify
                         config applies (mirrors _start_grace_period's own arg)
      should_notify  -> whether expiry should send a notification (the real
                         code reads this from config at grace-START time and
                         must remember it until expiry — currently an inner
                         closure captures it; here it's plain data)
    """

    at: datetime
    source: str
    should_notify: bool


def decide_grace_start(
    *,
    source: str,
    manual_duration_seconds: float,
    manual_should_notify: bool,
    automation_duration_seconds: float,
    automation_should_notify: bool,
    now: datetime,
) -> GraceUntil | None:
    """Pure reimplementation of _start_grace_period()'s duration/should_notify resolution.

    Architecture-reset Step 2 (session state machine slice) — the first real
    wiring of a DesiredState type into production, not just a paper design.
    Returns None when the resolved duration is <= 0 (grace disabled for that
    source), mirroring the real method's early return in that case. The shell
    (``_start_grace_period()``) still owns all the side effects this decision
    doesn't cover: cancelling prior timers, scheduling the real
    ``async_call_later``, setting instance flags, and emitting the
    ``grace_started`` event.
    """
    if source == "manual":
        duration = manual_duration_seconds
        should_notify = manual_should_notify
    else:
        duration = automation_duration_seconds
        should_notify = automation_should_notify

    if duration <= 0:
        return None

    return GraceUntil(at=now + timedelta(seconds=duration), source=source, should_notify=should_notify)


@dataclass(frozen=True)
class OverrideConfirmPending:
    """ "Please confirm/reject this detected override at this time." Covers
    _override_confirm_cancel — the window during which a detected thermostat
    change might be transient (reverted) or a real user override.

      at     -> absolute confirm-expiry time
      mode   -> the mode that was detected, to compare against at expiry
    """

    at: datetime
    mode: str


@dataclass(frozen=True)
class SetpointVerify:
    """ "Please re-assert this setpoint at this time." Covers the two 30s
    post-fan-on/off verify timers — Ecobee-class thermostats may revert to
    their own comfort program right after a fan command, so CA re-asserts its
    setpoint shortly after to make sure the correction sticks.

      at            -> absolute verify time
      expected_temp -> the setpoint that should be in effect
      expected_mode -> the hvac_mode that should be in effect
      write_seq     -> same staleness-check purpose as PendingSetpointRetry.write_seq
    """

    at: datetime
    expected_temp: float | None
    expected_mode: str | None
    write_seq: int


@dataclass(frozen=True)
class NotificationRequest:
    """ "Please send this notification." Every _notify() call already IS this
    shape at the call site (message, title, notification_type) — this makes
    that shape a first-class part of DesiredState instead of a side effect
    fired inline mid-decision, so a pure core could express "this decision
    also wants to tell the occupant X" without calling hass.services directly.
    """

    message: str
    title: str
    notification_type: str


@dataclass(frozen=True)
class DesiredState:
    """The full intended state a decision produces — instantaneous AND temporal.

    Instantaneous fields mirror what today's action_log already records
    (climate.set_temperature / set_hvac_mode / fan turn_on/off calls).
    Temporal fields mirror the 9 real timer mechanisms enumerated in the
    Step-3 status report — every one of the dataclasses above traces to a
    specific existing `async_call_later` call site, not an invented concept.

    A None temporal field means "no change to that pending intention" (e.g.
    a decision that doesn't touch grace leaves grace_until unset, rather than
    the shell having to guess whether to cancel an existing timer). The shell
    reconciles DesiredState against actual pending timers the same way it
    already reconciles instantaneous state against the actual thermostat.
    """

    hvac_mode: str | None = None
    setpoint: float | None = None
    setpoint_low: float | None = None
    setpoint_high: float | None = None
    fan_command: str | None = None  # "on" | "off" | None (no change)

    scheduled_reevaluations: tuple[ScheduledReevaluation, ...] = field(default_factory=tuple)
    pending_setpoint_retry: PendingSetpointRetry | None = None
    grace_until: GraceUntil | None = None
    override_confirm_pending: OverrideConfirmPending | None = None
    setpoint_verify: SetpointVerify | None = None
    notifications: tuple[NotificationRequest, ...] = field(default_factory=tuple)


def decide_revisit(*, has_revisit_callback: bool, delay_seconds: float, now: datetime) -> ScheduledReevaluation | None:
    """Pure reimplementation of _schedule_revisit()'s decision: whether to schedule
    a follow-up re-evaluation after an HVAC action, and when.

    Returns None when no revisit callback is registered (mirrors the real
    method's `if not self._revisit_callback: return` early exit) — the shell
    still owns cancelling any prior timer and scheduling the real
    `async_call_later`.
    """
    if not has_revisit_callback:
        return None
    return ScheduledReevaluation(at=now + timedelta(seconds=delay_seconds), reason="revisit")


def decide_override_confirm(
    *, confirm_seconds: float, detected_mode: str, now: datetime
) -> OverrideConfirmPending | None:
    """Pure reimplementation of start_override_confirmation()'s disabled-vs-pending branch.

    Returns None when confirm_seconds <= 0 (confirmation disabled) — the shell
    interprets None as "accept the override immediately" (calls
    _confirm_override(detected_mode) right away), mirroring the real method's
    own early-return branch. Otherwise returns the pending window to schedule.
    """
    if confirm_seconds <= 0:
        return None
    return OverrideConfirmPending(at=now + timedelta(seconds=confirm_seconds), mode=detected_mode)


def decide_fan_thermo_backstop(
    *, fan_running: bool, delay_seconds: float, now: datetime
) -> ScheduledReevaluation | None:
    """Pure reimplementation of _thermo_backstop_task()'s re-arm decision: the
    backstop timer only reschedules itself while a CA-owned fan is still active.

    Returns None when the fan is no longer running (mirrors the real method's
    `if self._fan_running: self._start_fan_thermo_backstop()` — only re-arms in
    that case). The shell still owns scheduling the real `async_call_later` and
    invoking `fan_thermostat_check()`/`nat_vent_temperature_check()`.
    """
    if not fan_running:
        return None
    return ScheduledReevaluation(at=now + timedelta(seconds=delay_seconds), reason="fan_thermo_backstop")


_FAN_MODE_DISABLED = "disabled"  # mirrors const.FAN_MODE_DISABLED — duplicated locally per the
# same import-independence convention nat_vent_gate.py/fan_thermostat_decision.py already use.


class FanCycleOutcome(Enum):
    """The real outcomes _fan_cycle_on() can produce."""

    DISABLED = "disabled"  # feature disabled (min_runtime<=0 or fan_mode disabled) — no-op
    OVERRIDE_SUSPENDED = "override_suspended"  # user has manual control — cycle suspended
    ACTIVATE_ALWAYS_ON = "activate_always_on"  # min_runtime>=60 — activate, never schedule off
    ACTIVATE_WITH_OFF_TIMER = "activate_with_off_timer"  # activate, schedule off at min_runtime
    RETRY_LATER = "retry_later"  # fan already active for another reason — retry in 60min


def decide_fan_cycle_on(
    *,
    min_runtime_minutes: float,
    fan_mode: str,
    fan_override_active: bool,
    fan_active: bool,
) -> tuple[FanCycleOutcome, float | None]:
    """Pure reimplementation of _fan_cycle_on()'s decision (min-runtime cycling "on" phase).

    Returns (outcome, delay_seconds). delay_seconds is a raw duration (not an
    absolute time) — this mechanism only ever needs a relative re-check delay,
    never wall-clock alignment, so it deliberately does NOT take a `now`
    parameter (unlike decide_grace_start/decide_revisit/etc.) — avoiding a
    dependency on `dt_util.now()` that existing tests mock as a bare
    MagicMock in this specific code path (the original code never called
    dt_util.now() here either, just raw arithmetic on min_runtime).

    delay_seconds is None for DISABLED/OVERRIDE_SUSPENDED/ACTIVATE_ALWAYS_ON
    (nothing further to schedule); for ACTIVATE_WITH_OFF_TIMER it's the
    off-phase re-check delay; for RETRY_LATER it's the 60-minute retry delay.
    The shell owns actually calling _activate_fan() when the outcome starts
    with ACTIVATE, and converting delay_seconds into a real async_call_later.
    """
    if min_runtime_minutes <= 0 or fan_mode == _FAN_MODE_DISABLED:
        return FanCycleOutcome.DISABLED, None
    if fan_override_active:
        return FanCycleOutcome.OVERRIDE_SUSPENDED, None
    if not fan_active:
        if min_runtime_minutes >= 60:
            return FanCycleOutcome.ACTIVATE_ALWAYS_ON, None
        return FanCycleOutcome.ACTIVATE_WITH_OFF_TIMER, min_runtime_minutes * 60.0
    return FanCycleOutcome.RETRY_LATER, 60.0 * 60.0


def decide_fan_cycle_off(*, fan_min_runtime_active: bool, min_runtime_minutes: float) -> tuple[bool, float]:
    """Pure reimplementation of _fan_cycle_off()'s decision (min-runtime cycling "off" phase).

    Returns (should_deactivate, wait_seconds) — wait_seconds is a raw duration,
    same rationale as decide_fan_cycle_on (no `now` parameter needed).
    should_deactivate mirrors the real method's `if self._fan_min_runtime_active:`
    guard — the shell calls _deactivate_fan() only when True. wait_seconds (the
    "on" phase re-check delay) is ALWAYS returned, matching the real method's
    unconditional final `async_call_later` call.
    """
    wait_seconds = max(0.0, (60.0 - min_runtime_minutes) * 60.0)
    return fan_min_runtime_active, wait_seconds


def decide_setpoint_retry_schedule(
    *,
    temperature: float,
    mode: str,
    write_seq: int,
    reject_streak: int,
    delay_seconds: float,
    now: datetime,
) -> PendingSetpointRetry:
    """Pure construction of the PendingSetpointRetry intention that WOULD represent
    the 900s backoff (`_check_single_setpoint_accepted()`'s REASSERT outcome) or
    the 30s nudge-then-target follow-up (`_retry_callback()`'s NUDGE_THEN_TARGET
    branch) — the same triple (`_retry_seq`/`_retry_temp`/`_retry_mode` in the
    real code) those two real call sites capture before scheduling
    `async_call_later`.

    NOT called from production automation.py — see the module docstring's
    "One deliberate exception" note. Both real delays are literal constants that
    never depended on `dt_util.now()`; this function exists as validated schema
    (proving `PendingSetpointRetry` fits the real data) rather than as a
    production dependency, avoiding a `dt_util.now()`-mocking pitfall.

    Always returns a value (no None case): an unlimited-retry backoff is the
    current, unchanged behavior — this function does not add a give-up path.
    """
    return PendingSetpointRetry(
        at=now + timedelta(seconds=delay_seconds),
        temperature=temperature,
        mode=mode,
        write_seq=write_seq,
        reject_streak=reject_streak,
    )


class SetpointRetryAction(Enum):
    """The real outcomes _retry_callback() can produce."""

    SUPERSEDED = "superseded"  # a newer command replaced this one; skip entirely
    DIRECT_RETRY = "direct_retry"  # re-send the target setpoint immediately
    NUDGE_THEN_TARGET = "nudge_then_target"  # send a 1-degree nudge now, real target in 30s


def decide_setpoint_retry_action(
    *, current_write_seq: int, retry_write_seq: int, reject_streak: int
) -> SetpointRetryAction:
    """Pure reimplementation of _retry_callback()'s decision: is this retry
    stale, and if not, nudge-first or retry directly.

    Mirrors the real method's `if self._write_seq != _retry_seq: return`
    staleness guard, then `_do_nudge = self._setpoint_reject_streak >= 2`
    (Issue #411: some thermostat integrations dedup a repeated identical
    set_temperature payload, so retrying with the exact same value can never
    succeed — a brief nudge forces the device to recognize a real change
    before the actual target is sent 30s later). The original code precomputed
    `_do_nudge` once at verify time and captured it in the closure; here it is
    re-derived from `reject_streak` at the moment of firing instead, since
    `PendingSetpointRetry` already carries `reject_streak` as data — one fewer
    piece of captured closure state.
    """
    if current_write_seq != retry_write_seq:
        return SetpointRetryAction.SUPERSEDED
    if reject_streak >= 2:
        return SetpointRetryAction.NUDGE_THEN_TARGET
    return SetpointRetryAction.DIRECT_RETRY


def decide_scheduled_write_seq_current(*, current_write_seq: int, target_write_seq: int) -> bool:
    """Pure staleness check shared by every write_seq-tagged scheduled action.

    True means "no newer command has superseded this one, proceed" — used by
    `_send_real_target()`'s own `if self._write_seq != _retry_seq: return`
    guard (the second-stage check after a NUDGE_THEN_TARGET decision), the
    same staleness concept `decide_setpoint_retry_action` checks for the first
    stage, kept as a standalone one-line function since the send-real-target
    site has no nudge/reject_streak branching of its own to combine it with.
    """
    return current_write_seq == target_write_seq
