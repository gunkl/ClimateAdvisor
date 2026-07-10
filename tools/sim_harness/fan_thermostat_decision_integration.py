"""fan_thermostat_decision_integration — production load-bearing proof (Step 2, slice 2).

Unlike the nat-vent reactivation gate, `fan_thermostat_check()` in automation.py
now DIRECTLY calls `decide_fan_thermostat_check()` (see automation.py's Issue
#435 follow-up refactor) — there is no separate "old" inline implementation left
to substitute against. The shadow/substitution distinction Step 1 built for the
gate has collapsed into one: production simply IS the pure function's caller.

What remains meaningful to prove: the extraction is genuinely LOAD-BEARING, not
dead code silently unused. `break_fan_thermostat_decision()` patches the exact
name automation.py imported at module scope (`automation.decide_fan_thermostat_check`
— a direct top-level import, unlike the gate's per-call `from ... import`, so the
patch target is the IMPORTING module, not the source module) to a rotation-based
corruption (every real outcome maps to a different, wrong one — robust regardless
of which outcome a given scenario naturally produces, no fixed constant to go
stale as real outcome coverage grows). If a real scenario's full action_log/event_log
does NOT diverge when this is applied, the extraction isn't actually driving
behavior — a real regression the positive control exists to catch.
"""

from __future__ import annotations

import contextlib
from typing import Any


@contextlib.contextmanager
def break_fan_thermostat_decision():
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import decide_fan_thermostat_check as original  # noqa: PLC0415
    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome  # noqa: PLC0415

    rotation = {
        FanThermostatOutcome.KEEP: FanThermostatOutcome.STOP_DEACTIVATE,
        FanThermostatOutcome.STOP_DEACTIVATE: FanThermostatOutcome.STOP_COOLED_TO_FLOOR,
        FanThermostatOutcome.STOP_COOLED_TO_FLOOR: FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT,
        FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT: FanThermostatOutcome.KEEP,
    }

    def _broken(inputs: Any) -> Any:
        return rotation[original(inputs)]

    with patch("custom_components.climate_advisor.automation.decide_fan_thermostat_check", _broken):
        yield
