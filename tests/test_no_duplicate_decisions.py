"""Golden-level automatic duplicate-decision check (Issue #591, Block 2 Step H).

Runs every golden scenario through the real production engine and asserts no
event is immediately followed by another event of the same type with an
identical payload — the Issue #584/#591 duplicate-decision bug shape — using
the shared conftest.assert_no_duplicate_events() allowlist. This is a permanent
regression guard: a future change that reintroduces an un-deduped multi-trigger
emit site fails this test automatically, across every golden scenario, instead
of waiting to be rediscovered in a live install's Activity Record a fourth time
(Issue #96, #444, #584 each individually rediscovered this bug class).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TOOLS), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from conftest import assert_no_duplicate_events  # noqa: E402

GOLDEN_DIR = TOOLS / "simulations" / "golden"
_GOLDEN_FILES = sorted(p for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json")


@pytest.mark.parametrize("scenario_path", _GOLDEN_FILES, ids=lambda p: p.stem)
def test_golden_has_no_duplicate_decisions(scenario_path: Path) -> None:
    from simulate import run_scenario_production  # noqa: PLC0415

    result = run_scenario_production(scenario_path, state="golden")
    events = [(e["event_type"], e["payload"]) for e in result["event_log"]]
    assert_no_duplicate_events(events)


def test_all_goldens_discovered() -> None:
    """Guard against an empty parametrization silently passing."""
    assert len(_GOLDEN_FILES) >= 20, f"expected the full golden set, found {len(_GOLDEN_FILES)}"
