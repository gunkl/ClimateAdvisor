"""Tests for the differential-replay harness (architecture-reset Step 1).

Covers:
  - the diff engine's own sequence-diff logic (unit),
  - the positive control ("test the test"): a mutated run MUST diverge, per log type,
  - old-vs-old determinism: representative goldens diff to zero against themselves.

Mirrors tests/test_production_harness.py's invocation pattern — the harness runner
manages its own event loop via asyncio.run internally, so these are plain sync tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.differential import (  # noqa: E402
    LogDivergence,
    _diff_sequences,
    bump_setpoint_mutation,
    diff_runs,
    extra_event_mutation,
    old_vs_old,
)

GOLDEN_DIR = TOOLS / "simulations" / "golden"

# A scenario known to command at least one setpoint and emit classification events —
# used to exercise both positive controls. Chosen because it applies a classification
# and cycles occupancy (guaranteed set_temperature calls).
_SETPOINT_SCENARIO = "away-mode-classification-cycle"

# A small representative spread for old-vs-old determinism checks.
_REPRESENTATIVE = [
    "away-mode-classification-cycle",
    "nat-vent-evening-activation",
    "warm_day_ceiling_breach_ac_defense",
    "2026-03-28-overnight",
]


def _load(name: str) -> dict:
    path = GOLDEN_DIR / f"{name}.json"
    if not path.exists():
        pytest.skip(f"golden scenario {name!r} not present")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Diff-engine unit tests
# ---------------------------------------------------------------------------


def test_diff_sequences_identical_is_empty() -> None:
    assert _diff_sequences([1, 2, 3], [1, 2, 3]) == []


def test_diff_sequences_detects_value_change() -> None:
    divs = _diff_sequences([1, 2, 3], [1, 9, 3])
    assert len(divs) == 1
    assert divs[0] == LogDivergence(index=1, a=2, b=9)


def test_diff_sequences_detects_length_mismatch() -> None:
    divs = _diff_sequences([1, 2], [1, 2, 3])
    assert len(divs) == 1
    assert divs[0] == LogDivergence(index=2, a=None, b=3)


# ---------------------------------------------------------------------------
# Positive control — the diff engine MUST detect a real difference per log type
# ---------------------------------------------------------------------------


def test_positive_control_action_log_detects_setpoint_mutation() -> None:
    scen = _load(_SETPOINT_SCENARIO)
    diff = diff_runs(scen, mutate_b=lambda: bump_setpoint_mutation(5.0), scenario_name=_SETPOINT_SCENARIO)
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert diff.action_divergences, (
        "positive control FAILED: a +5°F setpoint mutation produced no action_log divergence — "
        "the diff engine cannot be trusted to detect action differences"
    )


def test_positive_control_event_log_detects_extra_event() -> None:
    scen = _load(_SETPOINT_SCENARIO)
    diff = diff_runs(scen, mutate_b=extra_event_mutation, scenario_name=_SETPOINT_SCENARIO)
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert diff.event_divergences, (
        "positive control FAILED: the extra-event mutation produced no event_log divergence — "
        "the diff engine cannot be trusted to detect event differences"
    )


# ---------------------------------------------------------------------------
# Old-vs-old determinism — identical code, identical inputs → zero divergence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _REPRESENTATIVE)
def test_old_vs_old_is_clean(name: str) -> None:
    scen = _load(name)
    diff = old_vs_old(scen, scenario_name=name)
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert diff.is_clean, (
        f"{name}: old-vs-old divergence (a hidden input the harness failed to control): "
        f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences"
    )
