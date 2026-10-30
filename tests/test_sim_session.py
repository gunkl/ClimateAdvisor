"""The Session seam (Issue #1127 Increment 5 / #1128 I2).

The occupant never sees this layer; what it protects is that the SAME scenario runner can be driven
by the FakeHass adapter here and by the real-Home-Assistant adapter in the HA test tier, without
the scenario model, the Session protocol or the runner module installing Home Assistant stubs at
import time.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.sim_harness.run_production import FakeHassSession, run_production_scenario
from tools.sim_harness.scenario_model import ProductionRunResult, parse_event_time
from tools.sim_harness.session import Session, ThermostatView, drive_scenario, make_session

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tools" / "simulations" / "golden"
# A coordinator-mode golden (real listeners) and an engine-only one: both shapes of the adapter.
_COORDINATOR_GOLDEN = "override_second_override_during_grace"
_ENGINE_GOLDEN = "2026-03-28-overnight"


def _load(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _actions(log: list[dict]) -> list[dict]:
    """An action's ``context`` is a fresh object per run (identity only); compare that it was set."""
    return [{**a, "context": a.get("context") is not None} for a in log]


def _normalize(result: ProductionRunResult) -> tuple:
    return (result.event_log, _actions(result.action_log), result.engine_state, result.callback_errors)


class TestImportCleanliness:
    """Finding F6: the runner module must import in an environment that owns ``homeassistant.*``."""

    def _run(self, code: str) -> subprocess.CompletedProcess:
        preamble = f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r});"
        return subprocess.run(
            [sys.executable, "-I", "-c", preamble + code],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )

    def test_importing_the_runner_seam_installs_no_ha_stubs(self) -> None:
        proc = self._run(
            "import tools.sim_harness.scenario_model, tools.sim_harness.session, tools.sim_harness.run_production;"
            "print('homeassistant' in sys.modules)"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False", "importing the runner installed Home Assistant stubs"

    def test_the_fakehass_adapter_is_what_installs_them(self) -> None:
        golden = str(GOLDEN_DIR / f"{_ENGINE_GOLDEN}.json")
        code = (
            "import json, pathlib;"
            "from tools.sim_harness.run_production import FakeHassSession;"
            f"sc = json.loads(pathlib.Path({golden!r}).read_text(encoding='utf-8'));"
            "assert 'homeassistant' not in sys.modules;"
            "s = FakeHassSession(sc);"
            "print('homeassistant' in sys.modules); s.close()"
        )
        proc = self._run(code)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().splitlines()[-1] == "True"


class TestSessionProtocol:
    def test_fakehass_session_satisfies_the_protocol(self) -> None:
        with FakeHassSession(_load(_ENGINE_GOLDEN)) as session:
            assert isinstance(session, Session)
            assert isinstance(session.thermostat(), ThermostatView)

    def test_make_session_builds_the_fakehass_adapter(self) -> None:
        session = make_session(_load(_ENGINE_GOLDEN), engine="fakehass")
        try:
            assert isinstance(session, FakeHassSession)
        finally:
            session.close()

    def test_make_session_rejects_an_unknown_engine(self) -> None:
        with pytest.raises(ValueError, match="unknown session engine"):
            make_session(_load(_ENGINE_GOLDEN), engine="nope")

    @pytest.mark.parametrize("name", [_ENGINE_GOLDEN, _COORDINATOR_GOLDEN])
    def test_driving_the_seam_equals_the_public_runner(self, name: str) -> None:
        """``run_production_scenario`` is the seam: driving the Session by hand gives the same result."""
        scenario = _load(name)
        use_coordinator = bool(scenario.get("use_coordinator", False))
        via_runner = run_production_scenario(scenario, use_coordinator=use_coordinator)
        session = FakeHassSession(scenario, use_coordinator=use_coordinator)
        try:
            via_seam = drive_scenario(session, scenario)
        finally:
            session.close()
        assert _normalize(via_seam) == _normalize(via_runner)
        assert via_seam.promise_report is None  # no probe attached = no report

    def test_thermostat_view_reflects_the_world(self) -> None:
        scenario = _load(_COORDINATOR_GOLDEN)
        session = FakeHassSession(scenario, use_coordinator=True)
        try:
            for event in scenario["events"]:
                session.advance_to(parse_event_time(event["time"], session.now()))
                session.feed(event)
                if event["type"] == "thermostat_state_changed" and event["hvac_mode"] == "heat":
                    assert session.thermostat().mode == "heat"
            assert session.thermostat().mode == "off"
            assert session.thermostat().indoor_f == 73.0
        finally:
            session.close()

    def test_finish_is_idempotent_and_closes_the_clock_patches(self) -> None:
        session = FakeHassSession(_load(_ENGINE_GOLDEN))
        first = session.finish()
        assert session.finish() is first
        session.close()  # closing twice is harmless
