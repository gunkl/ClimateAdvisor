"""Real-HA tests for Issue #1216: the occupant's SECOND thermostat change inside one override grace must be confirmed.

Occupant story: someone in the home changes the thermostat twice within one override grace -- Cool (confirmed), then
a few minutes later Off. Household Rule 2 says the newest occupant action wins, so the Off must be confirmed as the
override. Before the fix it was silently dropped and Climate Advisor went back to its own plan (it re-heated a house
the person had just switched off) as if the second press never happened.

It was dropped two ways, both pinned here (both failed on main before the fix):

1. Watchdog: the coordinator's mode-to-mode path (Fix D, "New mode change during active override grace") clears the
   first override but keeps its grace running for the new change, which is only PENDING confirmation
   (``_override_confirm_pending``) for the confirm period. The next coordinator update's ``_check_orphaned_grace()``
   sees ``_grace_active and _grace_protects_override`` with no override flag, logs ERROR "Stuck grace detected",
   cancels the grace and resolves the FSM as GRACE_TIMER_EXPIRED, which drops the pending change.
2. Mirror: the second change made close to the end of the first grace -- the first grace expires before the confirm
   period ends, and ``unprotected_grace_expiry_keeps_pending`` returned False for the occupant's own grace, so the
   pending change was cleared.

Both run in whole_house_fan and hvac_fan houses with no fan running (``_fan_override_active`` False, so the watchdog
can fire). The day is cold with CA heating, so Cool and Off are both genuine overrides of CA's plan. Test C is a
control: the first change alone is confirmed and its grace runs.

Harness notes: an occupant change must come more than 120 s after Climate Advisor's own last thermostat command, else
the coordinator reads it as the thermostat confirming that command.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_CONFIRM_S = 120
_STUCK_LOG = "Stuck grace detected: grace_active=True"
_FIX_D_LOG = "New mode change during active override grace"

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 68.0,
    "outdoor_f": 30.0,
    "high_f": 38.0,
    "low_f": 22.0,
    "initial_hvac_mode": "heat",
}


async def _steps(house, seconds: float, step: float = 10, *, cycles: bool = False) -> None:
    """Advance in 10 s steps; ``cycles``: every 30 s also run the REAL coordinator update (the watchdog lives there).

    Cycles stay off before the occupant's first press: each cycle re-sends CA's own set-point, and a press within 120 s
    of it would read as the thermostat confirming that command."""
    for i in range(1, int(seconds // step) + 1):
        await house.advance(seconds=step)
        if cycles and i % 3 == 0:
            await house.coordinator.async_refresh()
            await house.advance(seconds=0)


async def _house(make_house, fan: str, caplog, grace_s: int):
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    ca_config = {
        "fan_min_runtime_per_hour": 0,
        "sensor_debounce_seconds": 30,
        "manual_grace_seconds": grace_s,
        "override_confirm_seconds": _CONFIRM_S,
    }
    house = await make_house(fan=fan, ca_config=ca_config, **_HOUSE)
    await _steps(house, 360)  # past the start-up window and >120 s after CA's own commands
    return house


async def _confirmed_first_change(house) -> None:
    engine = house.coordinator.automation_engine
    await house.occupant_sets_thermostat(mode="cool")
    await _steps(house, _CONFIRM_S + 30)
    assert engine._manual_override_active and engine._manual_override_mode == "cool", "precondition: Cool confirmed"
    assert engine._grace_active, "precondition: the first override's grace runs"


def _assert_second_change_confirmed(house, caplog) -> None:
    engine = house.coordinator.automation_engine
    stuck = [r.getMessage() for r in caplog.records if _STUCK_LOG in r.getMessage()]
    assert not stuck, f"the watchdog killed the second change's grace: {stuck}"
    assert engine._manual_override_active and engine._manual_override_mode == "off", (
        f"the newest occupant action (Off) must be the confirmed override; got active={engine._manual_override_active} "
        f"mode={engine._manual_override_mode} pending={engine._override_confirm_pending}"
    )
    assert house.thermostat.mode == "off", f"thermostat must stay Off, is {house.thermostat.mode}"


@pytest.mark.parametrize("fan", _FANS)
async def test_control_first_change_confirmed_and_grace_runs(make_house, caplog, fan) -> None:
    house = await _house(make_house, fan, caplog, grace_s=900)
    engine = house.coordinator.automation_engine
    assert not engine._manual_override_active
    await _confirmed_first_change(house)
    assert house.thermostat.mode == "cool"
    assert engine._grace_protects_override
    assert not any(_STUCK_LOG in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("fan", _FANS)
async def test_second_change_survives_watchdog(make_house, caplog, fan) -> None:
    house = await _house(make_house, fan, caplog, grace_s=900)
    await _confirmed_first_change(house)
    await _steps(house, 180)  # a few minutes later
    await house.occupant_sets_thermostat(mode="off")
    await _steps(house, _CONFIRM_S + 60, cycles=True)
    assert any(_FIX_D_LOG in r.getMessage() for r in caplog.records), "precondition: Fix D path ran"
    _assert_second_change_confirmed(house, caplog)


@pytest.mark.parametrize("fan", _FANS)
async def test_second_change_survives_first_grace_expiry(make_house, caplog, fan) -> None:
    house = await _house(make_house, fan, caplog, grace_s=300)
    await _confirmed_first_change(house)  # 150 s into the 300 s grace
    engine = house.coordinator.automation_engine
    grace_end = dt_util.parse_datetime(str(engine._grace_end_time))
    assert grace_end is not None
    remaining = (grace_end - dt_util.utcnow()).total_seconds()
    await _steps(house, remaining - 60)
    await house.occupant_sets_thermostat(mode="off")
    # No coordinator cycles here: this isolates the expiry path from the watchdog (path 1).
    await _steps(house, _CONFIRM_S + 60)
    assert any(_FIX_D_LOG in r.getMessage() for r in caplog.records), "precondition: Fix D path ran"
    _assert_second_change_confirmed(house, caplog)
