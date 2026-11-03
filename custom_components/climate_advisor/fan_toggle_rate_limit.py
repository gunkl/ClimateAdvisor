"""Pure decision core for the fan toggle rapid-cycling backstop (Issue #641/#649,
extracted for Issue #731 Phase 1).

Pure decision logic of the rate-limit backstop (formerly ``_fan_toggle_rate_limited()``, deleted in Issue #1212) — the
hard safety backstop against rapid fan on/off/on cycling. Templated on
``nat_vent_gate.py``'s style (a simple pure-decision module): every field on
``FanToggleRateLimitInputs`` corresponds to a value the real method reads, and
this function returns only the decision — the shell (``automation.py``) still
owns logging and mutating ``self._fan_rate_limited_until`` /
``self._fan_rate_limited_direction``. This module intentionally contains no
logging: logging is a shell-only concern in every pure module in this codebase
(see ``fan_drift_reconciliation.py``'s and ``nat_vent_gate.py``'s docstrings for
the same convention).

Scope: a pure decision extraction. ``automation.py`` reaches it through ``fan_fsm`` (``_resolve_fan_fsm_state``);
the record writes happen in ``_activate_fan``/``_deactivate_fan`` and the clears in ``_clear_fan_deferral_record``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from homeassistant.util import dt as dt_util

from .time_utils import instant_after

# Issue #1247: how long past its ``applies_at`` a recorded deferral still counts as pending. The deferred start/stop
# is retried by (a) short timers due at ``applies_at`` + 1 s and (b) the 30-minute coordinator cycle (the paused-
# reactivation retry in ``check_natural_vent_conditions()``), so a real retry lands at most one cycle after
# ``applies_at``; 300 s of scheduling slack (one floor) is added on top. The min-runtime cycle's refused-start retry
# (``_MIN_RUNTIME_REFUSED_RETRY_S``, 3600 s) lands >= 3300 s after ``applies_at`` and must NOT count as the deferred
# one, which is why the bound sits well below 3300 s.
FAN_DEFERRAL_STALE_AFTER_S = 2100.0


def deferral_record_is_stale(until: datetime | None, now: datetime) -> bool:
    """Whether a recorded deferral is so far past its ``applies_at`` that it no longer counts (Issue #1247).

    Occupant view: an hour-old record made a later, unrelated fan start log "5-minute floor expired -- applying
    deferred activation" long after the fact. False for a missing/non-datetime record and for any record still
    pending or within ``FAN_DEFERRAL_STALE_AFTER_S`` past its ``applies_at``.
    """
    if not isinstance(until, datetime):
        return False
    # as_utc reads a naive value in the HA timezone, like instant_after() (the record is UTC, ``now`` may be naive).
    return (dt_util.as_utc(now) - dt_util.as_utc(until)).total_seconds() > FAN_DEFERRAL_STALE_AFTER_S


class FanToggleRateLimitOutcome(Enum):
    """The three real outcomes the fan toggle rate-limit decision can produce."""

    ALLOW = "allow"
    DEFER_NEW = "defer_new"
    DEFER_DUPLICATE = "defer_duplicate"


@dataclass(frozen=True)
class FanToggleRateLimitInputs:
    """Every input the rate-limit check may read — explicit, nothing hidden.

    Field-by-field correspondence to the real code:
      last_toggle_command_time -> self._fan_toggle_command_time. None, or anything
                                   that isn't a real datetime (e.g. a MagicMock in
                                   tests that never patched a real clock), both mean
                                   "no prior command" — matches the isinstance guard
                                   in the real method's docstring.
      action                   -> the caller's own action=... argument ("activate" /
                                   "deactivate")
      rate_limited_until       -> self._fan_rate_limited_until
      rate_limited_direction   -> self._fan_rate_limited_direction
      min_interval_s           -> const.py FAN_MIN_TOGGLE_INTERVAL_S
      now                      -> caller-resolved wall-clock time (replaces the real
                                   method's internal dt_util.now() read, same
                                   convention as nat_vent_gate.py's in_sleep_window)
    """

    last_toggle_command_time: datetime | None
    action: str
    rate_limited_until: datetime | None
    rate_limited_direction: str | None
    min_interval_s: float
    now: datetime


def decide_fan_toggle_rate_limit(
    inputs: FanToggleRateLimitInputs,
) -> tuple[FanToggleRateLimitOutcome, datetime | None]:
    """The pure fan toggle rate-limit decision (the FSM path's rate-limit step).

    Returns (outcome, applies_at). ``applies_at`` is non-None only for
    DEFER_NEW/DEFER_DUPLICATE (the moment the deferral window ends); it is
    always None for ALLOW.

    Precedence (matches the real method exactly):
    1. No real prior command timestamp (None, or not a datetime) -> ALLOW.
    2. Elapsed time since the last toggle command already meets/exceeds
       min_interval_s -> ALLOW.
    3. Otherwise this toggle is deferred until applies_at = last_toggle_command_time
       + min_interval_s. If a deferral for that exact applies_at moment AND the
       same action is already on record -> DEFER_DUPLICATE (Issue #649: a silent
       duplicate of already-known information, not a new report). Otherwise
       -> DEFER_NEW.
    """
    if not isinstance(inputs.last_toggle_command_time, datetime):
        return FanToggleRateLimitOutcome.ALLOW, None

    elapsed = (inputs.now - inputs.last_toggle_command_time).total_seconds()
    if elapsed >= inputs.min_interval_s:
        return FanToggleRateLimitOutcome.ALLOW, None

    applies_at = instant_after(inputs.min_interval_s, now=inputs.last_toggle_command_time)
    # Issue #1247: a record well past its applies_at is not "already deferred" (the shell also clears it).
    if (
        inputs.rate_limited_until == applies_at
        and inputs.rate_limited_direction == inputs.action
        and not deferral_record_is_stale(inputs.rate_limited_until, inputs.now)
    ):
        return FanToggleRateLimitOutcome.DEFER_DUPLICATE, applies_at

    return FanToggleRateLimitOutcome.DEFER_NEW, applies_at
