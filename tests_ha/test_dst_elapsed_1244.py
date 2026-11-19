"""Real-HA test for Issue #1244: the occupant's change in the repeated fall-back hour is the occupant, not an echo.

Occupant story (America/Los_Angeles, 2026-11-01, a cold night): a window is open and Climate Advisor pauses the heat
at about 01:40 PDT. The clocks go back at 02:00 PDT. At 01:10 PST -- 30 real minutes later -- the person turns the
heat back on at the thermostat. Climate Advisor used to compute "01:10 - 01:40 = -30 min", read that as "less than
3 seconds since my own command" and dropped the change as its own echo: no override, no grace, and the pause stayed.

The house runs in a real Home Assistant core whose time zone is Los Angeles, so ``dt_util.now()`` is the real local
``ZoneInfo`` clock (the unit tier, ``tests/test_dst_elapsed_1244.py``, covers the same guards with the stub clock).

Prove the test is not vacuous with ``scripts/revert_proof.py origin/main tests_ha/test_dst_elapsed_1244.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

_LA = "America/Los_Angeles"
_COLD_FALL_BACK_NIGHT = {
    "start": datetime(2026, 11, 1, 8, 30, tzinfo=UTC),  # 01:30 PDT
    "initial_indoor_f": 62.0,
    "outdoor_f": 38.0,
    "high_f": 44.0,
    "low_f": 28.0,
    "initial_hvac_mode": "heat",
    "time_zone": _LA,
}


@pytest.mark.parametrize("contact", ["window", "door"])
async def test_heat_press_in_the_repeated_hour_is_an_override(make_house, contact) -> None:
    house = await make_house(
        fan=None,
        contacts=[contact],
        startup="settled",
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "override_confirm_seconds": 600},
        **_COLD_FALL_BACK_NIGHT,
    )
    ae = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    await house.open(contact)
    await house.advance(minutes=2)
    assert ae._paused_by_door and house.thermostat.mode == "off", "precondition: CA paused the heat"
    paused_at = dt_util.as_local(ae._hvac_command_time)
    assert paused_at.utcoffset().total_seconds() == -7 * 3600, f"precondition: CA's command is PDT ({paused_at})"

    while dt_util.utcnow() < datetime(2026, 11, 1, 9, 10, tzinfo=UTC):  # 01:10 PST, the repeated hour
        await house.advance(minutes=1)
    now_local = dt_util.now()
    assert now_local.fold == 1 and now_local.hour == 1, f"precondition: inside the repeated hour ({now_local})"

    await house.occupant_sets_thermostat(mode="heat")
    await house.advance(minutes=14)  # past the 10-minute confirmation

    assert ae._manual_override_active, "the occupant's heat press was dropped as Climate Advisor's own echo"
    assert house.thermostat.mode == "heat"
    await house.close()
