"""Real-HA test for Issue #1246: Climate Advisor's own FIRST mode write, reported late, is never the occupant.

Occupant story: a window is closed after a pause. Climate Advisor restores the pre-pause mode (heat), and because the
house drifted hot while paused, switches straight to cool. A cloud thermostat reports the first switch 30-120 s late
with none of Climate Advisor's Context. Climate Advisor used to remember only the LAST mode it commanded, so the late
"heat" read as the person choosing heat: a false "manual override" (automation held for the confirm window, or paused
for the 1 h manual grace). A person who REALLY picks a mode Climate Advisor did not command must still be caught.

The simulated thermostat reports ``set_hvac_mode`` / ``set_temperature`` after ``set_echo_delay()`` seconds. Both fan
configurations.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
SIM_DOMAIN = "ca_dev_thermostat_sim"

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 86.0,
    "outdoor_f": 92.0,
    "high_f": 95.0,
    "low_f": 75.0,
    "initial_hvac_mode": "off",
}
_CA_CONFIG = {"fan_min_runtime_per_hour": 0, "manual_grace_seconds": 3600, "override_confirm_seconds": 60}


async def _quiet_house(make_house, fan, **house_kw):
    house = await make_house(fan=fan, ca_config=dict(_CA_CONFIG), **{**_HOUSE, **house_kw})
    await house.advance(seconds=420)  # past the start-up window and CA's own start-up commands
    return house


def _sim(house):
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


async def _door_close_restore_with_flip(house, echo_delay_s: float) -> None:
    """Closing the window: restore 'heat', then the selector flips to 'cool'; the thermostat answers late."""
    engine = house.coordinator.automation_engine
    _sim(house).set_echo_delay(echo_delay_s)
    await engine._restore_comfort_after_door_window_close(restore_mode="heat", trigger="sensor_closed")
    await house._drain()
    _sim(house).set_echo_delay(0)
    modes = [w.mode for w in engine._own_mode_writes]
    assert modes[:2] == ["heat", "cool"], f"precondition: restore then family flip, got {modes}"


def _overrides(house) -> int:
    return house.coordinator._today_record.manual_overrides


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("echo_s", [45, 110])
async def test_late_first_mode_echo_after_family_flip_is_not_an_override(make_house, fan, echo_s) -> None:
    house = await _quiet_house(make_house, fan)
    engine = house.coordinator.automation_engine
    await _door_close_restore_with_flip(house, echo_s)
    await house.advance(seconds=echo_s + 20)
    assert _overrides(house) == 0, "CA's own late first mode echo was counted as the occupant"
    assert not engine._manual_override_active and not engine._override_confirm_pending
    await house.advance(minutes=5)
    assert not engine._manual_override_active and not engine._override_confirm_pending
    assert not engine._grace_active, "a false override paused automation for the manual grace period"


@pytest.mark.parametrize("fan", _FANS)
async def test_occupant_mode_change_inside_the_window_is_still_an_override(make_house, fan) -> None:
    house = await _quiet_house(make_house, fan, initial_hvac_mode="cool")
    engine = house.coordinator.automation_engine
    await _door_close_restore_with_flip(house, echo_delay_s=110)
    await house.advance(seconds=45)
    await house.occupant_sets_thermostat(mode="off")  # CA wrote heat and cool, never off
    await house.advance(seconds=120)
    assert engine._manual_override_active or engine._override_confirm_pending, (
        "the occupant mode change was swallowed as CA echo"
    )
