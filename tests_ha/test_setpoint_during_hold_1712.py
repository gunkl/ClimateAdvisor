"""Issue #1712 (real HA): a setpoint change made while an override hold is running is the occupant's newest choice.

Occupant story (Mon 2026-11-23, heating): the person set the thermostat to 69 F; Climate Advisor confirmed it and held
it for 3 hours. Inside that hold they lowered it to 67 F, then raised it back to 69 F. Neither change was recorded,
so when the FIRST hold ran out seven minutes after the 69 F, Climate Advisor put the schedule's 68 F back over it.

The setpoint-only detector registered a change only when no override was active or pending. #201/#1216 made a second
MODE change supersede the first; a same-mode setpoint change never reached that branch. Now a setpoint change during
a confirmed hold supersedes it (Fix D, the old grace keeps running until the new one is confirmed) and one during the
confirmation window restarts it, so the newest change gets its own full hold. Both fan configurations.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_CONFIRM_S = 600
_GRACE_S = 1800

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 67.0,
    "outdoor_f": 40.0,
    "high_f": 50.0,
    "low_f": 35.0,
    "initial_hvac_mode": "heat",
}
_CA_CONFIG = {
    "fan_min_runtime_per_hour": 0,
    "manual_grace_seconds": _GRACE_S,
    "override_confirm_seconds": _CONFIRM_S,
}


async def _heating_house(make_house, fan):
    house = await make_house(fan=fan, ca_config=dict(_CA_CONFIG), **_HOUSE)
    await house.advance(seconds=420)  # past the start-up window and CA's own start-up commands
    plan_f = house.thermostat.setpoint_f
    assert house.thermostat.mode == "heat" and plan_f is not None, "precondition: CA heats on its plan"
    return house, plan_f


def _events(house, kind: str) -> list[dict]:
    return [e for e in house.coordinator._event_log if e.get("type") == kind]


async def _first_hold(house, plan_f: float) -> float:
    """The occupant sets plan + 1 F; it is confirmed and its hold starts. Returns the seconds left in that hold."""
    engine = house.coordinator.automation_engine
    await house.occupant_sets_thermostat(setpoint_f=plan_f + 1)
    await house.advance(seconds=_CONFIRM_S + 30)
    assert engine._manual_override_active and engine._grace_active, "precondition: the first change is held"
    return _GRACE_S - 30


@pytest.mark.parametrize("fan", _FANS)
async def test_control_a_single_change_ends_with_its_hold(make_house, fan) -> None:
    """Control: with no second change, the plan comes back when the hold ends (what the fix must not change)."""
    house, plan_f = await _heating_house(make_house, fan)
    left = await _first_hold(house, plan_f)
    await house.advance(seconds=left + 60)
    assert not house.coordinator.automation_engine._manual_override_active
    assert house.thermostat.setpoint_f == pytest.approx(plan_f), "the plan did not resume after the hold"


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("delta_f", [+2.0, -2.0], ids=["raised", "lowered"])
async def test_a_setpoint_change_inside_the_hold_outlives_the_first_hold(make_house, fan, delta_f) -> None:
    """Changed 5 minutes before the first hold ends: that hold's end must not put the plan back over it."""
    house, plan_f = await _heating_house(make_house, fan)
    engine = house.coordinator.automation_engine
    left = await _first_hold(house, plan_f)
    await house.advance(seconds=left - 300)
    detected_before = len(_events(house, "override_detected"))
    newest_f = plan_f + delta_f

    await house.occupant_sets_thermostat(setpoint_f=newest_f)
    await house.advance(seconds=30)
    assert len(_events(house, "override_detected")) == detected_before + 1, "the newest change was not recorded"
    assert engine._override_confirm_pending, "the newest change must be pending its own confirmation"

    await house.advance(seconds=300 + 60)  # the first hold's end has passed
    assert house.thermostat.setpoint_f == pytest.approx(newest_f), (
        f"#1712: the first hold's expiry put the plan ({plan_f} F) back over the occupant's newest {newest_f} F"
    )

    await house.advance(seconds=_CONFIRM_S - 390 + 30)  # the newest change's own confirmation (600 s after it)
    assert engine._manual_override_active and engine._grace_active, "the newest change got no hold of its own"
    await house.advance(seconds=_GRACE_S - 150)  # 2 minutes before the newest hold ends
    assert house.thermostat.setpoint_f == pytest.approx(newest_f), "the newest change's hold did not run in full"
    await house.advance(seconds=600)
    assert house.thermostat.setpoint_f == pytest.approx(plan_f), "the plan must resume when the newest hold ends"


@pytest.mark.parametrize("fan", _FANS)
async def test_a_setpoint_change_during_the_confirmation_restarts_it(make_house, fan) -> None:
    """Mirror (pending side): a second setpoint inside the confirmation window is recorded and restarts the window."""
    house, plan_f = await _heating_house(make_house, fan)
    engine = house.coordinator.automation_engine
    await house.occupant_sets_thermostat(setpoint_f=plan_f + 1)
    await house.advance(seconds=300)
    assert engine._override_confirm_pending and not engine._manual_override_active, "precondition: pending"
    detected_before = len(_events(house, "override_detected"))

    await house.occupant_sets_thermostat(setpoint_f=plan_f + 2)
    await house.advance(seconds=30)
    assert len(_events(house, "override_detected")) == detected_before + 1, "the second change was not recorded"
    await house.advance(seconds=_CONFIRM_S - 300)  # the FIRST change's window would have ended here
    assert engine._override_confirm_pending and not engine._manual_override_active, "the window did not restart"
    await house.advance(seconds=300)
    assert engine._manual_override_active and engine._grace_active
    assert house.thermostat.setpoint_f == pytest.approx(plan_f + 2)


@pytest.mark.parametrize("fan", _FANS)
async def test_a_setpoint_change_after_switching_a_pending_mode_back_is_kept(make_house, fan) -> None:
    """Mirror: Off (pending), back to Heat, then a new setpoint -- the setpoint is the newest change and is held."""
    house, plan_f = await _heating_house(make_house, fan)
    engine = house.coordinator.automation_engine
    await house.occupant_sets_thermostat(mode="off")
    await house.advance(seconds=60)
    assert engine._override_confirm_pending and engine._override_confirm_mode == "off", "precondition: Off pending"
    await house.occupant_sets_thermostat(mode="heat")
    await house.advance(seconds=60)
    await house.occupant_sets_thermostat(setpoint_f=plan_f + 2)
    await house.advance(seconds=30)
    assert engine._override_confirm_pending and engine._override_confirm_source == "setpoint", (
        "the new setpoint was dropped under the abandoned Off"
    )
    await house.advance(seconds=_CONFIRM_S + 30)
    assert engine._manual_override_active and engine._manual_override_mode == "heat" and engine._grace_active
    assert house.thermostat.setpoint_f == pytest.approx(plan_f + 2)
