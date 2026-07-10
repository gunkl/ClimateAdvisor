"""decision_compare_base — shared scaffolding for value-returning decision comparators.

Factors out the shape shared by differential comparators that prove a pure
``decide_*()`` module is behavior-identical to the production method it
replaces, for the common case where that production method RETURNS a value
(a bool, float, or small dataclass) rather than expressing its outcome only
through side effects. ``nat_vent_gate_compare.py`` is the first instance of
this shape; the architecture-consolidation Phase A extractions (sleep-aware
floor resolver, fan-suppression predicate, occupancy-defer predicate,
comfort-band branch) all return a value too, so each gets a comparator by
supplying ``(engine_cls, method_name, reconstruct_inputs, pure_fn)`` here
instead of hand-rolling a new ``Call``/``ComparisonRun`` pair and
instrumentation contextmanager.

Two modes, mirroring ``nat_vent_gate_compare.py``'s original split:

- **Shadow mode** (``instrumented_value_decision`` / ``compare_scenario``):
  the real production method runs and its answer drives the live engine; the
  new pure function is evaluated on the same reconstructed inputs ONLY for
  comparison — it never affects behavior.
- **Substitution mode** (``substitute_new_decision``): the new pure
  function's answer is what actually gets returned to the live engine. Use
  with ``tools.sim_harness.differential.diff_runs(scenario,
  mutate_b=substitute_new_decision(...))`` to diff the entire resulting
  ``action_log``/``event_log`` against an untouched baseline.

Out of scope: ``fan_thermostat_decision_compare.py`` does NOT fit this base.
Its production method (``fan_thermostat_check()``) returns nothing — the
outcome must be inferred by observing side-effect calls
(``_exit_nat_vent()``/``_deactivate_fan()``), and inputs must be captured
BEFORE the call because production mutates state as part of the decision.
Forcing that shape onto a value-returning base would either lose the
side-effect-observation correctness or bloat this module with a second,
unrelated instrumentation strategy for a single consumer. It stays a bespoke
comparator; see its own module docstring for the full reasoning.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch


@dataclass
class DecisionCall:
    """One intercepted call to a production decision method + the comparison result."""

    scenario_name: str
    real_kwargs: dict[str, Any]
    real_result: Any
    new_inputs: Any
    new_result: Any

    @property
    def agrees(self) -> bool:
        return self.real_result == self.new_result


@dataclass
class DecisionComparisonRun:
    calls: list[DecisionCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    @property
    def n_agree(self) -> int:
        return sum(1 for c in self.calls if c.agrees)

    @property
    def disagreements(self) -> list[DecisionCall]:
        return [c for c in self.calls if not c.agrees]


@contextlib.contextmanager
def instrumented_value_decision(
    engine_cls: type,
    method_name: str,
    reconstruct_inputs: Callable[[Any, dict[str, Any]], Any],
    pure_fn: Callable[[Any], Any],
    run: DecisionComparisonRun,
    scenario_name: str,
):
    """Shadow-mode instrumentation for a production method that RETURNS a value.

    Wraps ``engine_cls.method_name`` so every call: runs the real method
    unchanged, reconstructs the equivalent pure-function inputs via
    ``reconstruct_inputs(self, kwargs)``, computes ``pure_fn(inputs)``, and
    records agreement on `run` — without ever changing what the real method
    returns to its caller.
    """
    original = getattr(engine_cls, method_name)

    def _wrapped(self: Any, **kwargs: Any) -> Any:
        real_result = original(self, **kwargs)
        try:
            new_inputs = reconstruct_inputs(self, kwargs)
            new_result = pure_fn(new_inputs)
            run.calls.append(
                DecisionCall(
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

    with patch.object(engine_cls, method_name, _wrapped):
        yield


def compare_scenario(
    scenario: dict,
    scenario_name: str,
    run: DecisionComparisonRun,
    *,
    engine_cls: type,
    method_name: str,
    reconstruct_inputs: Callable[[Any, dict[str, Any]], Any],
    pure_fn: Callable[[Any], Any],
) -> None:
    """Run one scenario through the real engine with `method_name` instrumented (shadow mode)."""
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    with instrumented_value_decision(engine_cls, method_name, reconstruct_inputs, pure_fn, run, scenario_name):
        run_production_scenario(scenario)


@contextlib.contextmanager
def substitute_new_decision(
    engine_cls: type,
    method_name: str,
    reconstruct_inputs: Callable[[Any, dict[str, Any]], Any],
    pure_fn: Callable[[Any], Any],
):
    """Substitution mode: the pure function's answer replaces the real return value.

    Intended for ``tools.sim_harness.differential.diff_runs(scenario,
    mutate_b=lambda: substitute_new_decision(...))`` so the FULL resulting
    action_log/event_log can be diffed against an untouched baseline, not
    just one function's return value.
    """

    def _substituted(self: Any, **kwargs: Any) -> Any:
        new_inputs = reconstruct_inputs(self, kwargs)
        return pure_fn(new_inputs)

    with patch.object(engine_cls, method_name, _substituted):
        yield
