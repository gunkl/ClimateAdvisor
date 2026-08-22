"""Pure decision core for the fan toggle rapid-cycling backstop (Issue #641/#649,
extracted for Issue #731 Phase 1).

Pure reimplementation of ``_fan_toggle_rate_limited()``'s decision logic — the
hard safety backstop against rapid fan on/off/on cycling. Templated on
``nat_vent_gate.py``'s style (a simple pure-decision module): every field on
``FanToggleRateLimitInputs`` corresponds to a value the real method reads, and
this function returns only the decision — the shell (``automation.py``) still
owns logging and mutating ``self._fan_rate_limited_until`` /
``self._fan_rate_limited_direction``. This module intentionally contains no
logging: logging is a shell-only concern in every pure module in this codebase
(see ``fan_drift_reconciliation.py``'s and ``nat_vent_gate.py``'s docstrings for
the same convention).

Scope: this is a pure decision extraction only. ``_fan_toggle_rate_limited()``
itself is NOT changed to delegate to this module in this phase — that wiring is
a later phase in the Issue #731 plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class FanToggleRateLimitOutcome(Enum):
    """The three real outcomes _fan_toggle_rate_limited() can produce."""

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
    """Pure reimplementation of _fan_toggle_rate_limited()'s decision logic.

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

    applies_at = inputs.last_toggle_command_time + timedelta(seconds=inputs.min_interval_s)
    if inputs.rate_limited_until == applies_at and inputs.rate_limited_direction == inputs.action:
        return FanToggleRateLimitOutcome.DEFER_DUPLICATE, applies_at

    return FanToggleRateLimitOutcome.DEFER_NEW, applies_at
