"""Pure outcome classification for ``cancel_override()`` (Issue #639, Block 5 Phase 3).

Reimplements ``cancel_override()``'s (automation.py:978-1012, Issue #508) no-op guard
and the classification that drives its one conditional side effect: a fan-only override
being cancelled needs its own ``override_cleared`` emit, because
``clear_manual_override()`` only emits that event when a thermostat-mode override
(``_manual_override_active``) was active — a fan-only cancel would otherwise leave zero
trace in the Activity Report.
"""

from __future__ import annotations

from enum import Enum


class OverrideCancelOutcome(Enum):
    """What ``cancel_override()`` actually had to clear, if anything."""

    NOOP = "noop"
    HAD_MANUAL = "had_manual"
    HAD_FAN_ONLY = "had_fan_only"
    HAD_GRACE_ONLY = "had_grace_only"


def decide_override_cancel_outcome(
    *,
    manual_override_active: bool,
    fan_override_active: bool,
    grace_active: bool,
) -> OverrideCancelOutcome:
    """Pure reimplementation of ``cancel_override()``'s had_manual/had_fan/had_grace
    classification.

    Field-by-field correspondence:
      manual_override_active -> self._manual_override_active (snapshotted before clearing)
      fan_override_active     -> self._fan_override_active (snapshotted before clearing)
      grace_active             -> self._grace_active (snapshotted before clearing)

    NOOP: nothing was active — real method returns False, no-op.
    HAD_MANUAL: a thermostat-mode override was active — clear_manual_override() will emit
      its own "override_cleared" event; caller does not need a supplemental emit.
    HAD_FAN_ONLY: only the fan override was active (not manual) — caller must emit its own
      supplemental "override_cleared" event, since clear_manual_override() stays silent.
    HAD_GRACE_ONLY: neither override flag was active, only a bare grace period (e.g. a
      fan-off cooldown or window-close grace with no override behind it) — caller emits no
      supplemental event, same as HAD_MANUAL (no override was ever active to announce).
    """
    if not (manual_override_active or fan_override_active or grace_active):
        return OverrideCancelOutcome.NOOP
    if manual_override_active:
        return OverrideCancelOutcome.HAD_MANUAL
    if fan_override_active:
        return OverrideCancelOutcome.HAD_FAN_ONLY
    return OverrideCancelOutcome.HAD_GRACE_ONLY
