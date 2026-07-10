"""Tests for the chart_log input-trajectory driver (architecture-reset Step 1).

Uses a small SYNTHETIC fixture, not real chart_log data — chart_log files are real
home telemetry and are never committed to the test suite. The driver's contract
(discard recorded decisions, keep only indoor/outdoor/ts, skip nulls, respect
max_entries/stride) is what's under test here; it was additionally validated
manually against a real ~5700-entry chart_log with zero old-vs-old divergence
(see the differential-replay Step-1 status report).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
for _p in (str(REPO_ROOT), str(TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.chart_log_driver import (  # noqa: E402
    build_scenario_from_chart_log,
    load_chart_log,
)
from tools.sim_harness.differential import old_vs_old  # noqa: E402

_SYNTHETIC_ENTRIES = [
    {"ts": "2026-06-01T08:00:00-07:00", "hvac": "off", "fan": False, "indoor": 72.0, "outdoor": 65.0},
    # A "decision" field present in the real log (hvac/fan) — must be discarded, not replayed.
    {"ts": "2026-06-01T08:30:00-07:00", "hvac": "cool", "fan": True, "indoor": 73.0, "outdoor": 68.0},
    # Null indoor — must be skipped, not crash.
    {"ts": "2026-06-01T09:00:00-07:00", "hvac": "cool", "fan": True, "indoor": None, "outdoor": 70.0},
    # Null outdoor — must be skipped.
    {"ts": "2026-06-01T09:30:00-07:00", "hvac": "cool", "fan": True, "indoor": 74.0, "outdoor": None},
    {"ts": "2026-06-01T10:00:00-07:00", "hvac": "cool", "fan": True, "indoor": 75.0, "outdoor": 72.0},
]


def test_build_scenario_discards_recorded_decisions_and_skips_nulls() -> None:
    scen = build_scenario_from_chart_log(_SYNTHETIC_ENTRIES, name="unit_test")
    # 5 entries in, 2 null-skipped -> 3 usable events.
    assert len(scen["events"]) == 3
    for ev in scen["events"]:
        assert ev["type"] == "temp_update"
        assert set(ev.keys()) == {"type", "time", "indoor_f", "outdoor_f"}
        # No hvac/fan/windows_* keys ever carried through — inputs only.
    assert scen["events"][0]["indoor_f"] == 72.0
    assert scen["events"][0]["outdoor_f"] == 65.0


def test_build_scenario_respects_max_entries_and_stride() -> None:
    scen = build_scenario_from_chart_log(_SYNTHETIC_ENTRIES, max_entries=2)
    assert len(scen["events"]) == 2
    # max_entries takes the most recent usable entries (from the end).
    assert scen["events"][-1]["indoor_f"] == 75.0

    scen_strided = build_scenario_from_chart_log(_SYNTHETIC_ENTRIES, stride=2)
    assert len(scen_strided["events"]) == 2  # 3 usable -> indices 0,2


def test_build_scenario_skips_malformed_timestamps() -> None:
    entries = [*_SYNTHETIC_ENTRIES, {"ts": "not-a-timestamp", "indoor": 70.0, "outdoor": 60.0}]
    scen = build_scenario_from_chart_log(entries)
    assert len(scen["events"]) == 3  # malformed ts entry excluded


def test_load_chart_log_missing_file_returns_empty() -> None:
    assert load_chart_log("/nonexistent/path/does_not_exist.json") == []


def test_load_chart_log_round_trips_entries_key(tmp_path: Path) -> None:
    p = tmp_path / "sample.json"
    p.write_text(json.dumps({"entries": _SYNTHETIC_ENTRIES}), encoding="utf-8")
    loaded = load_chart_log(p)
    assert loaded == _SYNTHETIC_ENTRIES


def test_chart_log_driven_old_vs_old_is_clean() -> None:
    """The synthetic trajectory, replayed through the real engine, must diff to zero."""
    scen = build_scenario_from_chart_log(_SYNTHETIC_ENTRIES, name="unit_test_replay")
    diff = old_vs_old(scen, scenario_name="unit_test_replay")
    assert not diff.a_error and not diff.b_error, (diff.a_error, diff.b_error)
    assert diff.is_clean, (
        f"old-vs-old divergence on synthetic chart_log replay: "
        f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences"
    )
