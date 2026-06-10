"""_outcomes_smoke — validate production outcome extraction for 4 golden scenarios.

Runnable as::

    python -m tools.sim_harness._outcomes_smoke       # from worktree root
    pytest tools/sim_harness/_outcomes_smoke.py -v    # via pytest

Loads 4 representative golden scenarios, runs each through the production
adapter, then extracts the outcome timeline via ``outcomes.production_decisions()``.

Assertions (this module):
  - run completes without raising
  - ``callback_errors == []``
  - extracted outcome list is non-empty for scenarios with known decisions
  - outcome strings are from the known legacy vocabulary (no ``unknown:*`` entries)

Outcome comparison against scenario assertions is G4's job — this module
only proves that extraction yields sensible, plausible timelines.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

from tools.sim_harness.outcomes import (  # noqa: E402
    production_decisions,
    production_outcome_at,
    production_temp_at,
)
from tools.sim_harness.run_production import run_production_scenario  # noqa: E402

GOLDEN_DIR = Path(_PROJECT_ROOT) / "tools" / "simulations" / "golden"

# 4 representative scenarios: different event types, different day types
_SCENARIOS = [
    "cool_all_day_heat_all_day",  # cold day: classification + bedtime setback
    "mild_all_day_nat_vent_only",  # mild day: classification + nat-vent activation
    "hot_all_day_no_nat_vent_window",  # hot day: classification + sensor open/close
    "away-mode-classification-cycle",  # occupancy: away setback + home restore
]

# Legacy outcome vocabulary (all strings that Decision.outcome can take in simulate.py)
_KNOWN_OUTCOMES: frozenset[str] = frozenset(
    {
        "natural_ventilation",
        "paused",
        "resumed",
        "no_action",
        "setback_applied",
        "comfort_restored",
        "classification_applied",
        "ceiling_guard_fired",
        "bedtime_setback_skipped",
        "warm_day_comfort_gap",
        "override_detected",
        "override_confirmed",
        "override_self_resolved",
        "override_cleared",
        "fan_off",
        "fan_cycle_on",
        "fan_cycle_off",
        "stale_fan_cleared",
        "nat_vent_fan_preserved",
        "nat_vent_comfort_floor_exit",
        "nat_vent_outdoor_rise_exit",
        "nat_vent_away_ceiling_exit",
        "dual_setback_applied",
        "morning_wakeup_skipped",
        "economizer_engaged",
        "economizer_disengaged",
        # Ceiling guard variants seen in golden assertions
        "ceiling_guard_dormant_after_ac_run",
        "ceiling_guard_dormant_after_forecast_improvement",
        "ceiling_guard_fires_cool",
        "ceiling_guard_would_fire",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_scenario(name: str) -> dict:
    path = GOLDEN_DIR / f"{name}.json"
    with open(path) as f:
        return json.load(f)


def _safe_print(line: str) -> None:
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"))


def _print_outcome_timeline(name: str, result: Any, decisions: list) -> None:
    """Print a scenario's production outcome timeline."""
    _safe_print(f"\n{'=' * 70}")
    _safe_print(f"SCENARIO: {name}")
    _safe_print(f"{'=' * 70}")

    _safe_print(f"\n--- raw event_log ({len(result.event_log)} entries) ---")
    for evt_type, payload, ts in result.event_log:
        ts_str = ts.isoformat() if ts else "?"
        _safe_print(f"  [{ts_str}]  {evt_type}  {payload}")

    _safe_print(f"\n--- extracted outcome timeline ({len(decisions)} entries) ---")
    for d in decisions:
        temp_suffix = f"  -> {d.target_temp}F" if d.target_temp is not None else ""
        _safe_print(f"  [{d.time}]  [{d.outcome}]{temp_suffix}  (from: {d.event_type})")

    _safe_print(f"\n--- action_log ({len(result.action_log)} entries) ---")
    for action in result.action_log:
        ts_str = action["ts"].isoformat() if action["ts"] else "?"
        _safe_print(f"  [{ts_str}]  {action['domain']}.{action['service']}  {action['data']}")

    if result.callback_errors:
        _safe_print(f"\n--- callback_errors ({len(result.callback_errors)}) ---")
        for err_dt, exc in result.callback_errors:
            _safe_print(f"  [{err_dt}] {type(exc).__name__}: {exc}")
    else:
        _safe_print("\n--- callback_errors: none ---")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run_one(name: str) -> None:
    """Run one scenario and validate outcome extraction."""
    scenario = _load_scenario(name)
    result = run_production_scenario(scenario)
    decisions = production_decisions(result)

    _print_outcome_timeline(name, result, decisions)

    # 1. No callback errors
    assert result.callback_errors == [], f"{name}: expected no callback errors, got: {result.callback_errors}"

    # 2. At least one outcome or action (scenario did something)
    assert len(decisions) > 0 or len(result.action_log) > 0, (
        f"{name}: no outcomes or actions — engine appears to have done nothing"
    )

    # 3. All extracted outcomes use known vocabulary (no unknown:* entries)
    unknown = [d for d in decisions if d.outcome.startswith("unknown:")]
    assert unknown == [], f"{name}: unknown outcome strings detected: {[d.outcome for d in unknown]}"

    # 4. Quick spot-check: production_outcome_at + production_temp_at work
    if decisions:
        last_ts = decisions[-1].time
        oa = production_outcome_at(decisions, last_ts)
        assert oa != "no_decision", f"{name}: outcome_at(last_ts) returned 'no_decision'"
        # temp_at is allowed to be None (not all outcomes have temps)
        _ = production_temp_at(decisions, last_ts)

    _safe_print(f"\n  PASS: {name} — {len(decisions)} outcome(s) extracted, callback_errors=[]")


def test_cool_all_day_heat_all_day() -> None:
    _run_one("cool_all_day_heat_all_day")


def test_mild_all_day_nat_vent_only() -> None:
    _run_one("mild_all_day_nat_vent_only")


def test_hot_all_day_no_nat_vent_window() -> None:
    _run_one("hot_all_day_no_nat_vent_window")


def test_away_mode_classification_cycle() -> None:
    _run_one("away-mode-classification-cycle")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all() -> None:
    tests = [
        test_cool_all_day_heat_all_day,
        test_mild_all_day_nat_vent_only,
        test_hot_all_day_no_nat_vent_window,
        test_away_mode_classification_cycle,
    ]
    passed = 0
    failed = 0
    for t in tests:
        _safe_print(f"\nRunning {t.__name__}...")
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback  # noqa: PLC0415

            _safe_print(f"  FAIL: {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1

    _safe_print(f"\n{'=' * 60}")
    _safe_print(f"Outcomes smoke: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
