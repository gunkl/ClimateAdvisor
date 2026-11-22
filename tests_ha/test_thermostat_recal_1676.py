"""Real-HA run for Issue #1676: the thermostat jumps 7 F the moment the furnace trips its high limit, then settles.

Occupant view: the hallway thermostat is heating a 68 F house when the furnace's high-limit safety cuts it; the
thermostat then reports 75 F for a few minutes and drifts back. The house is still 68 F. With a bedroom (sleep)
sensor configured Climate Advisor must read the room from it until the thermostat settles, say so once (a WARNING and
a Repairs notice the occupant can ignore), switch back afterwards, and learn nothing from the false readings. Without
another sensor it warns and keeps learning clean. An ignored notice stays ignored through the next episode.

The REAL coordinator, engine and thermostat listener run against the dev thermostat sim (``scripted``: the room only
moves when the test moves it, so the jump and its decay are the owner's shape: +7 F, 71 F after 3 min, 68 F after 6).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.climate_advisor.const import DOMAIN

pytestmark = pytest.mark.house

_SLEEP = "sensor.bedroom_temperature"
_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    "initial_indoor_f": 68.0,
    "outdoor_f": 40.0,
    "high_f": 42.0,
    "low_f": 35.0,
    "initial_hvac_mode": "heat",
}


def _events(coord, kind: str) -> list[dict]:
    return [e for e in coord._event_log if e.get("type") == kind]


async def _heat_until_running(house) -> None:
    for _ in range(40):
        if house.thermostat.action == "heating":
            return
        await house.advance(seconds=30)
    raise AssertionError(
        f"the sim never heated: action={house.thermostat.action} setpoint={house.thermostat.setpoint_f}"
    )


async def _trip_and_settle(house) -> dict:
    """Heating at 68 F -> the trip reads 75 F -> 71 F after 3 min -> 68 F after 6 min, then 8 quiet minutes."""
    coord = house.coordinator
    await _heat_until_running(house)
    await house.advance(seconds=60)
    house.set_indoor(75.0)
    await house.advance(seconds=10)
    during = {
        "active": coord._recal.active,
        "indoor": coord._get_indoor_temp(),
        "learning": coord._learning_indoor_f(),
        "status": coord._thermostat_recal_status(),
        "chart_false": coord._chart_recal_false(),
    }
    await house.advance(minutes=3)
    house.set_indoor(71.0)
    await house.advance(minutes=3)
    house.set_indoor(68.0)
    await house.advance(minutes=8)
    during["after_active"] = coord._recal.active
    during["after_indoor"] = coord._get_indoor_temp()
    return during


def _no_false_samples(coord) -> None:
    for obs in (coord._pending_observations or {}).values():
        for sample in obs.get("active_samples", []) + obs.get("samples", []):
            assert sample.get("indoor_temp_f", 0) < 74, sample


async def test_trip_with_a_bedroom_sensor_falls_back_and_returns(make_house, caplog) -> None:
    caplog.set_level(logging.INFO)
    house = await make_house(**_HOUSE, ca_config={"sleep_indoor_temp_entity": _SLEEP}, extra_states={_SLEEP: "68.4"})
    coord = house.coordinator
    result = await _trip_and_settle(house)

    assert result["active"] is True, result
    assert result["indoor"] == pytest.approx(68.4), result  # the bedroom, not the false 75 F
    assert result["learning"] is None and result["chart_false"], result
    assert result["status"]["entity"] == _SLEEP, result
    assert result["after_active"] is False and result["after_indoor"] == pytest.approx(68.0), result

    assert len(_events(coord, "thermostat_recalibration_started")) == 1
    assert [e["how"] for e in _events(coord, "thermostat_recalibration_ended")] == ["settled"]
    switches = [e["to_entity"] for e in _events(coord, "indoor_source_changed")]
    assert switches[-2:] == [_SLEEP, house.thermostat._state.entity_id], switches
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "recalibration detected" in r.message]
    assert len(warnings) == 1
    _no_false_samples(coord)

    issue_id = f"thermostat_recalibration_{house.ca_entry.entry_id}"
    issue = ir.async_get(house.hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_key == "thermostat_recalibration" and not issue.is_fixable
    # the occupant ignores it; the next episode keeps it ignored
    ir.async_ignore_issue(house.hass, DOMAIN, issue_id, True)
    await house.advance(minutes=20)
    await _trip_and_settle(house)
    assert len(_events(coord, "thermostat_recalibration_started")) == 2
    issue = ir.async_get(house.hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.dismissed_version is not None


async def test_trip_without_another_sensor_warns_only(make_house, caplog) -> None:
    caplog.set_level(logging.INFO)
    house = await make_house(**_HOUSE)
    coord = house.coordinator
    before = len(_events(coord, "indoor_source_changed"))
    result = await _trip_and_settle(house)

    assert result["active"] is True and result["indoor"] == pytest.approx(75.0), result  # nothing else to read
    assert result["learning"] is None and result["status"]["entity"] is None, result
    assert result["after_active"] is False, result
    assert len(_events(coord, "indoor_source_changed")) == before  # no switch without a sensor
    assert [e["how"] for e in _events(coord, "thermostat_recalibration_ended")] == ["settled"]
    assert any(r.levelno == logging.WARNING and "no other temperature sensor" in r.message for r in caplog.records)
    issue = ir.async_get(house.hass).async_get_issue(DOMAIN, f"thermostat_recalibration_{house.ca_entry.entry_id}")
    assert issue is not None and issue.translation_key == "thermostat_recalibration_no_alternate"
    _no_false_samples(coord)


async def test_guard_off_ignores_the_jump(make_house) -> None:
    house = await make_house(
        **_HOUSE,
        ca_config={"sleep_indoor_temp_entity": _SLEEP, "thermostat_recalibration_guard": False},
        extra_states={_SLEEP: "68.4"},
    )
    result = await _trip_and_settle(house)
    assert result["active"] is False and result["indoor"] == pytest.approx(75.0), result
    assert _events(house.coordinator, "thermostat_recalibration_started") == []
