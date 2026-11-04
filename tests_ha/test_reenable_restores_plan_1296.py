"""Real-HA tests for Issue #1296: switching automation back on resumes the plan within ~30 s.

Occupant story: a cold day, Climate Advisor heats the house. The occupant opens a window and switches the
whole-house fan on, so Climate Advisor turns the heat off for the fan. Automation is then switched off
(observe-only) and the fan is switched off. Climate Advisor correctly sends no restore (it is observe-only), so the
thermostat stays Off. When automation is switched back on, the heat used to come back only at the next
classification cycle, up to ~30 minutes later, while the house cooled. Now the re-enable re-applies the current plan
at once (after the whole-house-fan safety check and the door/window reconcile), and only where nothing else owns the
thermostat: a fan still running, the occupant's own change and a door/window pause are all left alone.

Both fan shapes: ``whf`` (one entity) and ``whf_dual`` (command entity + power reading).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house


def _cold_house(debounce_s: int = 3600, **extra):
    cfg = {
        "contacts": ["window"],
        "startup": "settled",
        "start": datetime(2026, 1, 15, 15, 0, tzinfo=UTC),
        "initial_indoor_f": 68.0,
        "outdoor_f": 42.0,
        "high_f": 48.0,
        "low_f": 30.0,
        "initial_hvac_mode": "heat",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": debounce_s},
    }
    cfg.update(extra)
    return cfg


def _ca_mode_calls(house, since: int, mode: str | None = None) -> list:
    return [
        c
        for c in house.calls[since:]
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_hvac_mode"
        and (mode is None or c.data.get("hvac_mode") == mode)
    ]


async def _seconds(house, n: int) -> None:
    """1 s slices: the harness otherwise steps 30 s at a time, which would make a "within 30 s" claim untestable."""
    for _ in range(int(n)):
        await house.advance(seconds=1)


async def _fan_session_suppresses_heat(house) -> None:
    """Heat running on the plan; window open; the occupant switches the fan on; Climate Advisor turns the heat off."""
    ae = house.coordinator.automation_engine
    assert house.thermostat.mode == "heat", "precondition: Climate Advisor's heat plan is running"
    await house.open("window")
    await _seconds(house, 5)
    await house.coordinator.async_request_refresh()  # outside the coordinator's refresh cooldown (the #1130 race)
    await house.advance(seconds=3)
    await house.occupant_presses_fan(True)
    await _seconds(house, 20)
    assert house.fan.is_on, "precondition: the occupant's fan runs (window open)"
    assert house.thermostat.mode == "off", "precondition: Climate Advisor turned the heat off for the fan"
    assert ae._pre_fan_hvac_mode == "heat" and ae._whf_owns_hvac(), "precondition: the fan session owns the HVAC"


async def _observe_only_fan_stop(house) -> None:
    """Automation off, then the fan is switched off: no restore is sent (observe-only), the thermostat stays Off."""
    ae = house.coordinator.automation_engine
    await house.set_automation(False)
    await house.advance(minutes=1)
    calls_before = len(house.calls)
    await house.occupant_presses_fan(False)
    await _seconds(house, 30)
    assert not house.fan.is_on
    assert house.thermostat.mode == "off", "observe-only must not restore the heat"
    assert not _ca_mode_calls(house, calls_before), "observe-only sent a thermostat command"
    assert not ae._whf_owns_hvac(), "precondition: the stopped fan released its ownership"


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_reenable_after_an_observe_only_fan_stop_restores_the_heat_within_30s(make_house, fan) -> None:
    house = await make_house(fan=fan, **_cold_house())
    await _fan_session_suppresses_heat(house)
    await _observe_only_fan_stop(house)
    await house.close_contact("window")
    await house.advance(minutes=1)
    assert house.thermostat.mode == "off"

    calls_before = len(house.calls)
    await house.set_automation(True)
    await _seconds(house, 30)

    assert house.thermostat.mode == "heat", "re-enabling automation left the thermostat Off (the plan is heat)"
    assert len(_ca_mode_calls(house, calls_before, "heat")) == 1, house.calls[calls_before:]

    # The 30-minute cycle that follows sends nothing more: the plan is already in place.
    calls_before = len(house.calls)
    await house.advance(minutes=35)
    assert house.thermostat.mode == "heat"
    assert not _ca_mode_calls(house, calls_before), house.calls[calls_before:]


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_control_fan_still_running_at_reenable_keeps_the_heat_off(make_house, fan) -> None:
    """The whole-house fan session still owns the thermostat: the re-enable must not turn the heat on beside it."""
    house = await make_house(fan=fan, **_cold_house())
    ae = house.coordinator.automation_engine
    await _fan_session_suppresses_heat(house)
    await house.set_automation(False)
    await house.advance(minutes=2)

    calls_before = len(house.calls)
    await house.set_automation(True)
    await _seconds(house, 60)

    assert house.fan.is_on, "the occupant's fan must keep running"
    assert house.thermostat.mode == "off", "the heat was turned on beside the running whole-house fan"
    assert not _ca_mode_calls(house, calls_before, "heat"), house.calls[calls_before:]
    assert ae._whf_owns_hvac()


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_control_occupant_change_made_in_observe_only_is_left_alone(make_house, fan) -> None:
    """The occupant set the thermostat while automation was off: the re-enable must not overwrite that change."""
    house = await make_house(fan=fan, **_cold_house())
    ae = house.coordinator.automation_engine
    await _fan_session_suppresses_heat(house)
    await _observe_only_fan_stop(house)
    await house.close_contact("window")
    await house.advance(minutes=1)
    await house.occupant_sets_thermostat(mode="cool", setpoint_f=80.0)
    await house.advance(minutes=1)
    assert ae._manual_override_active or ae._override_confirm_pending, "precondition: the occupant's change is seen"

    calls_before = len(house.calls)
    await house.set_automation(True)
    await _seconds(house, 60)

    assert house.thermostat.mode == "cool", "the re-enable overwrote the occupant's own change"
    assert not _ca_mode_calls(house, calls_before), house.calls[calls_before:]


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_control_window_open_at_reenable_stays_paused(make_house, fan) -> None:
    """A window open past the debounce at the re-enable: the heat stays off (door/window pause), not restored."""
    house = await make_house(fan=fan, **_cold_house())
    ae = house.coordinator.automation_engine
    await _fan_session_suppresses_heat(house)
    await _observe_only_fan_stop(house)
    await house.advance(minutes=61)  # the window has now been open past the 3600 s debounce
    assert house.thermostat.mode == "off"

    calls_before = len(house.calls)
    await house.set_automation(True)
    await _seconds(house, 60)

    assert house.thermostat.mode == "off", "the heat was restored into the open window"
    assert not _ca_mode_calls(house, calls_before, "heat"), house.calls[calls_before:]
    assert ae._paused_by_door, "the re-enable must pause for the window open past its debounce"


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_thrash_off_on_off_on_settles_on_the_plan_with_one_command(make_house, fan) -> None:
    """Automation toggled off/on twice in quick succession: one heat command, no stuck state."""
    house = await make_house(fan=fan, **_cold_house())
    ae = house.coordinator.automation_engine
    await _fan_session_suppresses_heat(house)
    await _observe_only_fan_stop(house)
    await house.close_contact("window")
    await house.advance(minutes=1)

    calls_before = len(house.calls)
    await house.set_automation(True)
    await house.set_automation(False)
    await house.set_automation(True)
    await _seconds(house, 30)

    assert house.thermostat.mode == "heat"
    assert len(_ca_mode_calls(house, calls_before, "heat")) == 1, house.calls[calls_before:]
    assert not ae._whf_owns_hvac() and not ae._paused_by_door and not ae._manual_override_active
