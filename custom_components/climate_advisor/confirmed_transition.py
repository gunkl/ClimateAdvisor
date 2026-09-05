"""Shared sustain-confirmation primitive (Issue #821, Design §0).

Generalizes ``is_reactivation_locked_out()`` (``nat_vent_reactivation_lockout.py``) one
level further: a candidate new state must read true continuously for a required sustain
duration before the shell commits the transition, rather than committing the instant a
single instantaneous reading crosses a threshold.

Root cause this closes (Issue #821 investigation, live 2026-08-31 13:06:46-13:30:00Z
data on Zone 1's real WHF): every nat-vent exit reason in ``nat_vent_exit.py`` commits
the instant its condition is momentarily true, evaluated per-tick against the current
instantaneous reading. The reactivation lockout that already exists
(``is_reactivation_locked_out()``) only guards re-entry AFTER an exit has already
committed — it does nothing to stop the exit itself from firing on a single noisy or
momentarily-crossing reading. The observed 300-second off/on gap on Zone 1's real WHF
matched ``NAT_VENT_REACTIVATION_LOCKOUT_S``'s configured value to the second — strong
evidence the lockout was delaying a flap, not preventing one.

Two independent consumers, ONE implementation (DRY, per the project owner's explicit
instruction): nat-vent's own 5 non-manual-override exit reasons in
``decide_nat_vent_exit()`` (wired into the shell in ``automation.py``, since the pure
decision module itself is stateless per-call and cannot own the "since when has this
been true" clock), and the comfort-family switch lockout (``automation.py``'s
``_resolve_comfort_family_via_fsm()``/family-switch state — this module's original
``_resolve_comfort_family_mode()`` reference was retired by the #827 FSM
consolidation).

Pure, stateless per call, like every other leaf module in this codebase. The caller
owns the actual state — the candidate value currently being timed, and the wall-clock
timestamp it first appeared — tracked via ``resolve_candidate_since()`` below (also
pure) so callers don't each reimplement the reset-on-change / hold-while-unchanged
bookkeeping, a second place to get that subtly wrong.

Deliberately NOT persisted across restarts — matches ``_nat_vent_outdoor_exit_time``'s
documented precedent (``nat_vent_reactivation_lockout.py``) exactly: a fresh process
has no flapping history worth guarding against.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def resolve_candidate_since(
    *,
    candidate: Any,
    previous_candidate: Any,
    previous_since: datetime | None,
    now: datetime,
) -> datetime | None:
    """Timestamp bookkeeping for a candidate value that may or may not be new.

    Returns ``now`` when ``candidate`` is not ``None`` and differs from
    ``previous_candidate`` (a fresh candidate just started reading true — restart the
    clock). Returns ``previous_since`` unchanged when ``candidate`` equals
    ``previous_candidate`` (still the same candidate — keep timing it, do not reset on
    every re-affirming tick). Returns ``None`` when ``candidate`` is ``None`` (nothing
    is currently a candidate — nothing to time; the caller should also clear its own
    ``previous_candidate`` state to ``None`` in this case so a later reappearance of
    the same value is correctly treated as fresh, not as a resumed timer).
    """
    if candidate is None:
        return None
    if candidate != previous_candidate:
        return now
    return previous_since


def is_confirmed(
    *,
    candidate: Any,
    candidate_since: datetime | None,
    now: datetime,
    sustain_seconds: float,
) -> bool:
    """True once ``candidate`` has been the standing candidate for >= ``sustain_seconds``.

    False when there is no candidate (``candidate is None``), or ``candidate_since`` is
    unset (the caller has not yet recorded when this candidate first appeared — treat
    as "just started", i.e. not yet confirmed), or the elapsed time is still short of
    ``sustain_seconds``. A ``sustain_seconds`` of ``0`` (or negative) confirms
    immediately as soon as a candidate and its timestamp exist — callers that want a
    transition to be fully exempt from sustain-confirmation (e.g.
    ``MANUAL_OVERRIDE_CONFLICT``) should bypass this function entirely rather than pass
    ``0``, so the exemption is visible at the call site.
    """
    if candidate is None or candidate_since is None:
        return False
    elapsed = (now - candidate_since).total_seconds()
    return elapsed >= sustain_seconds
