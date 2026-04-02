"""Tests for tools/simulate.py — simulator CLI, run_scenario, print_result, and new features.

New features covered:
- Verdict field: scenario JSON may include "verdict" dict; run_scenario returns it;
  print_result displays it.
- EXPECTED FAIL: state="pending-fix" + verdict.type="negative" + failing assertions
  → EXPECTED FAIL display.  Passing assertions → promote notice.
- --cases flag: grouped summary across all scenario directories.
- Assertion skip: assertions with "simulator_support": false are skipped ([SKIP]),
  don't count toward pass/fail.
- Backward compatibility: existing scenarios without new fields run without error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import path setup — tools/ is not a package; insert it before test collection
# ---------------------------------------------------------------------------
_TOOLS_DIR = Path(__file__).parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import simulate as _sim  # noqa: E402  (after sys.path insert)
from simulate import ClimateSimulator, print_result, run_scenario  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scenario(
    tmp_path: Path,
    name: str = "test-scenario",
    events: list | None = None,
    assertions: list | None = None,
    config: dict | None = None,
    verdict: dict | None = None,
    **extra,
) -> Path:
    """Write a minimal scenario JSON to tmp_path and return the Path."""
    scenario: dict = {
        "name": name,
        "description": f"Test scenario: {name}",
        "source": "test",
        "config": config or {"comfort_heat": 70, "comfort_cool": 72, "natural_vent_delta": 3.0},
        "events": events or [],
        "assertions": assertions or [],
    }
    if verdict is not None:
        scenario["verdict"] = verdict
    scenario.update(extra)
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(scenario))
    return p


def _passing_assertion() -> dict:
    """Return an assertion that will PASS (no events means outcome == 'no_decision')."""
    return {"at": "2099-01-01T00:00:00", "expect": "no_decision", "reason": "no events fired"}


def _failing_assertion() -> dict:
    """Return an assertion that will FAIL (expects 'natural_ventilation' but none fires)."""
    return {"at": "2099-01-01T00:00:00", "expect": "natural_ventilation", "reason": "expected vent but none fired"}


# ---------------------------------------------------------------------------
# TestVerdictDisplay
# ---------------------------------------------------------------------------


class TestVerdictDisplay:
    def test_print_result_with_verdict(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Verdict dict in result is displayed by print_result."""
        verdict = {
            "type": "negative",
            "summary": "HVAC resumed too early",
            "observed_behavior": "resumed at 06:00",
            "expected_behavior": "should wait until 07:00",
        }
        p = _make_scenario(tmp_path, name="with-verdict", assertions=[_passing_assertion()], verdict=verdict)
        result = run_scenario(p)
        assert result.get("verdict") == verdict, "run_scenario must return verdict in results dict"
        print_result(result)
        captured = capsys.readouterr()
        assert "negative" in captured.out, "verdict type should appear in output"
        assert "HVAC resumed too early" in captured.out, "verdict summary should appear in output"

    def test_print_result_without_verdict(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Scenarios without a verdict field print normally — no crash, no verdict line."""
        p = _make_scenario(tmp_path, name="no-verdict", assertions=[_passing_assertion()])
        result = run_scenario(p)
        assert result.get("verdict") is None
        print_result(result)
        captured = capsys.readouterr()
        # Standard output should still be present
        assert "no-verdict" in captured.out
        assert "PASS" in captured.out
        # No verdict-specific text should appear
        assert "Verdict" not in captured.out
        assert "observed_behavior" not in captured.out

    def test_list_shows_verdict_tag(self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch) -> None:
        """--list output includes [positive] or [negative] tag next to scenario name."""
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()
        positive_verdict = {"type": "positive", "summary": "Works correctly"}
        scenario = {
            "name": "good-scenario",
            "description": "A scenario that works",
            "source": "test",
            "config": {},
            "events": [],
            "assertions": [],
            "verdict": positive_verdict,
        }
        (golden_dir / "good-scenario.json").write_text(json.dumps(scenario))

        monkeypatch.setattr(
            _sim,
            "STATE_DIRS",
            {"golden": golden_dir},
        )
        monkeypatch.setattr(sys, "argv", ["simulate.py", "--list"])
        _sim.main()
        captured = capsys.readouterr()
        assert "positive" in captured.out, "--list should display verdict type tag"


# ---------------------------------------------------------------------------
# TestExpectedFail
# ---------------------------------------------------------------------------


class TestExpectedFail:
    def test_pending_fix_negative_fail_shows_expected_fail(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """state='pending-fix' + verdict.type='negative' + failing assertions → EXPECTED FAIL."""
        verdict = {
            "type": "negative",
            "summary": "known regression",
            "observed_behavior": "wrong",
            "expected_behavior": "right",
        }
        p = _make_scenario(tmp_path, name="pf-fail", assertions=[_failing_assertion()], verdict=verdict)
        result = run_scenario(p, state="pending-fix")
        assert result["passed"] is False, "assertions should still report False"
        print_result(result)
        captured = capsys.readouterr()
        assert "EXPECTED FAIL" in captured.out, "should display EXPECTED FAIL for pending-fix negative"

    def test_pending_fix_negative_pass_shows_promote_notice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """state='pending-fix' + verdict.type='negative' + passing assertions → promote notice."""
        verdict = {
            "type": "negative",
            "summary": "was broken",
            "observed_behavior": "wrong",
            "expected_behavior": "right",
        }
        p = _make_scenario(tmp_path, name="pf-pass", assertions=[_passing_assertion()], verdict=verdict)
        result = run_scenario(p, state="pending-fix")
        assert result["passed"] is True, "assertions should report True when they pass"
        print_result(result)
        captured = capsys.readouterr()
        # Should NOT show EXPECTED FAIL — should show a promote notice instead
        assert "EXPECTED FAIL" not in captured.out
        assert "promot" in captured.out.lower(), "should suggest promoting scenario to golden"

    def test_pending_fix_without_verdict_behaves_normally(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """state='pending-fix' but no verdict field → normal FAIL display (no EXPECTED FAIL)."""
        p = _make_scenario(tmp_path, name="pf-no-verdict", assertions=[_failing_assertion()])
        result = run_scenario(p, state="pending-fix")
        assert result["passed"] is False
        print_result(result)
        captured = capsys.readouterr()
        assert "EXPECTED FAIL" not in captured.out
        assert "FAIL" in captured.out


# ---------------------------------------------------------------------------
# TestAssertionSkip
# ---------------------------------------------------------------------------


class TestAssertionSkip:
    def test_assertion_with_simulator_support_false_skipped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Assertion with simulator_support=false is shown as [SKIP] and not counted."""
        skip_assertion = {
            "at": "2099-01-01T00:00:00",
            "expect": "natural_ventilation",
            "reason": "requires HA state machine",
            "simulator_support": False,
        }
        p = _make_scenario(tmp_path, name="skip-assert", assertions=[skip_assertion])
        result = run_scenario(p)
        # Skipped assertions mean no pass/fail determination — passed should be None or True
        # (not False), since the unsupported assertion was not evaluated
        assert result["passed"] is not False, "skipped assertions must not cause FAIL"
        # The assertion result should be marked as skipped
        assert len(result["assertions"]) == 1
        assert result["assertions"][0].get("skipped") is True, "result should mark assertion as skipped"
        print_result(result)
        captured = capsys.readouterr()
        assert "[SKIP]" in captured.out, "skipped assertion should display [SKIP] marker"
        assert "[FAIL]" not in captured.out

    def test_assertion_without_simulator_support_evaluated_normally(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Normal assertions (no simulator_support key or simulator_support=True) are evaluated."""
        # Passing assertion — no simulator_support key
        p = _make_scenario(tmp_path, name="normal-assert", assertions=[_passing_assertion()])
        result = run_scenario(p)
        assert result["passed"] is True
        assert result["assertions"][0].get("skipped") is not True
        print_result(result)
        captured = capsys.readouterr()
        assert "[OK]" in captured.out
        assert "[SKIP]" not in captured.out

    def test_mixed_skip_and_normal_assertions(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Mix of skipped and normal passing assertions → overall PASS."""
        skip_assertion = {
            "at": "2099-01-01T00:00:00",
            "expect": "natural_ventilation",
            "simulator_support": False,
        }
        normal_passing = _passing_assertion()
        p = _make_scenario(tmp_path, name="mixed-assert", assertions=[skip_assertion, normal_passing])
        result = run_scenario(p)
        assert result["passed"] is True, "only non-skipped assertions count toward pass/fail"
        print_result(result)
        captured = capsys.readouterr()
        assert "[SKIP]" in captured.out
        assert "[OK]" in captured.out

    def test_mixed_skip_and_failing_assertion_still_fails(self, tmp_path: Path) -> None:
        """Skipped assertion + failing normal assertion → overall FAIL."""
        skip_assertion = {
            "at": "2099-01-01T00:00:00",
            "expect": "natural_ventilation",
            "simulator_support": False,
        }
        failing = _failing_assertion()
        p = _make_scenario(tmp_path, name="mixed-fail", assertions=[skip_assertion, failing])
        result = run_scenario(p)
        assert result["passed"] is False, "failing non-skipped assertion should cause overall FAIL"


# ---------------------------------------------------------------------------
# TestCasesFlag
# ---------------------------------------------------------------------------


class TestCasesFlag:
    def _make_dir_with_scenario(
        self,
        parent: Path,
        dir_name: str,
        scenario_name: str,
        verdict: dict | None = None,
        issue: int | None = None,
    ) -> Path:
        d = parent / dir_name
        d.mkdir(parents=True, exist_ok=True)
        scenario: dict = {
            "name": scenario_name,
            "description": f"Scenario in {dir_name}",
            "source": "test",
            "config": {},
            "events": [],
            "assertions": [_passing_assertion()],
        }
        if verdict:
            scenario["verdict"] = verdict
        if issue:
            scenario["issue"] = issue
        (d / f"{scenario_name}.json").write_text(json.dumps(scenario))
        return d

    def test_cases_output_groups_by_directory(self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch) -> None:
        """--cases groups output by directory name."""
        golden_dir = self._make_dir_with_scenario(tmp_path, "golden", "golden-scenario")
        pending_dir = self._make_dir_with_scenario(tmp_path, "pending", "pending-scenario")
        monkeypatch.setattr(
            _sim,
            "STATE_DIRS",
            {"golden": golden_dir, "pending": pending_dir},
        )
        monkeypatch.setattr(_sim, "SIMULATIONS_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["simulate.py", "--cases"])
        _sim.main()
        captured = capsys.readouterr()
        output = captured.out.lower()
        assert "golden" in output, "output should include golden group header"
        assert "pending" in output, "output should include pending group header"
        assert "golden-scenario" in output
        assert "pending-scenario" in output

    def test_cases_output_includes_verdict_and_issue(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch
    ) -> None:
        """--cases output shows verdict type and issue number per scenario."""
        verdict = {"type": "negative", "summary": "Known broken path"}
        golden_dir = self._make_dir_with_scenario(tmp_path, "golden", "issue-scenario", verdict=verdict, issue=42)
        monkeypatch.setattr(_sim, "STATE_DIRS", {"golden": golden_dir})
        monkeypatch.setattr(_sim, "SIMULATIONS_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["simulate.py", "--cases"])
        _sim.main()
        captured = capsys.readouterr()
        assert "negative" in captured.out, "verdict type should appear in --cases output"
        assert "42" in captured.out, "issue number should appear in --cases output"

    def test_cases_empty_directories_omitted(self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch) -> None:
        """--cases output omits directories that have no scenario files."""
        golden_dir = self._make_dir_with_scenario(tmp_path, "golden", "only-scenario")
        empty_dir = tmp_path / "pending-fix"
        empty_dir.mkdir()
        unsupported_dir = tmp_path / "unsupported"
        unsupported_dir.mkdir()
        monkeypatch.setattr(
            _sim,
            "STATE_DIRS",
            {"golden": golden_dir, "pending-fix": empty_dir, "unsupported": unsupported_dir},
        )
        monkeypatch.setattr(_sim, "SIMULATIONS_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["simulate.py", "--cases"])
        _sim.main()
        captured = capsys.readouterr()
        assert "golden" in captured.out.lower()
        # Empty directories should not produce section headers in the output
        assert "pending-fix" not in captured.out.lower()
        assert "unsupported" not in captured.out.lower()


# ---------------------------------------------------------------------------
# TestBackwardCompatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_existing_scenario_without_new_fields(self) -> None:
        """Load and run the real pending scenario — must produce valid results with no errors."""
        scenario_path = Path(__file__).parent.parent / "tools" / "simulations" / "pending" / "2026-03-28-overnight.json"
        assert scenario_path.exists(), f"Real pending scenario not found at {scenario_path}"
        # run_scenario with no state param — backward-compatible call signature
        result = run_scenario(scenario_path)
        assert isinstance(result, dict), "run_scenario must return a dict"
        assert "name" in result
        assert "assertions" in result
        assert "decisions" in result
        assert "passed" in result
        # No verdict in this scenario — must not crash and must return None/absent verdict
        assert result.get("verdict") is None

    def test_run_scenario_no_state_param(self, tmp_path: Path) -> None:
        """run_scenario(path) with no state kwarg runs without error."""
        p = _make_scenario(tmp_path, name="compat", assertions=[_passing_assertion()])
        result = run_scenario(p)
        assert result["passed"] is True

    def test_print_result_no_state_param(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """print_result(result) with no state kwarg runs without error."""
        p = _make_scenario(tmp_path, name="compat-print", assertions=[_passing_assertion()])
        result = run_scenario(p)
        print_result(result)  # no state kwarg — must not raise
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_simulator_core_nat_vent_logic(self) -> None:
        """ClimateSimulator core logic still works — nat vent activates when outdoor <= threshold."""
        config = {"comfort_cool": 72, "natural_vent_delta": 3.0}
        sim = ClimateSimulator(config)
        sim.process_event({"type": "temp_update", "time": "T1", "indoor_f": 74.0, "outdoor_f": 74.0})
        sim.process_event({"type": "sensor_open", "time": "T2", "entity": "binary_sensor.door"})
        assert sim.state.natural_vent_active, "nat vent should activate when outdoor <= threshold"

    def test_simulator_core_pause_logic(self) -> None:
        """ClimateSimulator core logic: pause when outdoor > threshold."""
        config = {"comfort_cool": 72, "natural_vent_delta": 3.0}
        sim = ClimateSimulator(config)
        # threshold = 75; outdoor 80 > 75 → pause
        sim.process_event({"type": "temp_update", "time": "T1", "indoor_f": 78.0, "outdoor_f": 80.0})
        sim.process_event({"type": "sensor_open", "time": "T2", "entity": "binary_sensor.door"})
        assert sim.state.paused_by_door, "should pause when outdoor exceeds threshold"
        assert not sim.state.natural_vent_active
