"""Real-HA tests for Issue #1209: a thermostat blip that returns to the mode Climate Advisor set is not an override.

Occupant story: a warm day. The day label is "off" but the house is above the comfort ceiling, so Climate Advisor runs
cool on the comfort band. The thermostat briefly reports heat (a glitch) and is back on cool two minutes later, before
the override confirmation window ends. Nobody touched anything. Before the fix the window's expiry compared cool with
the "off" label, confirmed a manual override and froze comfort management for the whole manual-change pause (1 h).

Both fan modes (whole_house_fan and hvac_fan; ``both`` is unsupported), no fan running. The press is made more than
120 s after Climate Advisor's own last command (else the coordinator reads it as the thermostat confirming that
command) and after the 300 s start-up window. Controls: the same press that stays is confirmed; an occupant Off
(#1159) is confirmed.

Prove the tests are not vacuous with ``scripts/revert_proof.py`` (fail on the pre-fix tree, pass on HEAD).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_CONFIRM_S = 300

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 75.0,  # above comfort_cool (74): CA cools although the day label is "off"
    "outdoor_f": 68.0,
    "high_f": 80.0,
    "low_f": 62.0,
    "initial_hvac_mode": "cool",
}


async def _warm_day_house(make_house, fan: str):
    house = await make_house(
        fan=fan,
        ca_config={"fan_min_runtime_per_hour": 0, "manual_grace_seconds": 3600, "override_confirm_seconds": _CONFIRM_S},
        **_HOUSE,
    )
    await house.advance(minutes=8)  # past the start-up window and >120 s after CA's own commands
    engine = house.coordinator.automation_engine
    assert house.thermostat.mode == "cool", "precondition: CA is cooling the warm day"
    assert engine._current_classification.hvac_mode == "off", "precondition: the warm-day label is off"
    assert engine._last_commanded_hvac_mode == "cool", "precondition: CA commanded cool"
    assert not engine._manual_override_active
    return house, engine


@pytest.mark.parametrize("fan", _FANS)
async def test_blip_that_returns_to_the_commanded_mode_is_not_an_override(make_house, fan) -> None:
    house, engine = await _warm_day_house(make_house, fan)

    await house.occupant_sets_thermostat(mode="heat")
    await house.advance(seconds=120)
    assert engine._override_confirm_pending, "precondition: the blip started a confirmation window"
    await house.occupant_sets_thermostat(mode="cool")
    await house.advance(seconds=_CONFIRM_S + 60)

    assert not engine._override_confirm_pending
    assert not engine._manual_override_active, "a blip that returned to CA's own cool is not a manual override"
    assert not engine._grace_active, "no manual-change pause for a change nobody made"
    assert house.thermostat.mode == "cool"


@pytest.mark.parametrize("fan", _FANS)
async def test_control_change_that_stays_is_confirmed(make_house, fan) -> None:
    house, engine = await _warm_day_house(make_house, fan)

    await house.occupant_sets_thermostat(mode="heat")
    await house.advance(seconds=_CONFIRM_S + 60)

    assert engine._manual_override_active and engine._manual_override_mode == "heat"
    assert engine._grace_active
    assert house.thermostat.mode == "heat"


@pytest.mark.parametrize("fan", _FANS)
async def test_control_occupant_off_on_a_warm_day_is_confirmed(make_house, fan) -> None:
    """#1159 through the real stack: an Off is a decision, whatever the 'off' label says."""
    house, engine = await _warm_day_house(make_house, fan)

    await house.occupant_sets_thermostat(mode="off")
    await house.advance(seconds=_CONFIRM_S + 60)

    assert engine._manual_override_active and engine._manual_override_mode == "off"
    assert engine._grace_active
    assert house.thermostat.mode == "off"
