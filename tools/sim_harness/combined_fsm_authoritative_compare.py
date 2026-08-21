"""combined_fsm_authoritative_compare — all-three-switches-together equivalence
proof (Epic #594 Phase R, Phase G's G-test deliverable).

The three individual comparators (``nat_vent_fsm_authoritative_compare.py``,
``doorwindow_fsm_authoritative_compare.py``, ``override_grace_fsm_authoritative_
compare.py``) each flip exactly one switch in isolation. Nothing in the repo,
before this module, ever flipped all three together — confirmed absent by a
2026-08-20 audit that specifically searched for this and found none.

This matters because the combined rollout (Phase V) flips
``natvent_fsm_authoritative``, ``doorwindow_fsm_authoritative``, and
``override_grace_fsm_authoritative`` all at once, in a single deploy, rather
than as two sequential live-verification windows. Blocker F (a real race
between nat-vent's ``await self._activate_fan(...)`` yield point and an
unlocked, synchronously-invoked ``handle_fan_manual_override()``) only exists
once *both* ``natvent_fsm_authoritative`` and ``override_grace_fsm_
authoritative`` are on together — no single-switch comparator could ever have
found it, and none did; it was found by manual trace.

Use exactly like the single-switch comparators: ``tools.sim_harness.
differential.diff_runs(scenario, mutate_b=fsm_authoritative_mutation)``. Run A
is untouched production (all three flags default False), run B forces all
three True on every engine constructed during that run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def fsm_authoritative_mutation() -> Iterator[None]:
    """Force all three ``*_fsm_authoritative`` flags True together on every
    engine constructed while this context is active — the "run B" side of a
    combined diff_runs comparison.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.automation import AutomationEngine

    original_init = AutomationEngine.__init__

    def _wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._natvent_fsm_authoritative = True
        self._doorwindow_fsm_authoritative = True
        self._override_grace_fsm_authoritative = True

    with patch.object(AutomationEngine, "__init__", _wrapped_init):
        yield
