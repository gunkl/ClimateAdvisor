"""Issue #1310: the FakeHass thermostat reports what a real one would be doing after Climate Advisor's commands.

Occupant view: in simulated days the AC counted as "running" whenever Climate Advisor put the thermostat in cool,
even with the house already below the setpoint, because the fake thermostat kept whatever ``hvac_action`` the
scenario last injected (e.g. ``"fan"``) and production's mode fallback then credited the idle time as AC runtime.
Inflated runtime hides (or invents) real-house runtime regressions such as #1240's phantom runtime.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness.fake_hass import FakeHass, FakeState  # noqa: E402
from tools.sim_harness.world import thermostat_running  # noqa: E402

ENTITY = "climate.t"


def _hass(mode: str, *, indoor: float, setpoint: float, action: str | None) -> FakeHass:
    hass = FakeHass()
    attrs: dict = {"current_temperature": indoor, "temperature": setpoint}
    if action is not None:
        attrs["hvac_action"] = action
    hass.states.set(ENTITY, FakeState(state=mode, attributes=attrs))
    return hass


def _call(hass: FakeHass, service: str, **data) -> str | None:
    asyncio.run(hass.services.async_call("climate", service, {"entity_id": ENTITY, **data}))
    return hass.states.get(ENTITY).attributes.get("hvac_action")


@pytest.mark.parametrize(
    ("mode", "indoor", "expected"),
    [
        ("cool", 70.0, "idle"),  # below the cool setpoint: a real AC is idle (the #1310 / scenario-912 case)
        ("cool", 72.5, "idle"),  # inside the deadband: not started yet
        ("cool", 74.0, "cooling"),  # 2 F above: starts
        ("heat", 74.0, "idle"),  # above the heat setpoint: the furnace is idle
        ("heat", 70.0, "heating"),  # 2 F below: starts
    ],
)
def test_mode_change_reports_idle_past_setpoint_and_running_otherwise(mode: str, indoor: float, expected: str) -> None:
    hass = _hass("fan_only", indoor=indoor, setpoint=72.0, action="fan")
    assert _call(hass, "set_hvac_mode", hvac_mode=mode) == expected


def test_set_temperature_with_mode_change_uses_the_new_setpoint() -> None:
    hass = _hass("off", indoor=70.0, setpoint=60.0, action="off")
    assert _call(hass, "set_temperature", hvac_mode="cool", temperature=72.0) == "idle"
    assert _call(hass, "set_temperature", hvac_mode="cool", temperature=68.0) == "cooling"


def test_running_session_keeps_hysteresis_then_stops_past_the_far_side() -> None:
    hass = _hass("off", indoor=74.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "cooling"
    # setpoint raised to 73: 74 is not yet 1.5 F below it, the running AC keeps going
    assert _call(hass, "set_temperature", temperature=73.0) == "cooling"
    # setpoint raised to 76: 74 <= 76 - 1.5, the AC stops
    assert _call(hass, "set_temperature", temperature=76.0) == "idle"


def test_switching_off_or_fan_only_stops_a_running_session() -> None:
    hass = _hass("off", indoor=75.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "cooling"
    assert _call(hass, "set_hvac_mode", hvac_mode="off") == "off"
    hass = _hass("off", indoor=68.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="heat") == "heating"
    assert _call(hass, "set_hvac_mode", hvac_mode="fan_only") == "fan"


def test_scenario_scripted_action_is_kept_in_the_same_mode() -> None:
    # A scenario that scripts "idle" (a thermostat that never starts, Issue #1194 e) keeps it on setpoint changes.
    hass = _hass("cool", indoor=74.0, setpoint=72.0, action="idle")
    assert _call(hass, "set_temperature", temperature=68.0) == "idle"


def test_thermostat_without_hvac_action_is_untouched() -> None:
    # No hvac_action attribute = a thermostat that reports none; production's mode fallback is the real behavior.
    hass = _hass("off", indoor=70.0, setpoint=72.0, action=None)
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") is None


def test_disabled_entity_is_untouched() -> None:
    hass = _hass("fan_only", indoor=70.0, setpoint=72.0, action="fan")
    hass.set_action_deadband(ENTITY, None)
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "fan"


def test_shared_rule_matches_the_real_ha_sim_thermostat_thresholds() -> None:
    # start at setpoint +/- deadband, stop at the far side (ca_dev_thermostat_sim _decide_compressor_state)
    assert thermostat_running("cool", 73.5, 72.0, None, start_f=1.5) == "cooling"
    assert thermostat_running("cool", 73.4, 72.0, None, start_f=1.5) is None
    assert thermostat_running("cool", 70.6, 72.0, "cooling", start_f=1.5) == "cooling"
    assert thermostat_running("cool", 70.5, 72.0, "cooling", start_f=1.5) is None
    assert thermostat_running("heat", 70.5, 72.0, None, start_f=1.5) == "heating"
    assert thermostat_running("heat", 73.5, 72.0, "heating", start_f=1.5) is None
    assert thermostat_running("heat", 73.0, 72.0, "cooling", start_f=1.5) is None  # mode change = fresh start


def test_scenario_912_ac_idle_below_setpoint_is_not_counted_as_runtime() -> None:
    """End to end through the real coordinator: CA resumes cool at 22:20:30 with the house at 70 F under a 72 F
    ceiling. Before #1310 the thermostat kept reporting "fan", the mode fallback counted ~100 min of AC runtime and
    the 23:59 watchdog fired (hvac_runtime_minutes 103.5). A real AC is idle there: no runtime, no watchdog."""
    from simulate import run_scenario_production  # noqa: PLC0415

    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    path = REPO_ROOT / "tools/simulations/pending/issue_912_fan_only_runtime_not_reported_as_hvac.json"
    scenario = json.loads(path.read_text(encoding="utf-8"))
    result = run_production_scenario(scenario, use_coordinator=True)
    assert not result.callback_errors
    watchdog = [p for t, p, _ in result.event_log if t == "thermal_learning_no_observations"]
    assert watchdog == [], f"idle AC counted as runtime: {watchdog}"
    assert run_scenario_production(path, state="pending")["passed"] is True


# --- Issue #1316 (mirror of #1310): a new indoor reading runs the thermostat's own loop again --------------------


def _reading(hass: FakeHass, indoor: float) -> str | None:
    from tools.sim_harness.run_production import _inject_indoor_temp  # noqa: PLC0415

    _inject_indoor_temp(hass, ENTITY, indoor)
    return hass.states.get(ENTITY).attributes.get("hvac_action")


def test_derived_idle_cool_starts_when_the_house_warms_and_stops_past_the_far_side() -> None:
    hass = _hass("off", indoor=70.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "idle"
    assert _reading(hass, 73.0) == "idle"  # inside the deadband
    assert _reading(hass, 73.5) == "cooling"  # the AC starts on its own
    assert _reading(hass, 71.0) == "cooling"  # hysteresis: still running
    assert _reading(hass, 70.5) == "idle"  # stops 1.5 F below the setpoint


def test_derived_idle_heat_starts_when_the_house_cools_and_stops_past_the_far_side() -> None:
    hass = _hass("off", indoor=74.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="heat") == "idle"
    assert _reading(hass, 70.5) == "heating"
    assert _reading(hass, 73.0) == "heating"
    assert _reading(hass, 73.5) == "idle"


def test_reading_leaves_a_scenario_scripted_action_alone() -> None:
    hass = _hass("cool", indoor=72.0, setpoint=72.0, action="idle")  # injected: a thermostat that never starts
    assert _reading(hass, 80.0) == "idle"
    hass = _hass("off", indoor=70.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "idle"
    hass.states.get(ENTITY).attributes["hvac_action"] = "fan"  # the scenario takes the action back
    assert _reading(hass, 80.0) == "fan"


def test_reading_leaves_attribute_less_and_disabled_thermostats_alone() -> None:
    hass = _hass("cool", indoor=70.0, setpoint=72.0, action=None)
    assert _reading(hass, 80.0) is None
    hass = _hass("off", indoor=70.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "idle"
    hass.set_action_deadband(ENTITY, None)
    assert _reading(hass, 80.0) == "idle"


def test_scenario_912_runtime_split_in_the_finalized_day_record() -> None:
    """#912's point through the day record production finalizes at 23:59: 15.5 fan_only minutes, 0 heat/cool."""
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    path = REPO_ROOT / "tools/simulations/pending/issue_912_fan_only_runtime_not_reported_as_hvac.json"
    result = run_production_scenario(json.loads(path.read_text(encoding="utf-8")), use_coordinator=True)
    day = result.engine_state["_finalized_days"]["2026-06-10"]
    assert day["thermostat_fan_only_runtime_minutes"] == 15.5
    assert day["hvac_runtime_minutes"] == 0.0


# --- Issue #1318 (occupant mirror): a scenario-injected mode change without an hvac_action -----------------------


def _occupant(hass: FakeHass, mode: str, **kw) -> str | None:
    from tools.sim_harness.run_production import _inject_thermostat_mode  # noqa: PLC0415

    _inject_thermostat_mode(hass, ENTITY, mode, **kw)
    return hass.states.get(ENTITY).attributes.get("hvac_action")


def test_occupant_turning_a_running_ac_off_or_to_fan_only_stops_it() -> None:
    hass = _hass("off", indoor=75.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "cooling"
    assert _occupant(hass, "off") == "off"
    hass = _hass("off", indoor=75.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "cooling"
    assert _occupant(hass, "fan_only") == "fan"


def test_occupant_switching_to_heat_or_cool_runs_the_thermostat_loop() -> None:
    hass = _hass("off", indoor=75.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "cooling"
    assert _occupant(hass, "heat") == "idle"  # 75 F is above the heat setpoint: fresh start, nothing runs
    assert _occupant(hass, "heat", temperature=78.0) == "heating"  # occupant asks for 78 F: 75 <= 78 - 1.5
    assert _occupant(hass, "heat", temperature=76.0) == "heating"  # same mode: hysteresis keeps it running


def test_occupant_change_leaves_scripted_attribute_less_and_disabled_thermostats_alone() -> None:
    hass = _hass("cool", indoor=75.0, setpoint=72.0, action="cooling")  # scenario-scripted
    assert _occupant(hass, "off") == "cooling"
    hass = _hass("cool", indoor=75.0, setpoint=72.0, action=None)
    assert _occupant(hass, "off") is None
    hass = _hass("off", indoor=75.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "cooling"
    hass.set_action_deadband(ENTITY, None)
    assert _occupant(hass, "off") == "cooling"
    hass = _hass("off", indoor=75.0, setpoint=72.0, action="off")
    assert _call(hass, "set_hvac_mode", hvac_mode="cool") == "cooling"
    assert _occupant(hass, "off", hvac_action="fan") == "fan"  # an injected action always wins
