"""Tests for the territory-map coverage instrument (architecture-reset Step 1).

Verifies the map builds over the golden corpus and reports coverage in the shape
downstream steps depend on. Not asserting the exact blind-spot list (that shifts as
goldens are added) — only that the instrument runs, records real calls, and yields a
well-formed coverage report.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.territory_map import _DECISION_METHODS, build_map  # noqa: E402


def test_territory_map_builds_and_records_calls() -> None:
    m = build_map()
    assert m["n_goldens"] >= 20, f"expected the full golden set, found {m['n_goldens']}"
    # The map must report every catalogued decision function (fired or blind).
    assert set(m["decision_calls"].keys()) == set(_DECISION_METHODS)
    # The corpus must exercise the core nat-vent/classification paths heavily —
    # if these are zero, the instrument is not actually wrapping the engine.
    assert m["decision_calls"]["apply_classification"] > 0
    assert m["decision_calls"]["check_natural_vent_conditions"] > 0
    # Control primitives must fire (goldens command setpoints).
    assert m["control_calls"]["_set_temperature"] > 0


def test_territory_map_reports_blind_spots_as_list() -> None:
    m = build_map()
    # blind_spots is exactly the decision functions with zero calls.
    expected = {meth for meth, c in m["decision_calls"].items() if c == 0}
    assert set(m["blind_spots"]) == expected
