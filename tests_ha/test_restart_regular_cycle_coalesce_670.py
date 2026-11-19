"""Issue #1604 (real HA): after a restart, a regular cycle inside the start-up window starts neither fan nor AC.

Occupant story: the window is open and the outdoor air turns cool while Home Assistant is down. When it comes back,
Climate Advisor's first update cycles run inside the 300 s start-up coalescing window, before the start-up reconcile
(``_do_startup_coalesce()``) has looked at the house. The regular-cycle checks that can switch real hardware on are
held back in that window (#670): ``_should_run_regular_cycle_nat_vent_check`` (free cooling: the whole-house fan or the
thermostat blower) and ``_should_run_regular_cycle_window_cooling_check`` (the hot-day economizer: AC cool-down with
the fan assisting). Without those gates the fan (or the AC) starts on the first cycle, minutes before the reconcile
whose job that decision is, against a fan state the reconcile never chose (on the live incident the reconcile's own
decision arrived minutes late). After the window, starting is allowed: the reconcile does it.

* ``test_nat_vent_waits_for_the_startup_window``: a warm day (the economizer is hot-day only, so only the nat-vent
  gate is in play). Remove the coalesce condition from ``_should_run_regular_cycle_nat_vent_check`` and it fails.
* ``test_window_cooling_waits_for_the_startup_window``: a hot evening inside the economizer's hours. The
  window-cooling gate is the SECOND line here: after a restart the engine has no classification until the reconcile
  applies one (``_should_run_regular_cycle_apply_classification``, #978, holds the regular cycle's
  ``apply_classification()`` back), and the economizer does nothing without a hot-day classification. So removing
  only the coalesce condition from ``_should_run_regular_cycle_window_cooling_check`` does not fail this test;
  removing it together with the #978 gate fails it (both fan modes), and removing the #978 gate alone does not
  (Issue #1604 evidence). On a whole-house-fan house, removing the nat-vent gate fails it too (free cooling is what
  would start).

Fan modes: the window-cooling test runs on both (hvac_fan: the economizer runs the blower after the window; whf: free
cooling takes the house after the window). The nat-vent test is whole_house_fan only: with an hvac_fan house an open
window gets the door/window pause, not free cooling (the blower draws no outdoor air), so there is nothing to start.
The cool air arrives while HA is down (``House.restart(while_down=...)``), so no listener saw it and
only a regular cycle or the reconcile can act on it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests_ha.house.builder import STARTUP_WINDOW_S

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_IN_WINDOW_S = 120
_CFG = {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30}
# Climate Advisor reads the outdoor temperature from the hourly forecast (a low-at-05:00 / high-at-15:00 curve built
# from ``high_f``/``low_f``), so the air turning cool while HA is down is a new forecast envelope.
_WARM = {  # warm day at 20:00 (outdoor ~80 F, warmer than the 76 F house): the economizer is hot-day only
    "start": datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
    "initial_indoor_f": 76.0,
    "outdoor_f": 80.0,
    "high_f": 84.0,
    "low_f": 74.0,
}
_WARM_COOL_AIR = (80.0, 56.0)  # still a warm day; ~71 F at 20:00, 5 F below the house: free cooling warranted
_HOT_EVENING = {  # hot day at 21:30, inside the economizer's evening hours (outdoor ~89 F)
    "start": datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
    "initial_indoor_f": 80.0,
    "outdoor_f": 89.0,
    "high_f": 100.0,
    "low_f": 80.0,
}
_HOT_COOL_AIR = (88.0, 64.0)  # still a hot day; ~74 F at 21:30: within comfort_cool + delta, below the house


def _ca_starts(house, since: int) -> list:
    """Climate Advisor switching a fan or the AC on: the whole-house fan, the thermostat blower, or cool mode."""
    out = []
    for c in house.calls[since:]:
        if not c.origin.startswith("ca:"):
            continue
        if (
            c.domain == "fan"
            and c.service == "turn_on"
            or c.domain == "climate"
            and c.service == "set_fan_mode"
            and c.data.get("fan_mode") == "on"
            or c.domain == "climate"
            and c.data.get("hvac_mode") == "cool"
        ):
            out.append(c)
    return out


def _fan_running(house, fan: str) -> bool:
    return house.fan.is_on if fan == "whf" else house.thermostat.fan_mode == "on"


async def _restart_into_cool_air(make_house, fan: str, day: dict, cool_air: tuple[float, float]):
    house = await make_house(
        fan=fan,
        contacts=["window"],
        startup="settled",
        profile="scripted",
        initial_hvac_mode="cool",
        ca_config=_CFG,
        **day,
    )
    await house.open("window")
    await house.advance(minutes=2)
    assert not _fan_running(house, fan), "precondition: outdoor is warmer, so nothing ventilates"

    async def _cool_air_while_down(h) -> None:
        high, low = cool_air
        h.set_outdoor((high + low) / 2.0, high_f=high, low_f=low)

    mark = len(house.calls)
    await house.restart(settle=False, while_down=_cool_air_while_down, down_for_seconds=60)
    coord = house.coordinator
    assert coord._startup_coalesce_active, "precondition: the cycles below run inside start-up coalescing"
    await coord.async_refresh()  # a regular cycle, inside the window
    await house.advance(seconds=_IN_WINDOW_S)
    assert coord._startup_coalesce_active
    return house, mark


@pytest.mark.parametrize("fan", ["whf"])
async def test_nat_vent_waits_for_the_startup_window(make_house, fan) -> None:
    house, mark = await _restart_into_cool_air(make_house, fan, _WARM, _WARM_COOL_AIR)
    assert not _ca_starts(house, mark), "#670: a regular cycle started free cooling inside the start-up window"
    assert not _fan_running(house, fan)

    await house.advance(seconds=STARTUP_WINDOW_S)
    assert not house.coordinator._startup_coalesce_active
    assert house.coordinator.automation_engine._natural_vent_active, "after the window the reconcile starts it"
    assert _fan_running(house, fan)


@pytest.mark.parametrize("fan", _FANS)
async def test_window_cooling_waits_for_the_startup_window(make_house, fan) -> None:
    house, mark = await _restart_into_cool_air(make_house, fan, _HOT_EVENING, _HOT_COOL_AIR)
    assert not _ca_starts(house, mark), "#670: a regular cycle started window cooling inside the start-up window"
    assert not house.coordinator.automation_engine._economizer_active

    await house.advance(seconds=STARTUP_WINDOW_S)
    assert not house.coordinator._startup_coalesce_active
    assert _ca_starts(house, mark), "after the window Climate Advisor may use the cool evening air"
