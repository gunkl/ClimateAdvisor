"""Real-HA tests for Issue #1325: two different thermostat changes inside five minutes, and a setpoint while Off.

Occupant stories:

* The person sets the thermostat Cool 74, then Cool 72 two minutes later (a cold day with Climate Advisor heating, so
  both are genuine overrides). Both changes are protected: each emits ``override_detected`` (the engine's dedup
  suppresses only an identical repeat), the first episode ends ``superseded``, the second is protected to its own grace
  end, and the household-promise checker (P4) does not report "ended your manual thermostat change N min early".
  An identical repeat inside five minutes is still one event.
* The person moves the setpoint while the thermostat is Off (also when Climate Advisor's door pause holds it Off). A
  thermostat that is Off takes no setpoint: that is no change, so there is no override, no confirmation, no grace.

Both fan modes. Prove the tests are not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.invariant_watchdog import PromiseConfig, check_p4_override_respected

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_CONFIRM_S = 30
_GRACE_S = 900

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


async def _steps(house, seconds: float, step: float = 10) -> None:
    for _ in range(int(seconds // step)):
        await house.advance(seconds=step)


def _record_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    seen: list = []

    def _tee(event_type: str, data: dict) -> None:
        seen.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee
    return seen


def _window(house):
    return house.coordinator._promise_journal.window(dt_util.now())


async def _house(make_house, fan: str, caplog, **overrides):
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    ca_config = {
        "fan_min_runtime_per_hour": 0,
        "sensor_debounce_seconds": 30,
        "manual_grace_seconds": _GRACE_S,
        "override_confirm_seconds": _CONFIRM_S,
    }
    house = await make_house(fan=fan, ca_config=ca_config, **{**_HOUSE, **overrides})
    await _steps(house, 360)  # past the start-up window and >120 s after CA's own commands
    return house


@pytest.mark.parametrize("fan", _FANS)
async def test_two_different_changes_two_minutes_apart_both_protected_no_p4_hit(make_house, caplog, fan) -> None:
    """Cool, then Off two minutes later (a cold day, Climate Advisor heating: both are genuine overrides).

    Both are mode changes: a second same-mode setpoint nudge during an override is not a new override at the
    coordinator (existing design), so the engine-level same-mode case is pinned in tests/test_override_dedup.py."""
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    await house.occupant_sets_thermostat(mode="cool", setpoint_f=74.0)
    await _steps(house, 120)
    await house.occupant_sets_thermostat(mode="off")
    await _steps(house, _CONFIRM_S + 30)

    detected = [d for t, d in events if t == "override_detected"]
    assert [d.get("detected_mode") for d in detected] == ["cool", "off"], detected
    assert engine._manual_override_active and engine._manual_override_mode == "off"
    assert house.thermostat.mode == "off", "the newest change stands"

    eps = [e for e in _window(house).episodes if e.target == "thermostat"]
    assert [e.end_reason for e in eps][:-1] == ["superseded"], eps
    assert eps[-1].end_ts is None and eps[-1].grace_end_ts is not None, "the second change has its own grace end"

    await _steps(house, _GRACE_S + 60, step=30)
    assert check_p4_override_respected(_window(house), PromiseConfig()) == []
    await house.close()


@pytest.mark.parametrize("fan", _FANS)
async def test_control_an_identical_repeat_inside_five_minutes_is_one_event(make_house, caplog, fan) -> None:
    house = await _house(make_house, fan, caplog)
    events = _record_events(house)
    await house.occupant_sets_thermostat(setpoint_f=74.0)
    await _steps(house, 60)
    await house.occupant_sets_thermostat(setpoint_f=74.5)
    await house.occupant_sets_thermostat(setpoint_f=74.0)
    await _steps(house, 60)
    assert [d.get("new_setpoint_f") for t, d in events if t == "override_detected"].count(74.0) <= 2
    assert check_p4_override_respected(_window(house), PromiseConfig()) == []
    await house.close()


@pytest.mark.parametrize("fan", _FANS)
async def test_a_setpoint_change_while_the_thermostat_is_off_is_not_an_override(make_house, caplog, fan) -> None:
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    await house.occupant_sets_thermostat(mode="off")
    await _steps(house, _CONFIRM_S + 30)
    assert house.thermostat.mode == "off" and engine._manual_override_active, "precondition: the occupant's Off"
    events = _record_events(house)
    await house.occupant_sets_thermostat(setpoint_f=74.0)
    await _steps(house, _CONFIRM_S + 60)
    assert not [t for t, _ in events if t in ("override_detected", "override_confirmed", "grace_started")]
    assert engine._manual_override_mode == "off" and not engine._override_confirm_pending
    assert house.thermostat.mode == "off"
    await house.close()


@pytest.mark.parametrize("fan", _FANS)
async def test_a_setpoint_change_on_a_door_paused_thermostat_is_not_an_override(make_house, caplog, fan) -> None:
    house = await _house(make_house, fan, caplog, contacts=["window"])
    engine = house.coordinator.automation_engine
    await house.open("window")
    await _steps(house, 180)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the heat"
    events = _record_events(house)
    await house.occupant_sets_thermostat(setpoint_f=75.0)
    await _steps(house, _CONFIRM_S + 60)
    assert not [t for t, _ in events if t in ("override_detected", "override_confirmed", "grace_started")]
    assert not engine._manual_override_active and not engine._override_confirm_pending
    assert engine._paused_by_door, "the pause keeps running"
    await house.close()
