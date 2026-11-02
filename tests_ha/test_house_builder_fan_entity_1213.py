"""Issue #1213 (test harness only): the house stores a fan config the real config flow can produce.

Nothing changes for anyone at home. The real config flow never stores ``fan_entity: None`` (an empty optional entity
selector is left out, the options flow pops cleared keys). The house builder used to, for ``disabled`` and
``hvac_fan``, so real-HA tests ran a config no install can have (and a stranded blower session crashed the fan-drift
tick until 1ea2c19 hardened it). Now the key is simply absent, and the drift tick runs clean on an hvac_fan house.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_HOUSE = {
    "startup": "settled",
    "start": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 78.0,
    "outdoor_f": 90.0,
    "high_f": 95.0,
    "low_f": 70.0,
    "initial_hvac_mode": "cool",
}


@pytest.mark.parametrize("fan", [None, "hvac_fan"], ids=["disabled", "hvac_fan"])
async def test_no_fan_entity_key_when_there_is_no_fan_entity(make_house, fan) -> None:
    house = await make_house(fan=fan, **_HOUSE)
    data = house.ca_entry.data
    assert "fan_entity" not in data, f"the config flow never stores fan_entity={data.get('fan_entity')!r}"
    await house.close()


async def test_whf_house_still_stores_its_fan_entity(make_house) -> None:
    house = await make_house(fan="whf", **_HOUSE)
    assert house.ca_entry.data["fan_entity"] == house.fan.command_entity
    await house.close()


async def test_hvac_fan_drift_tick_with_a_stranded_blower_session_runs_clean(make_house, caplog) -> None:
    house = await make_house(fan="hvac_fan", ca_config={"fan_min_runtime_per_hour": 0}, **_HOUSE)
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    engine._fan_active = True  # a stranded blower session: CA believes the blower runs, the thermostat says auto
    for _ in range(3):
        engine._reconcile_fan_physical_drift()
        await house.advance(minutes=5)
    assert "AttributeError" not in caplog.text
    await house.close()
