"""Pure predicate for "does this grace trigger protect a real override?" (Issue #639,
Block 5 Phase 3).

Reimplements the one-line, previously-unextracted piece of ``_start_grace_period()``
(automation.py:4349, Issue #530): ``_grace_protects_override = trigger in
_GRACE_TRIGGERS_PROTECTING_OVERRIDE``. Trivial, but kept as its own module — one
predicate, one file, one test file — same granularity precedent as
``nat_vent_reactivation_lockout.py`` staying separate from ``nat_vent_gate.py``.

Companion piece ``desired_state.decide_grace_start()`` (existing, reused unchanged)
resolves duration/should_notify; this module resolves the one remaining branch
``decide_grace_start()`` does not cover.
"""

from __future__ import annotations

from .const import GRACE_TRIGGERS_PROTECTING_OVERRIDE

# Issue #664: previously a hand-duplicated frozenset literal here, independent of
# automation.py's own copy — override_grace_fsm.py's call site never passed
# `protecting_triggers=` explicitly, so it silently depended on this duplicate staying
# in sync by hand. Both automation.py and this module now import the same const.py
# definition; there is exactly one frozenset, not two kept manually consistent.


def decide_grace_protects_override(
    trigger: str,
    *,
    protecting_triggers: frozenset[str] = GRACE_TRIGGERS_PROTECTING_OVERRIDE,
) -> bool:
    """Pure reimplementation of _start_grace_period()'s protects-override determination."""
    return trigger in protecting_triggers
