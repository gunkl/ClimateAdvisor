"""Pure decision core for the nat-vent reactivation lockout (architecture-reset Step 2).

Guards against rapid re-activation flapping right after an outdoor-warm exit:
once nat-vent exits because outdoor rose above indoor (the ONLY exit reason
that records `_nat_vent_outdoor_exit_time`, per `_exit_nat_vent()`'s
docstring), the paused-by-door reactivation check inside
`check_natural_vent_conditions()` must wait out a configured lockout window
before considering reactivation again.

Verified narrowly and deliberately scoped to this ONE call site, not a gap to
spread to the other 4 reactivation-gate call sites (`handle_door_window_open`,
the Priority-0 grace+ceiling re-entry check, `reconcile_fan_on_startup`,
`_re_pause_for_open_sensor`): each of those is structurally unreachable in the
immediate aftermath of an outdoor-warm exit-with-pause, because they're each
guarded by a `not self._paused_by_door` (or equivalent) condition that is
already False at that moment — `_paused_by_door` and `_natural_vent_active`
being True is exactly what a pause/reactivation cycle means, and none of the
other 4 sites can even be entered while paused. Applying the lockout there
too would be dead code, not a fix.
"""

from __future__ import annotations

from datetime import datetime


def is_reactivation_locked_out(
    *,
    outdoor_exit_time: datetime | None,
    now: datetime,
    lockout_seconds: float,
) -> bool:
    """Pure reimplementation of the lockout check inside check_natural_vent_conditions().

    Returns True (locked out — do not reactivate yet) only when a prior
    outdoor-warm exit was recorded AND the configured lockout window hasn't
    elapsed since then. Returns False (no lockout in effect) when no such
    exit has happened yet, or the window has elapsed.
    """
    if outdoor_exit_time is None:
        return False
    elapsed = (now - outdoor_exit_time).total_seconds()
    return elapsed < lockout_seconds
