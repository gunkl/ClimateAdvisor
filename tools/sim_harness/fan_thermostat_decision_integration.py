"""fan_thermostat_decision_integration — production load-bearing proof (Step 2, slice 2).

Updated for Issue #757 Step 2 (strangler-fig graduation): `fan_thermostat_check()`
in automation.py no longer calls `decide_fan_thermostat_check()` directly — it now
delegates entirely to `_resolve_fan_fsm_state()`, and it is `fan_fsm.py`'s own
`_transition_on_thermostat_check_tick()` that calls the pure function (via its own
top-level `from .fan_thermostat_decision import ... decide_fan_thermostat_check`
import). The real call site moved; it did not disappear — patching automation.py's
now-unused copy of the name stopped affecting production behavior at all, which is
exactly the kind of silent breakage this positive control exists to catch (and did
catch, in Step 2's own verification pass).

What remains meaningful to prove: the extraction is genuinely LOAD-BEARING, not
dead code silently unused. `break_fan_thermostat_decision()` patches the name
`fan_fsm.py` actually calls (`fan_fsm.decide_fan_thermostat_check`) to a
rotation-based corruption (every real outcome maps to a different, wrong one —
robust regardless of which outcome a given scenario naturally produces, no fixed
constant to go stale as real outcome coverage grows). If a real scenario's full
action_log/event_log does NOT diverge when this is applied, the extraction isn't
actually driving behavior — a real regression the positive control exists to catch.
"""

from __future__ import annotations

import contextlib
from typing import Any


@contextlib.contextmanager
def break_fan_thermostat_decision():
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.fan_fsm import decide_fan_thermostat_check as original  # noqa: PLC0415
    from custom_components.climate_advisor.fan_thermostat_decision import FanThermostatOutcome  # noqa: PLC0415

    rotation = {
        FanThermostatOutcome.KEEP: FanThermostatOutcome.STOP_DEACTIVATE,
        FanThermostatOutcome.STOP_DEACTIVATE: FanThermostatOutcome.STOP_COOLED_TO_FLOOR,
        FanThermostatOutcome.STOP_COOLED_TO_FLOOR: FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT,
        FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT: FanThermostatOutcome.KEEP,
    }

    def _broken(inputs: Any) -> Any:
        return rotation[original(inputs)]

    with patch("custom_components.climate_advisor.fan_fsm.decide_fan_thermostat_check", _broken):
        yield
