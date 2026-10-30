"""RealHASession adapter checks (Step 2 Increment 6): protocol conformance, dispatch report, effectiveness.

The goldens themselves run in ``test_goldens_realha.py``.  Here: the adapter implements the shared Session
protocol and returns the shared RunResult; the seam can be imported without any Home Assistant stub (finding F6);
and a deleted production behavior is seen by the real-HA run (the sabotage proof of design 8.5, exercise (a) of
the three-exercise protocol).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests_ha.realha_session import RealHASession, async_drive_scenario
from tools.sim_harness import divergence
from tools.sim_harness.scenario_model import ProductionRunResult
from tools.sim_harness.session import RunResult, Session, make_session

GOLDEN_DIR = divergence.GOLDEN_DIR
RUN_RESULT_FIELDS = ("event_log", "action_log", "engine_state", "callback_errors", "promise_report")


def _scenario(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_seam_imports_without_any_home_assistant_stub() -> None:
    """F6: the real-HA environment can import the seam; ``homeassistant`` here is the real package."""
    import homeassistant

    assert not isinstance(homeassistant, MagicMock)
    assert "tools.sim_harness.ha_stubs" not in sys.modules or "homeassistant.core" in sys.modules
    assert [f.name for f in dataclasses.fields(RunResult)] == list(RUN_RESULT_FIELDS)


def test_make_session_builds_the_real_ha_adapter(hass, freezer, tmp_path) -> None:
    session = make_session(
        _scenario("away_morning_wakeup_skipped_assertion"),
        engine="realha",
        hass=hass,
        freezer=freezer,
        config_dir=tmp_path,
    )
    assert isinstance(session, RealHASession)
    assert isinstance(session, Session)  # runtime protocol: feed / advance_to / now / thermostat / finish
    with pytest.raises(ValueError, match="unknown session engine"):
        make_session({}, engine="nope")


async def test_adapter_returns_the_shared_result_and_reports_dispatch_modes(hass, freezer, tmp_path) -> None:
    scenario = _scenario("away_morning_wakeup_skipped_assertion")
    session = make_session(scenario, engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
    result = await async_drive_scenario(session, scenario)
    assert isinstance(result, ProductionRunResult)
    assert result.event_log and result.action_log
    assert set(session.dispatch_modes) <= {
        "state-write",
        "service-occupant",
        "time-travel",
        "real-lifecycle",
        "direct-callback",
        "no-op",
    }
    assert sum(session.dispatch_modes.values()) == len(scenario["events"])
    judged = divergence.judge_scenario(scenario, result, use_coordinator=True)
    assert judged["passed"] is True, judged["assertions"]
    # service calls are in the FakeHass action_log shape, with the scenario's thermostat entity id
    first = result.action_log[0]
    assert {"domain", "service", "data", "ts", "context"} <= set(first)
    assert first["data"]["entity_id"] == scenario["config"].get("climate_entity", "climate.test_thermostat")


async def test_deleted_behavior_is_seen_by_the_real_ha_run(hass, freezer, tmp_path) -> None:
    """Sabotage proof: with ``apply_classification`` turned into a no-op the golden's first assertion fails.

    The golden asserts ``classification_applied`` at 08:00; if the real-HA run did not reflect production behavior
    this would still pass.  (Exercise (a): would the scenario fail if the named behavior was deleted?)
    """
    from custom_components.climate_advisor.automation import AutomationEngine

    scenario = _scenario("away_morning_wakeup_skipped_assertion")
    with patch.object(AutomationEngine, "apply_classification", AsyncMock(return_value=None)):
        session = make_session(scenario, engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
        result = await async_drive_scenario(session, scenario)
    judged = divergence.judge_scenario(scenario, result, use_coordinator=True)
    assert judged["passed"] is False
    assert any(r["pass"] is False for r in judged["assertions"])
