"""Issue #1644 (Silver test-coverage): tools/coverage_ratchet.py keeps each module's coverage at or above its floor.

Occupant view: none directly. Coverage is how a change to one part of the house logic is shown not to break
another quietly; the ratchet stops it from slipping module by module while the climb to 95 % goes on.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ca_coverage_ratchet", REPO / "tools" / "coverage_ratchet.py")
ratchet = importlib.util.module_from_spec(_spec)
sys.modules["ca_coverage_ratchet"] = ratchet
_spec.loader.exec_module(ratchet)

PKG = "custom_components/climate_advisor/"


def _cov(**modules: float) -> dict:
    return {
        name.replace("__", "."): ratchet.ModuleCoverage(name.replace("__", "."), pct, 3, 1)
        for name, pct in modules.items()
    }


def _floors(tolerance: float = 0.0, **modules: float):
    return ratchet.Floors(
        target=95.0, tolerance=tolerance, modules={k.replace("__", "."): v for k, v in modules.items()}
    )


def _report(tmp_path: Path, **modules: float) -> Path:
    files = {
        f"{PKG}{name.replace('__', '.')}": {
            "summary": {"percent_covered": pct, "missing_lines": 2, "missing_branches": 1, "num_partial_branches": 1}
        }
        for name, pct in modules.items()
    }
    files["tests/conftest.py"] = {"summary": {"percent_covered": 10.0}}  # outside the package: ignored
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"files": files, "totals": {}}), encoding="utf-8")
    return path


def test_module_below_its_floor_fails():
    result = ratchet.check(_cov(a__py=91.9), _floors(a__py=92.0))
    assert len(result.errors) == 1
    assert "a.py" in result.errors[0] and "fell below its floor 92.0%" in result.errors[0]


def test_module_at_or_within_tolerance_of_its_floor_passes():
    assert not ratchet.check(_cov(a__py=92.0), _floors(a__py=92.0)).errors
    assert not ratchet.check(_cov(a__py=91.9), _floors(0.2, a__py=92.0)).errors


def test_new_module_below_target_fails_and_above_target_is_a_note():
    result = ratchet.check(_cov(old__py=90.0, low__py=80.0, high__py=99.0), _floors(old__py=90.0))
    assert len(result.errors) == 1 and result.errors[0].startswith("low.py: new module at 80.0%")
    assert any(n.startswith("high.py: new module") for n in result.notes)


def test_floor_for_a_module_that_is_gone_fails():
    result = ratchet.check(_cov(a__py=99.0), _floors(a__py=90.0, gone__py=80.0))
    assert [e.split(":")[0] for e in result.errors] == ["gone.py"]


def test_improvement_is_a_note_never_a_failure():
    result = ratchet.check(_cov(a__py=97.0), _floors(a__py=90.0))
    assert not result.errors
    assert any("above its floor" in n for n in result.notes)
    assert any("coverage_ratchet.py update" in n for n in result.notes)


def test_update_raises_never_lowers_adds_new_and_drops_gone():
    new, changes = ratchet.update(
        _cov(up__py=93.47, down__py=80.0, fresh__py=70.05), _floors(up__py=90.0, down__py=85.0, gone__py=50.0)
    )
    assert new.modules == {"up.py": 93.4, "down.py": 85.0, "fresh.py": 70.0}
    assert any(c.startswith("gone.py: dropped") for c in changes)


def test_floor_rounds_down_to_one_decimal():
    assert ratchet.floor_of(93.4999) == 93.4
    assert ratchet.floor_of(100.0) == 100.0
    assert ratchet.floor_of(70.1) == 70.1  # float noise in 70.1 * 10 must not round it down to 70.0


def test_report_lists_modules_under_target_largest_gap_first():
    table = ratchet.report_markdown(_cov(b__py=90.0, a__py=60.0, c__py=99.0), _floors(a__py=60.0), 95.0)
    rows = [line for line in table.splitlines() if line.startswith("| `")]
    assert [r.split("`")[1] for r in rows] == ["a.py", "b.py", "c.py"]
    assert "2 below it" in table
    assert "| 35.0 |" in rows[0]  # gap to target
    assert "| new |" in rows[1]


def test_main_check_exit_codes_and_summary(tmp_path):
    floors = tmp_path / "floors.json"
    ratchet.save_floors(_floors(a__py=90.0), floors)
    summary = tmp_path / "summary.md"
    ok = _report(tmp_path, a__py=95.0)
    assert ratchet.main(["check", str(ok), "--floors", str(floors), "--summary", str(summary)]) == 0
    assert "Coverage per module" in summary.read_text(encoding="utf-8")
    bad = _report(tmp_path, a__py=80.0)
    assert ratchet.main(["check", str(bad), "--floors", str(floors)]) == 1
    assert ratchet.main(["check", str(tmp_path / "missing.json"), "--floors", str(floors)]) == 2
    assert ratchet.main(["report", str(tmp_path / "missing.json"), "--floors", str(floors)]) == 0


def test_main_update_writes_the_floors_file(tmp_path):
    floors = tmp_path / "floors.json"
    ratchet.save_floors(_floors(a__py=90.0), floors)
    assert ratchet.main(["update", str(_report(tmp_path, a__py=96.25, b__py=50.0)), "--floors", str(floors)]) == 0
    data = json.loads(floors.read_text(encoding="utf-8"))
    assert data["modules"] == {"a.py": 96.2, "b.py": 50.0}
    assert data["target"] == 95.0


def test_committed_floors_cover_every_module_of_the_package():
    """Every integration module has a floor, and every floor names a real module (no stale or missing entries)."""
    floors = ratchet.load_floors()
    package = REPO / "custom_components" / "climate_advisor"
    modules = {p.relative_to(package).as_posix() for p in package.rglob("*.py") if "__pycache__" not in p.parts}
    assert set(floors.modules) == modules
    assert floors.target == 95.0
    assert all(0.0 <= v <= 100.0 for v in floors.modules.values())


@pytest.mark.parametrize("job", ["coverage", "typing"])
def test_validate_workflow_gates_on_the_ratchet_jobs(job):
    """The required check `validate-and-test` must wait for and require both ratchet jobs."""
    text = (REPO / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    needs = next(line for line in text.splitlines() if line.strip().startswith("needs: [lint, unit, isolated"))
    assert job in needs
    assert f"test '${{{{ needs.{job}.result }}}}' = success" in text
