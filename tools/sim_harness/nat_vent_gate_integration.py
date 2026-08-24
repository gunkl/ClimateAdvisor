"""nat_vent_gate_integration — production load-bearing proof for the reactivation gate.

Updated for Issue #757 Phase 6 Step 5 (strangler-fig graduation): 9 of the 10
real nat-vent trigger sites in automation.py no longer call
`_nat_vent_may_reactivate()` at all — they call `nat_vent_fsm.transition()`
directly, which reaches `decide_nat_vent_gate()` via `nat_vent_fsm.py`'s own
separate top-level import of the same name (a different module-level
reference than `automation.py`'s). Patching `automation.decide_nat_vent_gate`
stopped affecting the vast majority of real nat-vent decisions the moment
Step 5 made the FSM path unconditional — exactly the kind of silent breakage
this positive control exists to catch (and did catch, in Step 5's own
verification pass; the same class of gap Issue #759 found for
`fan_thermostat_decision_integration.py` in Step 2).

What remains meaningful to prove: the extraction is genuinely LOAD-BEARING for
the path that now actually decides nat-vent reactivation in production.
`break_nat_vent_gate()` patches the name `nat_vent_fsm.py` actually calls
(`nat_vent_fsm.decide_nat_vent_gate`) to an inverted function. If a real
scenario's full action_log/event_log does NOT diverge when this is applied,
the extraction isn't actually driving behavior.
"""

from __future__ import annotations

import contextlib
from typing import Any


@contextlib.contextmanager
def break_nat_vent_gate():
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.nat_vent_fsm import decide_nat_vent_gate as original  # noqa: PLC0415

    def _broken(inputs: Any) -> bool:
        # Preserve the None-safety guarantee production code relies on (never
        # activate with unavailable outdoor/indoor) — a blind inversion would force
        # True in exactly that case, tripping downstream formatting code that only
        # runs when the real gate's None-guard already ruled activation out.
        if inputs.outdoor is None or inputs.indoor is None:
            return False
        return not original(inputs)

    with patch("custom_components.climate_advisor.nat_vent_fsm.decide_nat_vent_gate", _broken):
        yield
