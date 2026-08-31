"""Pure decision core for the nat-vent reactivation lockout (architecture-reset Step 2).

Guards against rapid re-activation flapping right after an exit that arms
`_nat_vent_outdoor_exit_time` (originally only the outdoor-warm-rise exit; as
of Issue #641/#696/#755/#739 also the proactive-floor, ceiling-threshold,
comfort-floor (both its `_exit_nat_vent()`-routed fast-path twin and, as of
#739, `check_natural_vent_conditions()`'s own direct-`_deactivate_fan()`
branch), and fan_thermostat_check()'s STOP_COOLED_TO_FLOOR exits — see
`_exit_nat_vent()` call sites in `automation.py` for the current, authoritative
list, since it has grown more than once and this docstring should not be
trusted as the enumeration).

Issue #739: `check_natural_vent_conditions()`'s own `COMFORT_FLOOR` branch was
the last unarmed comfort-floor exit — it bypasses `_exit_nat_vent()` entirely
(direct `_deactivate_fan()` call, per that branch's own Issue #620 note), so it
never set this field even after #696/#755 fixed the two `_exit_nat_vent()`-
routed comfort-floor twins on the same "self-complementary at a fixed reading"
reasoning. Confirmed live on 2026-08-22: this exit fired with a monitored
window still open and HVAC already idle, and the WHF reactivated within a
minute on a ~1F uptick, immediately re-breaching the floor it had just exited
to protect. Fixed by setting `_nat_vent_outdoor_exit_time` directly at that
call site, the same pattern the `AWAY_CEILING` branch in the same method
already used for the identical reason.

As of Issue #696, this is consulted at TWO call sites in
`check_natural_vent_conditions()`: the paused-by-door reactivation block, and
the idle-open/"Priority-0 grace+ceiling re-entry" block (both its FSM and
legacy branches). It was previously scoped to the paused-by-door block ONLY,
on the claim that the idle-open block is "structurally unreachable... guarded
by `not self._paused_by_door`, already False at that moment." That claim was
wrong: the idle-open block's actual guard is `_actively_paused = paused_by_door
and not paused_with_hvac_already_off` (Issue #523), which is deliberately
False — making the block reachable — whenever `_paused_by_door=True` AND
`_paused_with_hvac_already_off=True`. A `COMFORT_FLOOR` exit with the
monitored sensor still open produces exactly that combination, and did so in
production on 2026-08-23 (WHF reactivated ~5 minutes after a comfort-floor
exit, unblocked, because the idle-open path never checked this lockout). See
Issue #696 for the full incident.

The other 2 reactivation-gate call sites (`handle_door_window_open`,
`_re_pause_for_open_sensor`) were individually re-read (not assumed) during
the #696 fix and their exemptions do hold, each for its own distinct reason —
`handle_door_window_open` genuinely guards on plain `not self._paused_by_door`;
`_re_pause_for_open_sensor` only fires after a grace-period expiry, a
different branch of the exit lifecycle than the pause branch this lockout
protects.

`reconcile_fan_on_startup` was ALSO believed exempt at that time, on the claim
that it "runs at most once per restart/30-min backstop, structurally
incapable of sub-minute repeats." Issue #790 found that claim false: 2 of its
4 real trigger sites (`thermostat_state_change`, `post_grace_expiry`) are
event-driven, not cadence-bound, and can fire sub-minute with no debounce on
the adopt path. It bypassed this lockout on BOTH sides — the check side
(hardcoded `paused_by_door=False` into the FSM inputs it feeds this lockout
check through) and the arm side (its own turn-off branch's `_exit_nat_vent()`
call never armed `_nat_vent_outdoor_exit_time`). Fixed by passing
`self._paused_by_door`'s real value on the check side and
`set_outdoor_exit_time=True` on the arm side — safe uniformly across all 4
triggers because `_nat_vent_outdoor_exit_time` is never persisted across HA
restarts (see `state.py`), so this lockout can only fire when a real exit was
armed earlier in the same running process; there is no restart-staleness
hazard that would justify treating the 2 cadence-bound triggers differently
from the 2 event-driven ones. Re-verify each individually before trusting
this summary if any of their guards change.
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
