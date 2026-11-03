"""Real-HA tests for Issue #1245: the arrival-home restore row says what the thermostat was actually sent.

Occupant story (hvac_fan install): you come home on a mild evening while the blower is ventilating through an open
window. The thermostat correctly stays Off (Rule 3), but the Activity Report said "comfort restored" and the push
said "Restoring comfort temperature". Now the coordinator's event log carries ``occupancy_comfort_restore_held``
(reason ``blower_hold``), the household-promise journal records it (so P7 watches the house again once you are
home), and the push says comfort resumes once ventilation ends. The normal arrival (both fan modes) is the
control: one ``occupancy_comfort_restored`` row whose ``target_f`` is the setpoint the thermostat now holds.

Drives the real coordinator emit path (``coordinator._emit_event`` -> event log + promise journal) and the real
``_notify``; the unit-level matrix (WHF session, sleep band, aggressive savings, dry run) is in
``tests/test_restore_event_1245.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60
_MILD_EVENING = {
    "fan": "hvac_fan",
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 73.0,
    "outdoor_f": 66.0,
    "high_f": 68.0,
    "low_f": 62.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
}
# Warm afternoon: arrival restores the cool edge (same shape as tests_ha/test_return_home_off_day_1208.py).
_WARM_AFTERNOON = {
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 4, 5, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 73.0,
    "outdoor_f": 71.0,
    "high_f": 80.0,
    "low_f": 62.0,
    "initial_hvac_mode": "off",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
}


def _rows(house, kind: str) -> list[dict]:
    return [e for e in house.coordinator._event_log if e.get("type") == kind]


def _spy_notifications(engine) -> list[str]:
    sent: list[str] = []
    real_notify = engine._notify

    async def _spy(message, title, notification_type, **kwargs):
        sent.append(message)
        return await real_notify(message, title, notification_type, **kwargs)

    engine._notify = _spy
    return sent


async def test_arrival_while_the_blower_ventilates_reports_held_not_restored(make_house) -> None:
    house = await make_house(**_MILD_EVENING)
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    await engine.handle_occupancy_away()
    await house.advance(minutes=1)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active, "precondition: the open window started blower ventilation"
    assert house.thermostat.mode == "off", "precondition: Rule 3 keeps the thermostat Off"
    house.set_indoor(79.0)  # nearer the away setback than comfort, so the welcome push is not near-comfort suppressed
    await house.advance(seconds=30)
    sent = _spy_notifications(engine)

    await engine.handle_occupancy_home()
    await house.advance(seconds=30)

    assert house.thermostat.mode == "off", "rule 3: arrival armed heat/cool through the open window"
    assert _rows(house, "occupancy_comfort_restored") == [], "the report claims a restore the thermostat never got"
    held = _rows(house, "occupancy_comfort_restore_held")
    assert len(held) == 1 and held[0]["reason"] == "blower_hold", held
    marks = [m for m in house.coordinator._promise_journal._marks if m.kind == "occupancy_comfort_restore_held"]
    assert marks, "the promise journal did not record the held arrival (P7 would treat the occupant as away)"
    assert sent, "precondition: a welcome-home push was sent"
    assert not any("Restoring comfort temperature" in m for m in sent), sent
    assert any("resume once ventilation ends" in m for m in sent), sent


@pytest.mark.parametrize("fan", ["whf", "hvac_fan"])
async def test_normal_arrival_reports_the_setpoint_the_thermostat_holds(make_house, fan) -> None:
    house = await make_house(fan=fan, **_WARM_AFTERNOON)
    await house.advance(minutes=40)
    engine = house.coordinator.automation_engine
    await engine.handle_occupancy_away()
    await house.advance(minutes=60)

    await engine.handle_occupancy_home()
    await house.advance(seconds=30)

    restored = _rows(house, "occupancy_comfort_restored")
    assert len(restored) == 1, restored
    assert restored[0]["outcome"] == "sent"
    assert restored[0]["mode"] == house.thermostat.mode
    assert restored[0]["target_f"] == pytest.approx(house.thermostat.setpoint_f, abs=0.6)
    assert _rows(house, "occupancy_comfort_restore_held") == []
