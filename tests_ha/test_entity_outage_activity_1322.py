"""Real-HA test for Issue #1322: a weather-source outage and its recovery appear in the Activity log.

Occupant story (2026-11-03/04): the weather source went unavailable three times overnight. Climate Advisor retried
and recovered on its own, and the dashboard warning came and went, but the Activity log recorded nothing. Now an
outage row ("… unavailable") and a recovery row ("… available again") are recorded, and the dashboard data's warning
clears on recovery. Prove a test is not vacuous with ``scripts/revert_proof.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house


def _rows(house, kind: str) -> list:
    return [e for e in house.coordinator._event_log if e.get("type") == kind]


async def test_weather_outage_and_recovery_are_recorded_and_the_warning_clears(make_house) -> None:
    house = await make_house(
        startup="settled",
        start=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        initial_indoor_f=67.0,
        outdoor_f=58.0,
        high_f=66.0,
        low_f=50.0,
        initial_hvac_mode="heat",
    )
    await house.advance(minutes=6)  # past the 300 s start-up window, where entity health is suppressed
    assert house.weather is not None, "precondition: the house has a weather entity"
    weather_id = house.weather.entity_id

    house.weather._attr_available = False
    house.weather.async_write_ha_state()
    await house.advance(minutes=31)  # the next 30-minute cycle sees it

    down = _rows(house, "entity_unavailable")
    assert [e["entity_id"] for e in down] == [weather_id], "the outage was not recorded once"
    assert down[0]["status"] == "unavailable"
    issues = house.coordinator.data.get("entity_health_issues") or []
    assert any(i["entity_id"] == weather_id for i in issues), "precondition: the dashboard warning shows"

    await house.advance(minutes=30)  # still out: no second outage row
    assert len(_rows(house, "entity_unavailable")) == 1

    house.weather._attr_available = True
    house.weather.async_write_ha_state()
    await house.advance(minutes=31)

    up = _rows(house, "entity_recovered")
    assert [e["entity_id"] for e in up] == [weather_id], "the recovery was not recorded"
    assert up[0]["outage_minutes"] >= 60
    issues = house.coordinator.data.get("entity_health_issues") or []
    assert not any(i["entity_id"] == weather_id for i in issues), "the dashboard warning did not clear"
