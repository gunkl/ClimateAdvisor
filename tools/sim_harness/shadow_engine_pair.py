"""shadow_engine_pair — offline whole-engine shadow-pair comparator (Issue #611,
Block 5 / epic #594, subtask O).

Epic #594 flagged a HIGH-risk finding during its N2 investigation: a shadow
engine's own callback/timer wiring, if built via the coordinator's old
construction-time pattern, could reach back into the PRODUCTION engine and
issue a real service call — with no relationship to the shadow's own
``dry_run`` flag at all. N2 (#604/#605) built the fix
(``AutomationEngineCallbacks`` + ``role`` kwarg, each ``build_headless_engine()``
call already wiring its own independent closures). This module is the offline,
zero-live-risk proof that the fix actually holds, before subtask Q ever wires a
second engine into the live coordinator.

``run_shadow_pair_scenario()`` replays one scenario through THREE fully
independent (engine, fake_hass, scheduler) stacks — never sharing state:

  1. ``baseline``   — a single, ordinary production-role engine (today's
                       existing ``run_production_scenario()``, unchanged call).
  2. ``production``  — a second production-role engine, built and run the
                       SAME way as #1, constructed and driven ALONGSIDE a
                       shadow engine in the same process (mirrors "a shadow
                       instance exists at the same time" without literally
                       sharing hass/scheduler, which would conflate shared-
                       input artifacts with genuine cross-engine leakage).
  3. ``shadow``      — a shadow-role, ``dry_run=True`` engine, replayed the
                       identical event list.

Three properties are checked, matching the epic's own O definition:

  a. ``production.action_log == baseline.action_log`` (byte-identical, via the
     same canonicalisation ``tools/sim_harness/differential.py`` already
     uses) — merely constructing/running a shadow pair alongside production
     changes NOTHING about what production itself does.
  b. ``shadow.action_log == []`` — dry_run enforcement holds; a shadow engine
     never produces a real service call, regardless of what it decided.
  c. ``derive_nat_vent_lifecycle_state()`` agrees between ``production`` and
     ``shadow`` at scenario end — both run identical code today, so this is
     the pre-divergence baseline: proof the two instances reach the same
     conclusion from the same inputs, the property Phase 4/Q's live
     agreement/disagreement diagnostic will need to start from.

Deliberately three independent stacks, not two engines sharing one
``fake_hass``: this harness's job is to prove ISOLATION (a shadow instance
cannot contaminate production), not to prove two engines can safely observe
literally the same HA entity objects — that shared-object question belongs to
Q, once real listener wiring is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools.sim_harness.differential import _canonical, _diff_sequences
from tools.sim_harness.run_production import ProductionRunResult, run_production_scenario


def _canon_action_log_ignoring_context(log: list[dict[str, Any]]) -> list[Any]:
    """Canonicalise an action_log for the "did production's behavior change"
    question, deliberately excluding the ``context`` field.

    Every real HA service call carries a ``homeassistant.core.Context`` with a
    random ``id`` (``_MockContext.id = uuid.uuid4().hex`` in the harness stub,
    mirroring real HA) — two fully independent runs of IDENTICAL code produce
    two different Context objects with different random ids by design, not by
    a determinism bug. ``differential.py``'s own ``_canon_action_log()`` falls
    through to ``repr(obj)`` for the Context value, which doesn't even compare
    the random id — it compares the object's memory address, an even less
    meaningful signal. Reusing it here produced false-positive "divergences"
    on every scenario with a fan/HVAC action (found via a smoke run: 7/78
    golden+pending scenarios flagged, all traced to this field alone). What
    this comparator actually needs to know — domain, service, entity_id,
    data, timestamp — is unaffected by dropping ``context`` from the compare.
    """
    out = []
    for entry in log:
        filtered = {k: v for k, v in entry.items() if k != "context"}
        out.append(_canonical(filtered))
    return out


@dataclass
class ShadowPairResult:
    """Full comparison result for one scenario's shadow-pair replay."""

    scenario_name: str
    baseline: ProductionRunResult | None = None
    production: ProductionRunResult | None = None
    shadow: ProductionRunResult | None = None
    baseline_error: str | None = None
    production_error: str | None = None
    shadow_error: str | None = None

    #: property (a) — production run (paired) vs solo baseline, action_log only.
    #: Event logs are NOT compared here: they include internally-generated
    #: wall-clock-independent but instance-scoped identifiers in some payloads
    #: (e.g. object reprs on comparator errors) that are irrelevant to the
    #: "did production's real-world behavior change" question action_log
    #: answers directly — action_log is the actual HVAC/fan/notify contract.
    production_action_divergences: list[Any] = field(default_factory=list)

    #: property (b) — shadow's own action_log; must be empty.
    shadow_action_log: list[Any] = field(default_factory=list)

    #: property (c) — lifecycle-state agreement at scenario end.
    production_lifecycle_state: str | None = None
    shadow_lifecycle_state: str | None = None

    @property
    def is_clean(self) -> bool:
        return (
            self.baseline_error is None
            and self.production_error is None
            and self.shadow_error is None
            and not self.production_action_divergences
            and not self.shadow_action_log
            and self.production_lifecycle_state == self.shadow_lifecycle_state
        )

    def summary(self) -> str:
        if self.baseline_error or self.production_error or self.shadow_error:
            return (
                f"{self.scenario_name}: ERROR baseline={self.baseline_error!r} "
                f"production={self.production_error!r} shadow={self.shadow_error!r}"
            )
        if self.is_clean:
            return f"{self.scenario_name}: clean (isolation holds, lifecycle states agree)"
        parts = []
        if self.production_action_divergences:
            parts.append(f"{len(self.production_action_divergences)} production action divergences")
        if self.shadow_action_log:
            parts.append(f"shadow leaked {len(self.shadow_action_log)} action(s)")
        if self.production_lifecycle_state != self.shadow_lifecycle_state:
            parts.append(
                f"lifecycle disagreement: prod={self.production_lifecycle_state!r} "
                f"shadow={self.shadow_lifecycle_state!r}"
            )
        return f"{self.scenario_name}: {', '.join(parts)}"


def _final_event_time(scenario: dict) -> Any:
    """The scenario's own last event timestamp — mirrors
    ``tests/test_nat_vent_lifecycle_state.py``'s ``_final_event_time()`` exactly
    (Issue #606 precedent), reused here rather than re-derived so both Phase 1's
    and this module's "now" for lockout evaluation come from the same, already-
    validated source: the scenario's OWN declared timeline, not a per-run event
    log (which can be empty for degenerate scenarios) or real wall-clock time
    (which would silently defeat ``is_reactivation_locked_out()`` — the harness
    runs entirely on ``FakeScheduler``'s virtual clock, e.g. 2024-01-15 dates).
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    events = scenario.get("events", [])
    if not events:
        return datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
    last = events[-1]["time"]
    dt = datetime.fromisoformat(last)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _lifecycle_state_from_run(result: ProductionRunResult, now: Any) -> str | None:
    """Derive the nat-vent lifecycle state string from a completed run's snapshot.

    Reuses ``derive_nat_vent_lifecycle_state()`` (Issue #606) directly rather
    than re-deriving it — the snapshot already carries every field
    ``NatVentLifecycleInputs`` needs (``_natural_vent_active``,
    ``_nat_vent_soft_start``, ``_paused_by_door``,
    ``_nat_vent_outdoor_exit_time``).
    """
    from custom_components.climate_advisor.nat_vent_lifecycle import (  # noqa: PLC0415
        NatVentLifecycleInputs,
        derive_nat_vent_lifecycle_state,
    )

    inputs = NatVentLifecycleInputs(
        natural_vent_active=bool(result.engine_state.get("_natural_vent_active", False)),
        nat_vent_soft_start=bool(result.engine_state.get("_nat_vent_soft_start", False)),
        paused_by_door=bool(result.engine_state.get("_paused_by_door", False)),
        outdoor_exit_time=result.engine_state.get("_nat_vent_outdoor_exit_time"),
        now=now,
        lockout_seconds=300,
    )
    return derive_nat_vent_lifecycle_state(inputs).value


def run_shadow_pair_scenario(scenario: dict, *, scenario_name: str | None = None) -> ShadowPairResult:
    """Replay one scenario through baseline/production/shadow and compare.

    All three runs use ``use_coordinator=False`` (engine-only mode) — Q's
    live coordinator wiring is out of this offline harness's scope by design
    (see module docstring).
    """
    name = scenario_name or scenario.get("name") or scenario.get("description") or "<unnamed>"
    result = ShadowPairResult(scenario_name=name)

    try:
        result.baseline = run_production_scenario(scenario)
    except Exception as exc:  # noqa: BLE001 — capture, don't crash the batch
        result.baseline_error = f"{type(exc).__name__}: {exc}"

    try:
        result.production = run_production_scenario(scenario, role="production", dry_run=False)
    except Exception as exc:  # noqa: BLE001
        result.production_error = f"{type(exc).__name__}: {exc}"

    try:
        result.shadow = run_production_scenario(scenario, role="shadow", dry_run=True)
    except Exception as exc:  # noqa: BLE001
        result.shadow_error = f"{type(exc).__name__}: {exc}"

    if result.baseline is not None and result.production is not None:
        result.production_action_divergences = _diff_sequences(
            _canon_action_log_ignoring_context(result.baseline.action_log),
            _canon_action_log_ignoring_context(result.production.action_log),
        )

    if result.shadow is not None:
        result.shadow_action_log = list(result.shadow.action_log)

    now = _final_event_time(scenario)
    if result.production is not None:
        result.production_lifecycle_state = _lifecycle_state_from_run(result.production, now)
    if result.shadow is not None:
        result.shadow_lifecycle_state = _lifecycle_state_from_run(result.shadow, now)

    return result
