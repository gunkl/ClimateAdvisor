"""Real-HA tests for Issue #1367: a setpoint outside the thermostat's own limits.

Occupant story: everyone leaves and the away heat setpoint is 42 F, but the thermostat only accepts 45 F and up
(its ``min_temp``). Home Assistant's ``climate.set_temperature`` refuses a value outside ``min_temp``/``max_temp``
(``ServiceValidationError``, ``temp_out_of_range``) before the thermostat sees it. Climate Advisor classified that as
"rejected by validation, skipped", so the thermostat KEPT the comfort setpoint it had: the empty house was heated
(or cooled) to comfort for the whole absence, and every later re-apply was refused again. No override or grace
started (the refused write never reached the thermostat, so nothing echoed), but the plan was not followed either.

Now Climate Advisor sends the setpoint already fitted to the thermostat's advertised limits (45 F here), so the
house gets the deepest setback the thermostat allows, the read-back confirms it, and nothing reads it as the occupant.

Run in both fan configurations; heat against ``min_temp`` and cool against ``max_temp``; away and vacation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
SIM_DOMAIN = "ca_dev_thermostat_sim"
_MIN_F = 45.0
_MAX_F = 85.0

_WINTER = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 15, 0, tzinfo=UTC),
    "initial_indoor_f": 68.0,
    "outdoor_f": 30.0,
    "high_f": 38.0,
    "low_f": 22.0,
    "initial_hvac_mode": "heat",
}
_SUMMER = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 7, 15, 18, 0, tzinfo=UTC),
    "initial_indoor_f": 76.0,
    "outdoor_f": 95.0,
    "high_f": 100.0,
    "low_f": 75.0,
    "initial_hvac_mode": "cool",
}
_BASE = {"fan_min_runtime_per_hour": 0, "manual_grace_seconds": 3600, "override_confirm_seconds": 60}


def _sim(house):
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


def _narrow_limits(house) -> None:
    """Give the thermostat a 45-85 F range (a real device's advertised limits)."""
    sim = _sim(house)
    sim._attr_min_temp = _MIN_F
    sim._attr_max_temp = _MAX_F
    for cached in ("min_temp", "max_temp"):
        sim.__dict__.pop(cached, None)
    sim.async_write_ha_state()
    attrs = house.hass.states.get(sim.entity_id).attributes
    assert (attrs["min_temp"], attrs["max_temp"]) == (_MIN_F, _MAX_F), attrs


def _ca_setpoint_calls(house) -> list:
    return [
        c
        for c in house.calls
        if getattr(c, "domain", None) == "climate" and getattr(c, "service", None) == "set_temperature"
    ]


async def _leave(house, handler: str):
    engine = house.coordinator.automation_engine
    await house.advance(seconds=420)  # past the start-up window and CA's own start-up commands
    _narrow_limits(house)
    await getattr(engine, handler)()
    await house._drain()
    return engine


def _assert_clean(house, engine) -> None:
    assert house.coordinator._today_record.manual_overrides == 0, "the fitted setpoint was read as the occupant"
    assert not engine._manual_override_active and not engine._override_confirm_pending
    assert not engine._grace_active
    assert engine._setpoint_reject_streak == 0, "the read-back called the fitted setpoint a rejection"


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("handler", ["handle_occupancy_away", "handle_occupancy_vacation"])
async def test_heat_setback_below_min_temp_lands_at_min_temp(make_house, fan, handler) -> None:
    house = await make_house(fan=fan, ca_config={**_BASE, "setback_heat": 42.0, "setback_cool": 80.0}, **_WINTER)
    engine = await _leave(house, handler)
    assert house.thermostat.setpoint_f == _MIN_F, (
        f"the setback was refused: thermostat kept {house.thermostat.setpoint_f} F while the house is empty"
    )
    await house.advance(minutes=30)  # read-back, echo window, the 15-minute retry slot, periodic cycles
    assert house.thermostat.setpoint_f == _MIN_F
    _assert_clean(house, engine)
    # settled (G2): every later re-apply sends the same fitted value -- never the refused one, never alternating.
    # (How OFTEN the away pass re-applies is Issue #1363, separate and unchanged here.)
    before = len(_ca_setpoint_calls(house))
    await house.advance(minutes=20)
    assert house.thermostat.setpoint_f == _MIN_F
    _assert_clean(house, engine)
    sent = {c.data.get("temperature") for c in _ca_setpoint_calls(house)[before:]}
    assert sent <= {_MIN_F}, f"Climate Advisor sent {sent} after the fitted setback held"


@pytest.mark.parametrize("fan", _FANS)
async def test_cool_setback_above_max_temp_lands_at_max_temp(make_house, fan) -> None:
    house = await make_house(fan=fan, ca_config={**_BASE, "setback_heat": 60.0, "setback_cool": 88.0}, **_SUMMER)
    engine = await _leave(house, "handle_occupancy_away")
    assert house.thermostat.setpoint_f == _MAX_F, (
        f"the setback was refused: thermostat kept {house.thermostat.setpoint_f} F while the house is empty"
    )
    await house.advance(minutes=30)
    assert house.thermostat.setpoint_f == _MAX_F
    _assert_clean(house, engine)


@pytest.mark.parametrize("fan", _FANS)
async def test_setpoint_inside_limits_is_sent_unchanged(make_house, fan) -> None:
    """Control: an away heat setpoint inside the thermostat's range is written exactly as configured."""
    house = await make_house(fan=fan, ca_config={**_BASE, "setback_heat": 55.0, "setback_cool": 80.0}, **_WINTER)
    engine = await _leave(house, "handle_occupancy_away")
    assert house.thermostat.setpoint_f == 55.0
    await house.advance(minutes=20)
    assert house.thermostat.setpoint_f == 55.0
    _assert_clean(house, engine)
