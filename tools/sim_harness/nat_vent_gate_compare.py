"""nat_vent_gate_compare — old-vs-new differential comparator for the nat-vent gate.

Architecture-reset Step 2: the first real "old vs new" comparison in this
project (everything in Step 1 was old-vs-old). Two modes, sharing one input
reconstruction:

- **Shadow mode** (``compare_scenario`` / ``_instrumented_gate``): the real
  production ``_nat_vent_may_reactivate()`` runs and its answer drives the live
  engine; the new pure ``decide_nat_vent_gate()`` is evaluated on the same
  reconstructed inputs ONLY for comparison — it never affects behavior. Proves
  "would the new function have agreed," not "does the rest of the scenario
  unfold the same way if you let it decide."
- **Substitution mode** (``substitute_new_gate`` / ``run_substituted_scenario``):
  the new pure function's answer is what actually gets returned to the live
  engine — it genuinely drives behavior. Used with ``tools.sim_harness.differential``'s
  ``diff_runs(scenario, mutate_b=substitute_new_gate)`` to diff the ENTIRE
  resulting ``action_log``/``event_log`` against an untouched baseline, not just
  one function's boolean. This is what closes the "shadow-only, never
  substituted" gap found after Step 2 shipped shadow-mode only.

Both modes reconstruct the full set of raw inputs the call chain actually used
(reading ``self.config`` directly and resolving ``in_sleep_window`` via the real
``_in_sleep_window()`` helper — not back-derived from already-resolved values),
via the shared ``_reconstruct_inputs()`` helper, so there is exactly one place
that logic lives (not duplicated between shadow and substitution modes).

Production integration follow-up: ``_nat_vent_may_reactivate()`` now calls
``decide_nat_vent_gate()`` directly (mirroring the same extraction done for
``fan_thermostat_check()``) — production simply IS the pure function's caller.
This means shadow mode above is now comparing production to itself (still
harmless, still a valid regression guard, but no longer an "old vs new"
comparison in the original sense), and substitution mode's "does letting the
new function decide change anything" question is trivially "no" by
construction. The genuinely informative check now is proving the extraction
is LOAD-BEARING — see ``tools/sim_harness/nat_vent_gate_integration.py``'s
positive control (patches ``automation.decide_nat_vent_gate``, the name
automation.py actually calls, to an inverted function and confirms real
scenarios diverge).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any


def _reconstruct_inputs(self: Any, kwargs: dict[str, Any]) -> Any:
    """Rebuild the equivalent NatVentGateInputs from a live _nat_vent_may_reactivate call.

    Shared by both shadow and substitution modes so there is exactly one place
    this reconstruction logic lives.
    """
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    from custom_components.climate_advisor import const  # noqa: PLC0415
    from custom_components.climate_advisor.automation import _in_sleep_window  # noqa: PLC0415
    from custom_components.climate_advisor.nat_vent_gate import NatVentGateInputs  # noqa: PLC0415

    comfort_heat_raw = float(self.config.get("comfort_heat", 70))
    sleep_heat = float(self.config.get(const.CONF_SLEEP_HEAT, comfort_heat_raw))
    in_sleep_window = _in_sleep_window(dt_util.now(), self.config)
    nat_vent_delta = float(self.config.get(const.CONF_NATURAL_VENT_DELTA, const.DEFAULT_NATURAL_VENT_DELTA))
    fan_mode = self.config.get(const.CONF_FAN_MODE, const.FAN_MODE_DISABLED)
    aggressive_savings = bool(self.config.get("aggressive_savings", False))

    return NatVentGateInputs(
        outdoor=kwargs.get("outdoor"),
        indoor=kwargs.get("indoor"),
        comfort_heat_raw=comfort_heat_raw,
        sleep_heat=sleep_heat,
        in_sleep_window=in_sleep_window,
        comfort_cool=float(kwargs.get("comfort_cool")),
        nat_vent_delta=nat_vent_delta,
        hysteresis=float(kwargs.get("hysteresis", 0.0)),
        fan_mode=fan_mode,
        aggressive_savings=aggressive_savings,
    )


# ---------------------------------------------------------------------------
# Shadow mode — compare only, never drives behavior
# ---------------------------------------------------------------------------


@dataclass
class GateCall:
    """One intercepted call to the real reactivation gate + the comparison result."""

    scenario_name: str
    real_kwargs: dict[str, Any]
    real_result: bool
    new_inputs: Any  # NatVentGateInputs
    new_result: bool

    @property
    def agrees(self) -> bool:
        return self.real_result == self.new_result


@dataclass
class GateComparisonRun:
    calls: list[GateCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    @property
    def n_agree(self) -> int:
        return sum(1 for c in self.calls if c.agrees)

    @property
    def disagreements(self) -> list[GateCall]:
        return [c for c in self.calls if not c.agrees]


@contextlib.contextmanager
def _instrumented_gate(run: GateComparisonRun, scenario_name: str):
    """Wrap AutomationEngine._nat_vent_may_reactivate to intercept every call (shadow mode)."""
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415
    from custom_components.climate_advisor.nat_vent_gate import decide_nat_vent_gate  # noqa: PLC0415

    original = AutomationEngine._nat_vent_may_reactivate

    def _wrapped(self: Any, **kwargs: Any) -> bool:
        real_result = original(self, **kwargs)

        try:
            new_inputs = _reconstruct_inputs(self, kwargs)
            new_result = decide_nat_vent_gate(new_inputs)
            run.calls.append(
                GateCall(
                    scenario_name=scenario_name,
                    real_kwargs=dict(kwargs),
                    real_result=real_result,
                    new_inputs=new_inputs,
                    new_result=new_result,
                )
            )
        except Exception as exc:  # noqa: BLE001 — never let comparison-side errors break production
            run.errors.append(f"{scenario_name}: comparator error: {type(exc).__name__}: {exc}")

        return real_result

    with patch.object(AutomationEngine, "_nat_vent_may_reactivate", _wrapped):
        yield


def compare_scenario(scenario: dict, scenario_name: str, run: GateComparisonRun) -> None:
    """Run one scenario through the real engine with the gate instrumented (shadow mode)."""
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    with _instrumented_gate(run, scenario_name):
        run_production_scenario(scenario)


# ---------------------------------------------------------------------------
# Substitution mode — the new function's answer actually drives behavior
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def substitute_new_gate():
    """Replace _nat_vent_may_reactivate's REAL return value with decide_nat_vent_gate()'s
    answer, reconstructed from the same inputs the real call would have used.

    Unlike shadow mode, this genuinely changes what the live engine does if the
    new function ever disagrees — intended for use as ``diff_runs(scenario,
    mutate_b=substitute_new_gate)`` so the FULL resulting action_log/event_log
    can be diffed against an untouched baseline, not just one function's boolean.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415
    from custom_components.climate_advisor.nat_vent_gate import decide_nat_vent_gate  # noqa: PLC0415

    def _substituted(self: Any, **kwargs: Any) -> bool:
        new_inputs = _reconstruct_inputs(self, kwargs)
        return decide_nat_vent_gate(new_inputs)

    with patch.object(AutomationEngine, "_nat_vent_may_reactivate", _substituted):
        yield
