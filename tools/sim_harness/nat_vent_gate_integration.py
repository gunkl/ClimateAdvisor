"""nat_vent_gate_integration — production load-bearing proof for the reactivation gate.

`_nat_vent_may_reactivate()` in automation.py now directly calls
`decide_nat_vent_gate()` (mirroring the same extraction already done for
`fan_thermostat_check()` / Issue #435 follow-up) — there is no separate "old"
inline implementation left to substitute against; production simply IS the
pure function's caller. `substitute_new_gate()` in nat_vent_gate_compare.py
predates this and is now comparing production to itself (still harmless, but
no longer informative on its own).

What remains meaningful to prove: the extraction is genuinely LOAD-BEARING.
`break_nat_vent_gate()` patches the exact name automation.py imported at
module scope (`automation.decide_nat_vent_gate` — a direct top-level import,
so the patch target is the IMPORTING module, not the source module) to an
inverted function. If a real scenario's full action_log/event_log does NOT
diverge when this is applied, the extraction isn't actually driving behavior.
"""

from __future__ import annotations

import contextlib
from typing import Any


@contextlib.contextmanager
def break_nat_vent_gate():
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import decide_nat_vent_gate as original  # noqa: PLC0415

    def _broken(inputs: Any) -> bool:
        # Preserve the None-safety guarantee production code relies on (never
        # activate with unavailable outdoor/indoor) — a blind inversion would force
        # True in exactly that case, tripping downstream formatting code that only
        # runs when the real gate's None-guard already ruled activation out.
        if inputs.outdoor is None or inputs.indoor is None:
            return False
        return not original(inputs)

    with patch("custom_components.climate_advisor.automation.decide_nat_vent_gate", _broken):
        yield
