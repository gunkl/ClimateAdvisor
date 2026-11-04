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
    """The occupant switches to Cool; the same change then reaches the real engine again a minute later (a cloud echo:
    same source, detected mode, new mode, no setpoint). It is ONE ``override_detected``; a distinct change right after
    (a setpoint) is a second. Through the coordinator a same-mode re-report is not a new override, so the repeat is
    handed to the engine directly, exactly as the coordinator's mode block calls it."""
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    await house.occupant_sets_thermostat(mode="cool")
    await _steps(house, 60)
    detected = [d for t, d in events if t == "override_detected"]
    assert [(d.get("source"), d.get("detected_mode")) for d in detected] == [("normal", "cool")], detected
    engine.handle_manual_override(old_mode="heat", new_mode="cool", classification_mode="heat")
    await _steps(house, 20)
    assert len([t for t, _ in events if t == "override_detected"]) == 1, (
        "an identical repeat inside 5 min is suppressed"
    )
    engine.handle_manual_override(
        source="setpoint", old_mode="cool", new_mode="cool", classification_mode="heat", new_setpoint_f=72.0
    )
    await _steps(house, 20)
    assert len([t for t, _ in events if t == "override_detected"]) == 2, "a distinct change still emits"
    await house.close()


@pytest.mark.parametrize("fan", _FANS)
async def test_a_setpoint_change_while_the_thermostat_is_off_is_not_an_override(make_house, caplog, fan) -> None:
    """Climate Advisor's own Off (no pause, no override: the engine commanded Off, which is what Climate Advisor
    expects the thermostat to be in). The occupant moves the number on the Off thermostat: nothing happens. Before
    #1325 this was a setpoint override (live Off == CA's expected Off), with a confirmation and a grace."""
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    await engine._set_hvac_mode("off", reason="test: Climate Advisor's own Off")
    await _steps(house, 150)  # past the 120 s own-command windows
    assert house.thermostat.mode == "off", "precondition: Climate Advisor's own Off"
    assert not engine._manual_override_active and not engine._override_confirm_pending, "precondition: no override"
    events = _record_events(house)
    caplog.set_level(logging.DEBUG, logger="custom_components.climate_advisor.coordinator")
    await house.occupant_sets_thermostat(setpoint_f=74.0)
    await _steps(house, _CONFIRM_S + 60)
    assert not [t for t, _ in events if t in ("override_detected", "override_confirmed", "grace_started")], events
    assert not engine._manual_override_active and not engine._override_confirm_pending
    assert any("a setpoint change while Off is not an override" in r.getMessage() for r in caplog.records)
    await house.close()


@pytest.mark.parametrize("fan", _FANS)
async def test_a_setpoint_nudge_on_the_occupants_own_off_keeps_their_off(make_house, caplog, fan) -> None:
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


# --- Fan mirror of the #1325 journal rule ------------------------------------------------------------------------

_MILD = {
    **_HOUSE,
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 72.0,
    "outdoor_f": 65.0,
    "high_f": 70.0,
    "low_f": 55.0,
    "initial_hvac_mode": "cool",
}


def _fan_episodes(house):
    return [e for e in _window(house).episodes if e.target == "fan"]


async def test_remote_timer_change_while_running_the_newer_press_is_protected(make_house, caplog) -> None:
    """Occupant (whole-house fan + RF remote): fan on with a 1 h timer, then two minutes later a 4 h timer. The real
    engine emits the new ``fan_manual_override`` BEFORE the supersede cancel of the old press's grace; the newer press
    must stay open with its own grace end (before the fix it was closed ``superseded`` the instant it started, so a CA
    fan-off during the 4 h run went unchecked). whole-house fan only: the thermostat blower has no timer/speed."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="whf_dual",
        ca_config={
            "fan_remote_entity": "event.house_quietcool_remote",
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 30,
        },
        **{**_MILD, "contacts": ["window"]},
    )
    await _steps(house, 360)
    engine = house.coordinator.automation_engine
    await house.open("window")
    await _steps(house, 10)

    def remote(event_type: str) -> None:
        house.hass.states.async_set(
            "event.house_quietcool_remote",
            dt_util.utcnow().isoformat(timespec="milliseconds"),
            {"event_type": event_type, "event_types": ["low", "high", "off", "timer_1h", "timer_4h"]},
        )

    remote("timer_1h")
    await house._drain()
    await house.occupant_presses_fan(True)
    await _steps(house, 120)
    remote("timer_4h")
    await house._drain()
    await _steps(house, 60)
    assert engine._fan_override_active and engine._fan_remote_timer_hours == 4.0, "precondition: the 4 h timer"
    newest = _fan_episodes(house)[-1]
    assert newest.end_ts is None, _fan_episodes(house)
    assert newest.grace_end_ts is not None and (newest.grace_end_ts - newest.start_ts).total_seconds() == 4 * 3600
    assert [e.end_reason for e in _fan_episodes(house)[:-1]] == ["superseded"] * (len(_fan_episodes(house)) - 1)
    assert check_p4_override_respected(_window(house), PromiseConfig()) == []
    await house.close()


@pytest.mark.parametrize("fan", _FANS)
async def test_regression_fan_on_off_on_keeps_the_newer_press_protected(make_house, caplog, fan) -> None:
    """Occupant: fan on, off a minute later, on again a minute after that (wall switch / thermostat blower). The
    newer press is open with its own grace end (traced: already true, pinned so it stays true)."""
    house = await _house(make_house, fan, caplog, **{**_MILD, "contacts": []})

    async def press(on: bool) -> None:
        if fan == "whf":
            await house.occupant_presses_fan(on)
        else:
            await house.occupant_sets_fan_mode("on" if on else "auto")

    await press(True)
    await _steps(house, 60)
    await press(False)
    await _steps(house, 60)
    await press(True)
    await _steps(house, 120)
    eps = _fan_episodes(house)
    assert [(e.kind, e.end_reason) for e in eps][-3:] == [
        ("override", "superseded"),
        ("fan_off", "superseded"),
        ("override", None),
    ], eps
    assert eps[-1].grace_end_ts is not None
    assert check_p4_override_respected(_window(house), PromiseConfig()) == []
    await house.close()
