"""doorwindow_fsm_authoritative_compare — Step 2/Step 3 equivalence proof (Issue #594
Phase R), door/window lifecycle.

Same shape and purpose as ``nat_vent_fsm_authoritative_compare.py`` — proves that
flipping ``AutomationEngine._doorwindow_fsm_authoritative`` from its default
``False`` to ``True`` produces a byte-for-byte identical ``event_log``/``action_log``
across the full golden + pending scenario corpus.

**Partial-authority scope (unlike nat-vent's comparator, which covers the whole
lifecycle)**: this increment's flag only governs
``handle_manual_override_during_pause()``/``resume_from_pause()`` — see each
method's own FSM-authoritative branch in ``automation.py`` and
``door_window_fsm.py``'s docstring for why the other 5 door/window methods stay on
the legacy path regardless of this flag. A clean corpus diff here proves those 2
methods are behavior-identical when authoritative; it says nothing about the other
5, which don't read this flag at all.

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
    """Force ``_doorwindow_fsm_authoritative = True`` on every engine constructed
    while this context is active — the "run B" side of a diff_runs comparison.
    """
    from unittest.mock import patch

    from custom_components.climate_advisor.automation import AutomationEngine

    original_init = AutomationEngine.__init__

    def _wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._doorwindow_fsm_authoritative = True

    with patch.object(AutomationEngine, "__init__", _wrapped_init):
        yield
