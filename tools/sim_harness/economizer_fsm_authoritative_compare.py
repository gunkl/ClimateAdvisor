"""economizer_fsm_authoritative_compare — strangler-fig completion program,
Phase 5 equivalence proof (Issue #746).

Proves that flipping ``AutomationEngine._economizer_fsm_authoritative`` from
its default ``False`` to ``True`` produces a byte-for-byte identical
``event_log``/``action_log`` across the full golden + pending scenario corpus.
This is a pure 1:1 translation (unlike nat-vent's Phase 2d fast-loop widening),
so — unlike ``nat_vent_fsm_authoritative_compare.py`` — this comparator is
expected to report clean for every scenario in the corpus with no allowlist.

Use via ``tools.sim_harness.differential.diff_runs(scenario,
mutate_b=fsm_authoritative_mutation)`` — run A is untouched production (flag
default False), run B forces the flag True on every engine instance
constructed during that run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def fsm_authoritative_mutation() -> Iterator[None]:
    """Force ``_economizer_fsm_authoritative = True`` on every engine
    constructed while this context is active — the "run B" side of a
    diff_runs comparison.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.automation import AutomationEngine

    original_init = AutomationEngine.__init__

    def _wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._economizer_fsm_authoritative = True

    with patch.object(AutomationEngine, "__init__", _wrapped_init):
        yield
