"""differential — old-vs-old / old-vs-mutated differential replay over the production engine.

This is the characterization + differential-replay harness (architecture-reset
Step 1). It runs a scenario through the REAL production engine (via
``run_production_scenario``) twice and diffs the resulting ``event_log`` +
``action_log``. Two modes:

**old-vs-old (a determinism / hidden-input detector).** Both runs use identical,
unmodified code. Any divergence between two runs of the SAME code is an input the
harness failed to control (wall-clock, RNG, dict/set iteration order,
un-snapshotted ``self._`` state). Zero old-vs-old divergence is the *exit*
criterion for Step 1 — reaching it proves input capture + state isolation is
complete, and the divergences found en route enumerate the hidden inputs for us.

**old-vs-mutated (the positive control / "test the test").** A deliberately
injected behavioral change MUST produce a non-empty diff, per log type. A diff
engine that always returned "equal" would also pass old-vs-old; the positive
control proves detection actually works before any zero result is trusted.

Nothing here touches production code — it is pure test/tooling that drives the
existing ``tools/sim_harness`` machinery.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tools.sim_harness.run_production import ProductionRunResult, run_production_scenario

# A mutation is a zero-arg callable returning a context manager that is active
# during a single run_production_scenario call (e.g. a unittest.mock.patch).
MutationFactory = Callable[[], contextlib.AbstractContextManager]


# ---------------------------------------------------------------------------
# Canonicalisation — make log entries stably comparable across two runs
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> Any:
    """Recursively convert a log entry into a stable, comparable structure.

    - ``datetime`` → ISO string (virtual-clock times are deterministic; a
      wall-clock leak shows up here as a mismatched string).
    - ``dict`` → dict with canonicalised values (insertion order irrelevant to ==).
    - ``list``/``tuple`` → list of canonicalised items (order IS significant).
    - other JSON scalars → as-is.
    - anything else → ``repr`` (last-resort stable-ish form; a memory-address in a
      repr would itself surface as a divergence, which is the point).
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def _canon_event_log(log: list[tuple[str, dict, datetime | None]]) -> list[Any]:
    """Canonicalise an event_log: list of (event_type, payload, ts)."""
    return [[etype, _canonical(payload), _canonical(ts)] for (etype, payload, ts) in log]


def _canon_action_log(log: list[dict]) -> list[Any]:
    """Canonicalise an action_log: list of {domain, service, data, ts}."""
    return [_canonical(entry) for entry in log]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@dataclass
class LogDivergence:
    """One positional difference between two canonicalised logs."""

    index: int
    a: Any | None  # entry from run A (None if A is shorter)
    b: Any | None  # entry from run B (None if B is shorter)


@dataclass
class ScenarioDiff:
    """Full diff result for one scenario run pair."""

    scenario_name: str
    event_divergences: list[LogDivergence] = field(default_factory=list)
    action_divergences: list[LogDivergence] = field(default_factory=list)
    a_error: str | None = None  # exception rendering, if run A raised
    b_error: str | None = None  # exception rendering, if run B raised

    @property
    def is_clean(self) -> bool:
        return (
            not self.event_divergences and not self.action_divergences and self.a_error is None and self.b_error is None
        )

    def summary(self) -> str:
        if self.a_error or self.b_error:
            return f"{self.scenario_name}: ERROR a={self.a_error!r} b={self.b_error!r}"
        if self.is_clean:
            return f"{self.scenario_name}: clean (0 event, 0 action divergences)"
        return (
            f"{self.scenario_name}: {len(self.event_divergences)} event, "
            f"{len(self.action_divergences)} action divergences"
        )


def _diff_sequences(a: list[Any], b: list[Any]) -> list[LogDivergence]:
    """Positional diff of two canonicalised sequences (order significant)."""
    out: list[LogDivergence] = []
    for i in range(max(len(a), len(b))):
        av = a[i] if i < len(a) else None
        bv = b[i] if i < len(b) else None
        if av != bv:
            out.append(LogDivergence(index=i, a=av, b=bv))
    return out


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------


def _run_once(scenario: dict, mutate: MutationFactory | None = None) -> ProductionRunResult:
    """Run a scenario once, optionally inside a mutation context."""
    if mutate is None:
        return run_production_scenario(scenario)
    with mutate():
        return run_production_scenario(scenario)


def diff_runs(
    scenario: dict,
    *,
    mutate_b: MutationFactory | None = None,
    scenario_name: str | None = None,
) -> ScenarioDiff:
    """Run a scenario twice and diff the two runs.

    Args:
        scenario: parsed scenario dict.
        mutate_b: if given, run B executes inside this mutation context (used for
                  the positive control). If ``None``, this is an old-vs-old run.
        scenario_name: label for reporting; defaults to ``scenario['name']``.

    Returns:
        A ``ScenarioDiff``. For old-vs-old, ``is_clean`` should ultimately be
        ``True``; every divergence is a hidden input to capture. For a positive
        control, ``is_clean`` should be ``False`` (detection works).
    """
    name = scenario_name or scenario.get("name") or scenario.get("description") or "<unnamed>"

    a_error: str | None = None
    b_error: str | None = None
    result_a: ProductionRunResult | None = None
    result_b: ProductionRunResult | None = None

    try:
        result_a = _run_once(scenario)
    except Exception as exc:  # noqa: BLE001 — capture, don't crash the batch
        a_error = f"{type(exc).__name__}: {exc}"
    try:
        result_b = _run_once(scenario, mutate=mutate_b)
    except Exception as exc:  # noqa: BLE001
        b_error = f"{type(exc).__name__}: {exc}"

    diff = ScenarioDiff(scenario_name=name, a_error=a_error, b_error=b_error)
    if result_a is None or result_b is None:
        return diff

    diff.event_divergences = _diff_sequences(
        _canon_event_log(result_a.event_log),
        _canon_event_log(result_b.event_log),
    )
    diff.action_divergences = _diff_sequences(
        _canon_action_log(result_a.action_log),
        _canon_action_log(result_b.action_log),
    )
    return diff


def old_vs_old(scenario: dict, *, scenario_name: str | None = None) -> ScenarioDiff:
    """Convenience: run a scenario against itself (determinism / hidden-input check)."""
    return diff_runs(scenario, mutate_b=None, scenario_name=scenario_name)


# ---------------------------------------------------------------------------
# Positive-control mutations (test-the-test)
# ---------------------------------------------------------------------------


@contextmanager
def bump_setpoint_mutation(delta_f: float = 5.0) -> Iterator[None]:
    """Positive control for the ACTION log: shift every commanded setpoint by delta_f.

    Wraps ``FakeState`` feedback? No — patches the engine's ``_set_temperature`` so
    any scenario that commands a temperature yields a different ``action_log`` in
    run B, proving the action-log diff detects real differences.
    """
    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415

    original = AutomationEngine._set_temperature

    async def _wrapped(self: Any, temperature: float, *args: Any, **kwargs: Any) -> Any:
        return await original(self, float(temperature) + delta_f, *args, **kwargs)

    from unittest.mock import patch  # noqa: PLC0415

    with patch.object(AutomationEngine, "_set_temperature", _wrapped):
        yield


@contextmanager
def extra_event_mutation() -> Iterator[None]:
    """Positive control for the EVENT log: emit one extra marker event per run.

    Patches ``AutomationEngine.apply_classification`` to emit a spurious event
    before delegating, guaranteeing the event_log differs in run B. Proves the
    event-log diff detects real differences independently of the action log.
    """
    from custom_components.climate_advisor.automation import AutomationEngine  # noqa: PLC0415

    original = AutomationEngine.apply_classification

    async def _wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        cb = getattr(self, "_emit_event_callback", None)
        if cb is not None:
            cb("differential_positive_control_marker", {"injected": True})
        return await original(self, *args, **kwargs)

    from unittest.mock import patch  # noqa: PLC0415

    with patch.object(AutomationEngine, "apply_classification", _wrapped):
        yield
