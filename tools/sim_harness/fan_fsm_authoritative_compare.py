"""fan_fsm_authoritative_compare — full-authority equivalence proof (Issue #731),
fan/WHF lifecycle.

Same shape and purpose as ``doorwindow_fsm_authoritative_compare.py``/
``nat_vent_fsm_authoritative_compare.py``/``override_grace_fsm_authoritative_
compare.py`` — proves that flipping ``AutomationEngine._fan_fsm_authoritative``
from its default ``False`` to ``True`` (with the other three ``*_fsm_
authoritative`` flags left at their scenario-default ``False``) produces a
byte-for-byte identical ``event_log``/``action_log`` across the full golden +
pending scenario corpus.

Single-switch only — the combined-comparator's own docstring (Issue #731 Phase
7) documents 3 scenarios that newly diverge once the fan flag joins the other
three together; those are a compound-interaction diagnosis out of scope here.
This module isolates the fan flag exactly the way the other three single-switch
comparators isolate theirs, so a divergence found here is attributable to the
fan/WHF FSM's own dispatch/derivation alone, not an interaction with another FSM.

Use via ``tools.sim_harness.differential.diff_runs(scenario,
mutate_b=fsm_authoritative_mutation)``. Run A is untouched production (flag
default False), run B forces ``_fan_fsm_authoritative`` True on every engine
instance constructed during that run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def fsm_authoritative_mutation() -> Iterator[None]:
    """Force ``_fan_fsm_authoritative = True`` on every engine constructed
    while this context is active — the "run B" side of a diff_runs comparison.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.automation import AutomationEngine

    original_init = AutomationEngine.__init__

    def _wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._fan_fsm_authoritative = True

    with patch.object(AutomationEngine, "__init__", _wrapped_init):
        yield
