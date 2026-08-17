"""override_grace_fsm_authoritative_compare — full-authority equivalence proof
(Issue #664), override/grace lifecycle.

Same shape and purpose as ``doorwindow_fsm_authoritative_compare.py``/
``nat_vent_fsm_authoritative_compare.py`` — proves that flipping
``AutomationEngine._override_grace_fsm_authoritative`` from its default ``False``
to ``True`` produces a byte-for-byte identical ``event_log``/``action_log`` across
the full golden + pending scenario corpus.

**Full authority, no partial-scope caveat** (unlike door/window's own comparator,
which only covers 2 of 7 methods for its increment) — this flag governs all 8 real
``OverrideGraceFsmEventKind`` call sites simultaneously; see
``AutomationEngine._resolve_override_grace_fsm_state()``'s docstring and the
approved Issue #664 plan's H1-H4 for why full authority was safe to ship in one
increment rather than staged.

Use via ``tools.sim_harness.differential.diff_runs(scenario,
mutate_b=fsm_authoritative_mutation)`` — run A is untouched production (flag
default False), run B forces the flag True on every engine instance constructed
during that run. ``diff.is_clean`` must be True for every scenario in the
golden/pending corpus.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def fsm_authoritative_mutation() -> Iterator[None]:
    """Force ``_override_grace_fsm_authoritative = True`` on every engine
    constructed while this context is active — the "run B" side of a diff_runs
    comparison.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.automation import AutomationEngine

    original_init = AutomationEngine.__init__

    def _wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._override_grace_fsm_authoritative = True

    with patch.object(AutomationEngine, "__init__", _wrapped_init):
        yield
