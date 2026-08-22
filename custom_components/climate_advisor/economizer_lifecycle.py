"""Pure session-state derivation for the economizer (two-phase window-cooling)
lifecycle — strangler-fig completion program, Phase 5 (Issue #746).

Mirrors ``nat_vent_lifecycle.py``'s role for nat-vent: names the states the
engine's existing economizer flags can represent and provides one pure
function to derive the current one — no new state is stored anywhere.

Unlike nat-vent (4 states across 2 flags with independent meaning), the
economizer's 2 flags are fully redundant with each other in every real
construction site in ``automation.py`` — ``self._economizer_active`` is always
set/cleared in lockstep with ``self._economizer_phase`` leaving/entering
``"inactive"`` (confirmed by reading every write site in
``check_window_cooling_opportunity()``/``_deactivate_economizer()``: the two
fields are never set independently). So this module models a single 3-value
enum derived primarily from ``economizer_phase``, with ``economizer_active``
read defensively (same "derive from every flag, don't just trust one" caution
``nat_vent_lifecycle.py`` already applies) in case the two ever desync.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EconomizerLifecycleState(Enum):
    """The three states an economizer session can be in."""

    INACTIVE = "inactive"
    COOL_DOWN = "cool-down"
    MAINTAIN = "maintain"


@dataclass(frozen=True)
class EconomizerLifecycleInputs:
    """Every flag the derivation reads — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine`` attributes:
      economizer_active  -> self._economizer_active
      economizer_phase   -> self._economizer_phase ("inactive"/"cool-down"/"maintain")
    """

    economizer_active: bool
    economizer_phase: str


def derive_economizer_lifecycle_state(inputs: EconomizerLifecycleInputs) -> EconomizerLifecycleState:
    """Pure derivation of the current economizer session state from existing flags.

    ``economizer_active=False`` always wins to ``INACTIVE`` regardless of
    ``economizer_phase`` — defensive precedence in case a caller ever clears
    only one of the two fields. Otherwise the phase string maps directly to its
    matching state; an unrecognized phase string (should never happen in
    production) also falls back to ``INACTIVE`` rather than raising.
    """
    if not inputs.economizer_active:
        return EconomizerLifecycleState.INACTIVE
    if inputs.economizer_phase == EconomizerLifecycleState.COOL_DOWN.value:
        return EconomizerLifecycleState.COOL_DOWN
    if inputs.economizer_phase == EconomizerLifecycleState.MAINTAIN.value:
        return EconomizerLifecycleState.MAINTAIN
    return EconomizerLifecycleState.INACTIVE
