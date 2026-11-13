"""Issue #1537: the sim-harness thermostat reports ``hvac_action`` by default, like a real thermostat.

Occupant view: the household-promise checks no longer guess whether the heat or AC was running from the mode and the
room temperature (owner decision 2026-11-13: never infer heating/cooling from mode, fan, a missing action or room
temperature). For the goldens to keep judging "was the equipment already working?" honestly, the simulated thermostat
must say what its equipment is doing, as real ones do (owner decision 2026-11-13: "Sims report hvac_action"). The
value comes from the harness's own thermostat loop (``world.thermostat_running`` over the scripted room reading and
the setpoint), never from Climate Advisor's decisions, so a golden cannot pass by echoing production back to itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.fake_hass import FakeHass, FakeState  # noqa: E402
from tools.sim_harness.run_production import FakeHassSession, _inject_indoor_temp  # noqa: E402

ENTITY = "climate.t"
GOLDEN = REPO_ROOT / "tools" / "simulations" / "golden"
# Goldens that deliberately model a thermostat that does NOT report hvac_action (their fix is the no-hvac_action path).
OPT_OUT = (
    "tou_home_cool_bank_ends_no_hvac_action_1416",
    "tou_home_heat_bank_ends_no_hvac_action_1416",
    "hot_night_same_mode_pre_cool_wakeup_keeps_cool_1417",
)


def _seed(mode: str, **attrs) -> FakeHass:
    hass = FakeHass()
    hass.states.set(ENTITY, FakeState(state=mode, attributes=dict(attrs)))
    hass.services.seed_hvac_action(ENTITY, hass.states.get(ENTITY).attributes)
    return hass


def _action(hass: FakeHass) -> str | None:
    return hass.states.get(ENTITY).attributes.get("hvac_action")


@pytest.mark.parametrize(
    ("mode", "attrs", "expected"),
    [
        ("off", {"current_temperature": 60.0, "temperature": 70.0}, "off"),
        ("fan_only", {}, "fan"),
        ("heat", {"current_temperature": 66.0, "temperature": 70.0}, "heating"),  # 4 F under: the loop runs
        ("heat", {"current_temperature": 69.0, "temperature": 70.0}, "idle"),  # inside the deadband
        ("cool", {"current_temperature": 77.0, "temperature": 74.0}, "cooling"),
        ("cool", {"current_temperature": 77.0}, "idle"),  # no setpoint yet: nothing to run against
        ("heat_cool", {"current_temperature": 60.0, "target_temp_low": 68.0}, "idle"),  # not modelled: idle
    ],
)
def test_seed_comes_from_the_thermostats_own_loop(mode: str, attrs: dict, expected: str) -> None:
    assert _action(_seed(mode, **attrs)) == expected


def test_seeded_action_is_owned_by_the_thermostat_so_readings_rederive_it() -> None:
    hass = _seed("heat", current_temperature=69.0, temperature=70.0)
    assert _action(hass) == "idle"
    _inject_indoor_temp(hass, ENTITY, 68.0)  # 2 F under the setpoint: the furnace starts
    assert _action(hass) == "heating"
    _inject_indoor_temp(hass, ENTITY, 71.6)  # past the far side: it stops
    assert _action(hass) == "idle"


def test_a_scenario_supplied_action_is_left_alone() -> None:
    assert _action(_seed("heat", current_temperature=60.0, temperature=70.0, hvac_action="idle")) == "idle"


def _session_action(config: dict) -> str | None:
    scenario = {
        "name": "t1537",
        "config": config,
        "events": [{"time": "2026-01-15T06:00:00", "type": "temp_update", "indoor_f": 68.0, "outdoor_f": 30.0}],
    }
    session = FakeHassSession(scenario)
    try:
        st = session.fake_hass.states.get(session.climate_entity)
        return st.attributes.get("hvac_action") if st is not None else None
    finally:
        session.close()


def test_harness_thermostat_reports_hvac_action_by_default() -> None:
    assert _session_action({}) == "off"  # the harness thermostat starts in Off


def test_scenario_can_model_a_thermostat_without_hvac_action() -> None:
    assert _session_action({"thermostat_reports_hvac_action": False}) is None


@pytest.mark.parametrize("name", OPT_OUT)
def test_no_hvac_action_goldens_keep_their_opt_out(name: str) -> None:
    """Their fix is the path a NON-reporting thermostat takes (#1416/#1417): with a reported action the settling
    guard is satisfied by the action instead, and the golden would pass on the old code (effectiveness lost)."""
    cfg = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))["config"]
    assert cfg.get("thermostat_reports_hvac_action") is False


def test_only_the_listed_goldens_opt_out() -> None:
    opted = sorted(
        p.stem
        for p in GOLDEN.glob("*.json")
        if p.name != "MANIFEST.json"
        and json.loads(p.read_text(encoding="utf-8")).get("config", {}).get("thermostat_reports_hvac_action") is False
    )
    assert opted == sorted(OPT_OUT)
