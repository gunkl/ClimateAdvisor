"""_adapter_smoke — end-to-end validation of the production scenario adapter.

Runnable as::

    python -m tools.sim_harness._adapter_smoke          # from worktree root
    pytest tools/sim_harness/_adapter_smoke.py -v       # via pytest

Loads 2 representative golden scenarios, drives them through
``run_production_scenario``, and prints the decision/action sequence +
final engine state.  The ONLY assertions here are:
  - the run completed without raising
  - ``callback_errors == []``
(Outcome assertions are G3's job.)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

from tools.sim_harness.run_production import run_production_scenario  # noqa: E402

GOLDEN_DIR = Path(_PROJECT_ROOT) / "tools" / "simulations" / "golden"

_SCENARIOS = [
    "cool_all_day_heat_all_day",
    "mild_all_day_nat_vent_only",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_scenario(name: str) -> dict:
    path = GOLDEN_DIR / f"{name}.json"
    with open(path) as f:
        return json.load(f)


def _safe_print(line: str) -> None:
    """Print a line, replacing unencodable characters for Windows console safety."""
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"))


def _print_result(name: str, result) -> None:
    """Pretty-print the decision/action/state output for human review."""
    _safe_print(f"\n{'=' * 70}")
    _safe_print(f"SCENARIO: {name}")
    _safe_print(f"{'=' * 70}")

    _safe_print(f"\n--- event_log ({len(result.event_log)} entries) ---")
    for evt_type, payload, ts in result.event_log:
        ts_str = ts.isoformat() if ts else "?"
        _safe_print(f"  [{ts_str}]  {evt_type}  {payload}")

    _safe_print(f"\n--- action_log ({len(result.action_log)} entries) ---")
    for action in result.action_log:
        ts_str = action["ts"].isoformat() if action["ts"] else "?"
        _safe_print(f"  [{ts_str}]  {action['domain']}.{action['service']}  {action['data']}")

    _safe_print("\n--- engine_state ---")
    for k, v in result.engine_state.items():
        _safe_print(f"  {k}: {v!r}")

    if result.callback_errors:
        _safe_print(f"\n--- callback_errors ({len(result.callback_errors)}) ---")
        for err_dt, exc in result.callback_errors:
            _safe_print(f"  [{err_dt}] {type(exc).__name__}: {exc}")
    else:
        _safe_print("\n--- callback_errors: none ---")


# ---------------------------------------------------------------------------
# Tests (runnable as pytest or standalone)
# ---------------------------------------------------------------------------


def test_cool_all_day_heat_all_day():
    """cold day → heat mode + bedtime setback drives real engine without errors."""
    scenario = _load_scenario("cool_all_day_heat_all_day")
    result = run_production_scenario(scenario)
    _print_result("cool_all_day_heat_all_day", result)

    assert result.callback_errors == [], f"Expected no callback errors, got: {result.callback_errors}"
    # Prove it ran — event_log must be non-empty (classification + bedtime fire events)
    assert len(result.event_log) > 0 or len(result.action_log) > 0, (
        "Expected at least one event or action — engine appears to have done nothing"
    )
    print("  PASS: cool_all_day_heat_all_day ran without callback errors")


def test_mild_all_day_nat_vent_only():
    """mild day → nat-vent activates, runs all day without errors."""
    scenario = _load_scenario("mild_all_day_nat_vent_only")
    result = run_production_scenario(scenario)
    _print_result("mild_all_day_nat_vent_only", result)

    assert result.callback_errors == [], f"Expected no callback errors, got: {result.callback_errors}"
    assert len(result.event_log) > 0 or len(result.action_log) > 0, (
        "Expected at least one event or action — engine appears to have done nothing"
    )
    print("  PASS: mild_all_day_nat_vent_only ran without callback errors")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all() -> None:
    tests = [test_cool_all_day_heat_all_day, test_mild_all_day_nat_vent_only]
    passed = 0
    failed = 0
    for t in tests:
        print(f"\nRunning {t.__name__}...")
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback

            print(f"  FAIL: {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Adapter smoke: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
