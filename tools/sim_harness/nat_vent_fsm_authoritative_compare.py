"""nat_vent_fsm_authoritative_compare — Step 2/Step 3 equivalence proof (Issue #594
Phase R).

Unlike ``nat_vent_gate_compare.py`` (old hand-written code vs. a newly-extracted
pure function), this comparator proves something narrower and specific to the
cutover work: that flipping ``AutomationEngine._natvent_fsm_authoritative`` from
its default ``False`` to ``True`` produces a byte-for-byte identical
``event_log``/``action_log`` across the full golden + pending scenario corpus.

This is the decision-equivalence test the Phase R plan called for — prior
coverage (``test_shadow_engine_coverage.py``, ``test_shadow_engine_pair.py``)
only asserted *state-label* agreement (would the FSM's derived state match
production's flags), never that flipping the switch leaves REAL commanded
actions unchanged. Since ``nat_vent_fsm.transition()``'s active-state path
calls the identical ``decide_nat_vent_gate()``/``decide_nat_vent_exit()`` pure
functions ``check_natural_vent_conditions()`` already calls directly (see
``automation.py``'s ``_natvent_fsm_authoritative`` branch), this comparator
is a refactor-safety net proving that swap didn't change behavior — not a
discovery mechanism for new logic differences (there is no new logic; Step 1's
escalation modeling was itself validated by ``tests/test_nat_vent_fsm.py``
before this comparator exists).

Use via ``tools.sim_harness.differential.diff_runs(scenario,
mutate_b=fsm_authoritative_mutation)`` — run A is untouched production (flag
default False), run B forces the flag True on every engine instance
constructed during that run. ``diff.is_clean`` must be True for every
scenario in the golden/pending corpus, EXCEPT the individually-diagnosed
entries in ``tests/test_nat_vent_fsm_authoritative_compare.py``'s own
``_KNOWN_DIVERGENT_SCENARIOS`` dict (see its comments for each root cause).
Issue #698 (Phase 2d, Decision 1) intentionally widened the fast per-tick
exit check inside ``nat_vent_temperature_check()`` to the full 5-check exit
chain, so the swap is no longer a universal no-op the way Phases 2a-2c's pure
wiring changes were — this comparator remains a real regression net for
every scenario NOT in that known-divergence list.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def fsm_authoritative_mutation() -> Iterator[None]:
    """Force ``_natvent_fsm_authoritative = True`` on every engine constructed
    while this context is active — the "run B" side of a diff_runs comparison.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.automation import AutomationEngine

    original_init = AutomationEngine.__init__

    def _wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._natvent_fsm_authoritative = True

    with patch.object(AutomationEngine, "__init__", _wrapped_init):
        yield
