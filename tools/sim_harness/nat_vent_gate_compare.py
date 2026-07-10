"""nat_vent_gate_compare — old-vs-new differential comparator for the nat-vent gate.

Architecture-reset Step 2: the first real "old vs new" comparison in this
project (everything in Step 1 was old-vs-old). Two modes, sharing one input
reconstruction:

- **Shadow mode** (``compare_scenario``): the real production
  ``_nat_vent_may_reactivate()`` runs and its answer drives the live engine;
  the new pure ``decide_nat_vent_gate()`` is evaluated on the same
  reconstructed inputs ONLY for comparison — it never affects behavior. Proves
  "would the new function have agreed," not "does the rest of the scenario
  unfold the same way if you let it decide."
- **Substitution mode** (``substitute_new_gate``): the new pure function's
  answer is what actually gets returned to the live engine — it genuinely
  drives behavior. Used with ``tools.sim_harness.differential``'s
  ``diff_runs(scenario, mutate_b=substitute_new_gate)`` to diff the ENTIRE
  resulting ``action_log``/``event_log`` against an untouched baseline, not just
  one function's boolean. This is what closes the "shadow-only, never
  substituted" gap found after Step 2 shipped shadow-mode only.

Both modes reconstruct the full set of raw inputs the call chain actually used
(reading ``self.config`` directly and resolving ``in_sleep_window`` via the real
``_in_sleep_window()`` helper — not back-derived from already-resolved values),
via the shared ``_reconstruct_inputs()`` helper, so there is exactly one place
that logic lives (not duplicated between shadow and substitution modes).

Instrumentation itself (the shadow-mode patch/compare loop, the
``GateCall``/``GateComparisonRun`` shape, substitution-mode's patch) is now
provided by ``tools.sim_harness.decision_compare_base`` (Issue #454) — this
module supplies only what's specific to the reactivation gate: input
reconstruction and which production method/pure function to wire together.
``GateCall``/``GateComparisonRun`` are thin aliases of the base's
``DecisionCall``/``DecisionComparisonRun`` kept for backward compatibility with
existing callers (``tools/nat_vent_gate_diff.py``).

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

from typing import Any

from tools.sim_harness.decision_compare_base import (
    DecisionCall as GateCall,
)
from tools.sim_harness.decision_compare_base import (
    DecisionComparisonRun as GateComparisonRun,
)
from tools.sim_harness.decision_compare_base import (
    compare_scenario as _base_compare_scenario,
)
from tools.sim_harness.decision_compare_base import (
    substitute_new_decision,
)

__all__ = ["GateCall", "GateComparisonRun", "compare_scenario", "substitute_new_gate"]


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


def _engine_and_pure_fn():
    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415
    from custom_components.climate_advisor.nat_vent_gate import decide_nat_vent_gate  # noqa: PLC0415

    return AutomationEngine, decide_nat_vent_gate


def compare_scenario(scenario: dict, scenario_name: str, run: GateComparisonRun) -> None:
    """Run one scenario through the real engine with the gate instrumented (shadow mode).

    Import order matters here: ``run_production`` must be imported (installing
    the HA stubs as its own module-level side effect) BEFORE anything imports
    ``custom_components.climate_advisor.automation`` — that module needs
    ``homeassistant.core`` already present in ``sys.modules``. The original
    (pre-#454) ``compare_scenario`` got this ordering right by accident (its
    ``run_production`` import came first, textually, in the function body); a
    first cut of this refactor broke it by resolving the engine class before
    handing off to the shared base. Kept as an explicit, load-bearing import
    here rather than relying on import order elsewhere staying accidentally
    correct.
    """
    from tools.sim_harness.run_production import run_production_scenario  # noqa: F401,PLC0415

    engine_cls, pure_fn = _engine_and_pure_fn()
    _base_compare_scenario(
        scenario,
        scenario_name,
        run,
        engine_cls=engine_cls,
        method_name="_nat_vent_may_reactivate",
        reconstruct_inputs=_reconstruct_inputs,
        pure_fn=pure_fn,
    )


def substitute_new_gate():
    """Replace _nat_vent_may_reactivate's REAL return value with decide_nat_vent_gate()'s
    answer, reconstructed from the same inputs the real call would have used.

    Unlike shadow mode, this genuinely changes what the live engine does if the
    new function ever disagrees — intended for use as ``diff_runs(scenario,
    mutate_b=substitute_new_gate)`` so the FULL resulting action_log/event_log
    can be diffed against an untouched baseline, not just one function's boolean.
    """
    engine_cls, pure_fn = _engine_and_pure_fn()
    return substitute_new_decision(
        engine_cls,
        "_nat_vent_may_reactivate",
        _reconstruct_inputs,
        pure_fn,
    )
