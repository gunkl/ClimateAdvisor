"""Tests for Issue #606 (Block 5 Phase 1): nat-vent session-state derivation.

Three layers of verification, in increasing order of "does this reflect real
production behavior":

1. Direct unit tests of ``derive_nat_vent_lifecycle_state()`` — exhaustive over
   the flag space, including the reactivation-lockout boundary.
2. A broad consistency smoke test across every golden + pending scenario: the
   derived state, computed from each scenario's REAL final engine flags after a
   full production replay, must satisfy the invariants the pure function's own
   contract promises (catches wiring bugs — wrong field name, wrong precedence
   — against a wide corpus of real flag combinations no hand-written unit test
   anticipated).
3. Hand-verified ground truth for a handful of named golden scenarios, read and
   reasoned about independently of the derivation function's own code, so this
   check cannot be circular. See each scenario's own JSON for the reasoning
   this test's expectations are based on.

No golden or pending scenario currently exercises the soft-start sub-mode
(``ACTIVE_SOFT_START``) end-to-end — soft-start has direct unit coverage in
``tests/test_nat_vent_soft_start.py`` but no golden replay reaches it. Noted
here rather than silently absent; a future scenario addition would extend
layer 3's ground-truth coverage to the fourth state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.climate_advisor.nat_vent_lifecycle import (
    NatVentLifecycleInputs,
    NatVentLifecycleState,
    derive_nat_vent_lifecycle_state,
)
from tools.sim_harness.run_production import run_production_scenario

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tools" / "simulations" / "golden"
_PENDING_DIR = _REPO_ROOT / "tools" / "simulations" / "pending"
_DEFAULT_LOCKOUT_S = 300.0


def _inputs(
    *,
    natural_vent_active: bool = False,
    nat_vent_soft_start: bool = False,
    paused_by_door: bool = False,
    outdoor_exit_time: datetime | None = None,
    now: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    lockout_seconds: float = _DEFAULT_LOCKOUT_S,
) -> NatVentLifecycleInputs:
    return NatVentLifecycleInputs(
        natural_vent_active=natural_vent_active,
        nat_vent_soft_start=nat_vent_soft_start,
        paused_by_door=paused_by_door,
        outdoor_exit_time=outdoor_exit_time,
        now=now,
        lockout_seconds=lockout_seconds,
    )


class TestDeriveNatVentLifecycleStateUnit:
    def test_inactive_clean_idle(self) -> None:
        assert derive_nat_vent_lifecycle_state(_inputs()) == NatVentLifecycleState.INACTIVE

    def test_inactive_paused_but_not_from_outdoor_exit(self) -> None:
        # paused_by_door=True with no recorded outdoor_exit_time (e.g. a plain
        # door/window pause unrelated to nat-vent) — never locked out.
        assert derive_nat_vent_lifecycle_state(_inputs(paused_by_door=True)) == NatVentLifecycleState.INACTIVE

    def test_active_full_gate(self) -> None:
        assert (
            derive_nat_vent_lifecycle_state(_inputs(natural_vent_active=True)) == NatVentLifecycleState.ACTIVE_FULL_GATE
        )

    def test_active_soft_start(self) -> None:
        assert (
            derive_nat_vent_lifecycle_state(_inputs(natural_vent_active=True, nat_vent_soft_start=True))
            == NatVentLifecycleState.ACTIVE_SOFT_START
        )

    def test_active_takes_precedence_over_paused_flag(self) -> None:
        # natural_vent_active=True together with a stale paused_by_door=True
        # (e.g. reactivated from PAUSED_REACTIVATION_LOCKOUT without the pause
        # flag having been cleared yet in some hypothetical caller) must still
        # read as active — active session state always wins.
        assert (
            derive_nat_vent_lifecycle_state(_inputs(natural_vent_active=True, paused_by_door=True))
            == NatVentLifecycleState.ACTIVE_FULL_GATE
        )

    def test_paused_reactivation_lockout_just_inside_window(self) -> None:
        now = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)
        exit_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # 300s ago, exactly at boundary
        state = derive_nat_vent_lifecycle_state(
            _inputs(paused_by_door=True, outdoor_exit_time=exit_time, now=now, lockout_seconds=300.0)
        )
        # elapsed == lockout_seconds: is_reactivation_locked_out() uses strict '<',
        # so exactly-at-boundary has already elapsed -> NOT locked out.
        assert state == NatVentLifecycleState.INACTIVE

    def test_paused_reactivation_lockout_one_second_inside_window(self) -> None:
        now = datetime(2026, 1, 1, 12, 4, 59, tzinfo=UTC)
        exit_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # 299s ago
        state = derive_nat_vent_lifecycle_state(
            _inputs(paused_by_door=True, outdoor_exit_time=exit_time, now=now, lockout_seconds=300.0)
        )
        assert state == NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT

    def test_paused_reactivation_lockout_elapsed(self) -> None:
        now = datetime(2026, 1, 1, 12, 10, 0, tzinfo=UTC)
        exit_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)  # 600s ago
        state = derive_nat_vent_lifecycle_state(
            _inputs(paused_by_door=True, outdoor_exit_time=exit_time, now=now, lockout_seconds=300.0)
        )
        assert state == NatVentLifecycleState.INACTIVE

    def test_paused_reactivation_lockout_requires_paused_flag(self) -> None:
        # outdoor_exit_time set + still within window, but paused_by_door=False
        # (e.g. the pause was separately resolved) — not locked-out state.
        now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
        exit_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        state = derive_nat_vent_lifecycle_state(
            _inputs(paused_by_door=False, outdoor_exit_time=exit_time, now=now, lockout_seconds=300.0)
        )
        assert state == NatVentLifecycleState.INACTIVE


def _load_scenario(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _all_golden_and_pending_scenarios() -> list[Path]:
    paths = [p for p in _GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    paths += [p for p in _PENDING_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    return sorted(paths)


def _final_event_time(scenario: dict) -> datetime:
    events = scenario.get("events", [])
    if not events:
        return datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
    last = events[-1]["time"]
    dt = datetime.fromisoformat(last)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _derive_state_for_scenario(scenario: dict) -> NatVentLifecycleState:
    use_coordinator = bool(scenario.get("use_coordinator", False))
    result = run_production_scenario(scenario, use_coordinator=use_coordinator)
    snap = result.engine_state
    lockout_s = float(scenario.get("config", {}).get("nat_vent_reactivation_lockout_s", _DEFAULT_LOCKOUT_S))
    inputs = NatVentLifecycleInputs(
        natural_vent_active=bool(snap.get("_natural_vent_active", False)),
        nat_vent_soft_start=bool(snap.get("_nat_vent_soft_start", False)),
        paused_by_door=bool(snap.get("_paused_by_door", False)),
        outdoor_exit_time=snap.get("_nat_vent_outdoor_exit_time"),
        now=_final_event_time(scenario),
        lockout_seconds=lockout_s,
    )
    return derive_nat_vent_lifecycle_state(inputs)


class TestScenarioReplayConsistency:
    """Layer 2: broad invariant check across every golden + pending scenario."""

    @pytest.mark.parametrize(
        "scenario_path",
        _all_golden_and_pending_scenarios(),
        ids=lambda p: p.stem,
    )
    def test_derived_state_consistent_with_final_flags(self, scenario_path: Path) -> None:
        scenario = _load_scenario(scenario_path)
        use_coordinator = bool(scenario.get("use_coordinator", False))
        result = run_production_scenario(scenario, use_coordinator=use_coordinator)
        snap = result.engine_state

        state = _derive_state_for_scenario(scenario)

        natural_vent_active = bool(snap.get("_natural_vent_active", False))
        paused_by_door = bool(snap.get("_paused_by_door", False))

        if state in (NatVentLifecycleState.ACTIVE_FULL_GATE, NatVentLifecycleState.ACTIVE_SOFT_START):
            assert natural_vent_active, (
                f"{scenario_path.stem}: derived {state} but _natural_vent_active is False in the "
                "real final engine snapshot"
            )
        else:
            assert not natural_vent_active, (
                f"{scenario_path.stem}: derived {state} (not ACTIVE_*) but _natural_vent_active is "
                "True in the real final engine snapshot"
            )

        if state == NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT:
            assert paused_by_door, f"{scenario_path.stem}: PAUSED_REACTIVATION_LOCKOUT but _paused_by_door is False"
            assert snap.get("_nat_vent_outdoor_exit_time") is not None, (
                f"{scenario_path.stem}: PAUSED_REACTIVATION_LOCKOUT but no _nat_vent_outdoor_exit_time recorded"
            )


class TestGroundTruthScenarios:
    """Layer 3: hand-verified expected end state for named scenarios, reasoned
    about independently by reading each scenario's own events/assertions/verdict
    (see docstrings below) rather than derived from the function under test."""

    def test_mild_all_day_nat_vent_only_ends_active_full_gate(self) -> None:
        """Window opens 08:00, nat-vent sustained all day (outdoor < indoor
        throughout, never approaches the 77F threshold) — verdict explicitly
        says 'nat vent persists all day, no HVAC fired.' Never a soft-start
        entry (gate is always the fast bulk-cooling path per its own event
        reasons), never an exit event. Expect ACTIVE_FULL_GATE."""
        scenario = _load_scenario(_GOLDEN_DIR / "mild_all_day_nat_vent_only.json")
        assert _derive_state_for_scenario(scenario) == NatVentLifecycleState.ACTIVE_FULL_GATE

    def test_comfort_floor_exit_ends_inactive_not_paused(self) -> None:
        """Issue #99 golden: comfort-floor exit inside check_natural_vent_conditions()
        is a hand-rolled inline exit (confirmed by reading automation.py directly —
        it does NOT route through _exit_nat_vent(), unlike most other exits) that
        clears only _natural_vent_active and never touches _paused_by_door or
        _nat_vent_outdoor_exit_time. The scenario's own verdict states this
        explicitly: 'fan stops, heat restores, paused_by_door stays False.'
        Expect INACTIVE, not PAUSED_REACTIVATION_LOCKOUT."""
        scenario = _load_scenario(_GOLDEN_DIR / "nat-vent-comfort-floor-exit-restores-heat.json")
        assert _derive_state_for_scenario(scenario) == NatVentLifecycleState.INACTIVE

    def test_outdoor_rise_exit_with_sensor_still_open_ends_locked_out(self) -> None:
        """Issue #115 golden: nat-vent activates at 18:00, the door/window sensor
        is opened once and never closed for the rest of the scenario, outdoor
        rises above indoor at 20:00 triggering nat_vent_outdoor_rise_exit. That
        exit path sets _nat_vent_outdoor_exit_time and — because the monitored
        sensor is still open — hands off into the pause lifecycle
        (_paused_by_door=True) rather than grace. At the scenario's final event
        time (20:00, the same instant the exit fired), the 300s reactivation
        lockout has not yet elapsed. Expect PAUSED_REACTIVATION_LOCKOUT."""
        scenario = _load_scenario(_GOLDEN_DIR / "nat-vent-outdoor-rises-above-indoor-exit.json")
        assert _derive_state_for_scenario(scenario) == NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT
