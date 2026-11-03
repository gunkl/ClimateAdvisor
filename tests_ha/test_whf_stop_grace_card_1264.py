"""Real-HA test for Issue #1264: the Status card drops the grace as soon as the sealed-house stop ends it.

Occupant story (2026-11-02, v0.8.32): she presses the whole-house fan on with every window closed. Climate Advisor
stops it ~10 s later (Rule 1) and ends the fan override and its grace, but ``sensor.climate_advisor_status`` kept
reading "grace period (manual) — WHF override — 3h (ends 5:52 PM)" for another 15-26 s: the stop runs from the
settle timer, the transmitter's off reading requests no refresh on a dual-entity install, and the cards read
``coordinator.data``, so the card waited for the power entity's lagging off reading (~15.5 s) to refresh it.

The ``whf_dual`` house with the production-like lag is the only shape where the late refresh is the power reading,
so the bug is exercised there. Prove the test is not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.const import ATTR_AUTOMATION_STATUS, ATTR_FAN_STATUS

pytestmark = pytest.mark.house

REMOTE = "event.house_quietcool_remote"
OFF_LAG_S = 15.5  # production power-detect lag after the last off (see test_whf_sealed_stop_1215.py)


def _ca_fan_off(calls):
    return [c for c in calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_off"]


async def _seconds(house, n: int) -> None:
    for _ in range(n):
        await house.advance(seconds=1)


async def test_sealed_stop_card_stops_showing_the_grace_before_the_power_reading_lands(make_house) -> None:
    house = await make_house(
        fan="whf_dual",
        contacts=["window"],
        startup="settled",
        initial_indoor_f=67.0,
        outdoor_f=66.0,
        high_f=69.0,
        low_f=47.0,
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "fan_remote_entity": REMOTE},
    )
    house.fan.enable_realistic_lag(remote_updates_command_entity=True)
    ae = house.coordinator.automation_engine
    try:
        await house.advance(minutes=3)  # past every start-up / expected-confirmation window
        await house.coordinator.async_request_refresh()
        await house.hass.async_block_till_done()
        await house.advance(seconds=3)
        calls_before = len(house.calls)
        await house.occupant_presses_fan(True)
        await _seconds(house, 8)
        assert ae._fan_override_active and ae._grace_active, "precondition: her press started the fan grace"
        assert "grace" in str(house.coordinator.data[ATTR_AUTOMATION_STATUS]), "precondition: the card shows it"

        stop_at = None
        for _ in range(20):  # the 10 s settle passes and CA stops the fan
            await _seconds(house, 1)
            stop = _ca_fan_off(house.calls[calls_before:])
            if stop:
                stop_at = stop[0].at
                break
        assert stop_at is not None, "precondition: CA stopped the fan on the sealed house"
        await _seconds(house, 3)
        assert (dt_util.utcnow() - stop_at).total_seconds() < OFF_LAG_S, "must look before the power reading lands"

        assert ae._grace_active is False, "the engine ended her grace with the stop"
        status = str(house.coordinator.data[ATTR_AUTOMATION_STATUS])
        fan_status = str(house.coordinator.data[ATTR_FAN_STATUS])
        assert "grace" not in status, f"the Status card still shows the cancelled grace: {status!r}"
        assert "manual override" not in fan_status, f"the Fan card still shows her override: {fan_status!r}"
    finally:
        house.fan.close()
        await house.open("window")
        await _seconds(house, 20)
