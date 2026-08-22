"""classification_fsm_authoritative_compare — full-authority equivalence proof
(Issue #742, strangler-fig completion Phase 3), classification/ODE-ceiling-guard.

Same shape and purpose as ``doorwindow_fsm_authoritative_compare.py``/
``nat_vent_fsm_authoritative_compare.py``/``override_grace_fsm_authoritative_
compare.py``/``fan_fsm_authoritative_compare.py`` — proves that flipping
``AutomationEngine._classification_fsm_authoritative`` from its default
``False`` to ``True`` (with the other four ``*_fsm_authoritative`` flags left
at their scenario-default ``False``) produces a byte-for-byte identical
``event_log``/``action_log`` across the full golden + pending scenario corpus.

Single-switch only — a compound-interaction diagnosis (this flag combined with
any of the other 4) is out of scope here, same convention every other
single-switch comparator documents for itself.

Use via ``tools.sim_harness.differential.diff_runs(scenario,
mutate_b=fsm_authoritative_mutation)``. Run A is untouched production (flag
default False), run B forces ``_classification_fsm_authoritative`` True on
every engine instance constructed during that run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def fsm_authoritative_mutation() -> Iterator[None]:
    """Force ``_classification_fsm_authoritative = True`` on every engine
    constructed while this context is active — the "run B" side of a
    diff_runs comparison.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.automation import AutomationEngine

    original_init = AutomationEngine.__init__

    def _wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._classification_fsm_authoritative = True

    with patch.object(AutomationEngine, "__init__", _wrapped_init):
        yield
