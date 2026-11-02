"""Pure predicates for the two mirror-image grace/override watchdogs (Issue #639,
Block 5 Phase 3).

Issue #508/#530 (orphaned grace) and Issue #321 (stuck override) are opposite shapes of
the same underlying inconsistency — something cleared one of {override, grace} without
going through the single choke point (``cancel_override()``) that clears both together:

  Orphaned grace (#508):  grace_active=True  but no override flag is set to justify it.
  Stuck override (#321):  an override flag is set but grace_active=False and its
                           grace_end_time has already passed (the timer callback never
                           fired, or was lost across an HA restart).

Colocated in one module, not split across two, because they must be proven mutually
exclusive for every reachable input tuple — a unit test in ``test_override_orphaned_grace.py``
asserts this directly. ``decide_orphaned_grace()`` IS ``coordinator._check_orphaned_grace()``'s condition (the
watchdog calls it, Issue #1216; before that it was a copy that had drifted). ``decide_stuck_override()`` mirrors the
Issue #321 stuck-grace check.
"""

from __future__ import annotations

from datetime import datetime


def decide_orphaned_grace(
    *,
    grace_active: bool,
    grace_protects_override: bool,
    manual_override_active: bool,
    fan_override_active: bool,
    override_confirm_pending: bool,
) -> bool:
    """``coordinator._check_orphaned_grace()``'s condition (Issues #508/#530/#1216).

    Field-by-field correspondence:
      grace_active             -> self.automation_engine._grace_active
      grace_protects_override   -> self.automation_engine._grace_protects_override
      manual_override_active    -> self.automation_engine._manual_override_active
      fan_override_active       -> self.automation_engine._fan_override_active
      override_confirm_pending  -> self.automation_engine._override_confirm_pending

    True iff a grace period started for a real override is still running with no override
    flag left to justify it — self-heals by force-cancelling grace. Scoped via
    ``grace_protects_override`` (Issue #530) so fan-off/window-close/dashboard-resume
    grace (never override-protecting) is never misread as orphaned. Issue #1216: a change still awaiting
    confirmation is what the grace protects -- coordinator Fix D keeps the replaced override's grace running for the
    occupant's second change, PENDING for the whole confirmation window; reaping it dropped that change (Rule 2).
    """
    return (
        grace_active
        and grace_protects_override
        and not manual_override_active
        and not fan_override_active
        and not override_confirm_pending
    )


def decide_stuck_override(
    *,
    manual_override_active: bool,
    grace_active: bool,
    grace_end_time: datetime | None,
    now: datetime,
) -> bool:
    """Pure reimplementation of the Issue #321 stuck-grace check
    (coordinator.py ~2488-2496).

    Field-by-field correspondence:
      manual_override_active -> self.automation_engine._manual_override_active
      grace_active             -> self.automation_engine._grace_active
      grace_end_time            -> dt_util.parse_datetime(self.automation_engine._grace_end_time)
      now                       -> dt_util.now()

    True iff a thermostat-mode override is active, its grace timer is NOT running, and its
    recorded end time is already in the past — the timer callback never fired (or was lost
    across an HA restart) and automation is stuck deferring to a "grace period" that no
    longer exists. Self-heals by force-clearing the override.

    Mutually exclusive with ``decide_orphaned_grace()`` by construction: this requires
    ``grace_active=False``; that requires ``grace_active=True``. No input tuple can satisfy
    both.
    """
    if not manual_override_active or grace_active or grace_end_time is None:
        return False
    return now > grace_end_time
