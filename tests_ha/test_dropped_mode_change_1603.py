"""Issue #1603 (real HA): the occupant's mode change that lands while a Climate Advisor command is in flight.

Occupant story: on a hot (or warm) day the person turns the thermostat Off and Climate Advisor confirms it as a manual
override. A while later they switch to Cool (or Heat). If that change lands while one of Climate Advisor's own
commands is still in flight, the coordinator's override detection used to drop it in every branch: the dashboard kept
saying "manual override: off", with its grace running, over a house that was cooling or heating, and at grace expiry
Climate Advisor acted on the abandoned Off (a Heat choice was quietly reverted).

Two REAL commands run during an Off override grace and open the guard window (Phase 1 evidence, #1603):

* ``hvac_fan``: the fan-minimum-runtime blower cycle sends ``climate.set_fan_mode`` while the override runs;
  ``_fan_command_pending`` is True for the whole call. A cloud thermostat takes seconds to answer, so the person's
  app change can be reported while CA's call is still awaited. Modelled here by letting the occupant's real
  ``set_hvac_mode`` service call land inside the thermostat's handling of CA's ``set_fan_mode``.
* ``whole_house_fan``: a window opened during the grace starts the whole-house fan, whose start sends
  ``climate.set_hvac_mode(off)`` (WHF/AC exclusion), stamping ``_hvac_command_time``: a mode change in the next 3 s
  (``_is_recent_hvac_command``) was dropped.

Now a change dropped only by those guards is re-checked once the guard clears and, when the thermostat still shows it
and it is not Climate Advisor's own mode write, routed through the same Fix-D supersede (``OVERRIDE_SUPERSEDED``) and
a fresh confirmation. CA's own mode echo inside the window is still not the occupant.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_CONFIRM_S = 300
_CLIMATE = "climate.house_thermostat"
_DAYS = {
    "hot": {
        "start": datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
        "initial_indoor_f": 80.0,
        "outdoor_f": 95.0,
        "high_f": 98.0,
        "low_f": 75.0,
    },
    "warm": {
        "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        "initial_indoor_f": 75.0,  # above comfort_cool (74): CA cools although the day label is "off"
        "outdoor_f": 68.0,
        "high_f": 80.0,
        "low_f": 62.0,
    },
}


def _sim(house):
    return house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]


async def _confirmed_off(make_house, fan: str, day: str):
    """CA cooling the day, then the occupant's Off confirmed as a manual override (1 h grace)."""
    house = await make_house(
        fan=fan,
        contacts=["window"],
        startup="settled",
        profile="scripted",
        initial_hvac_mode="cool",
        ca_config={
            "fan_min_runtime_per_hour": 10,  # the blower / fan minimum-runtime cycle runs during the grace
            "manual_grace_seconds": 3600,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **_DAYS[day],
    )
    await house.advance(minutes=8)  # past the start-up window and >120 s after CA's own commands
    engine = house.coordinator.automation_engine
    assert house.thermostat.mode == "cool", f"precondition: CA is cooling the {day} day"
    await house.occupant_sets_thermostat(mode="off")
    await house.advance(seconds=_CONFIRM_S + 60)
    assert engine._manual_override_active and engine._manual_override_mode == "off", "precondition: Off confirmed"
    assert engine._grace_active
    return house, engine


def _state(house, engine) -> str:
    return (
        f"thermostat={house.thermostat.mode} override_active={engine._manual_override_active} "
        f"override_mode={engine._manual_override_mode} confirm_pending={engine._override_confirm_pending} "
        f"confirm_mode={engine._override_confirm_mode} grace={engine._grace_active}"
    )


async def _change_during_blower_command(house, engine, target: str) -> None:
    """The occupant's app change is reported while CA's real blower command (``set_fan_mode``) is in flight."""
    sim = _sim(house)
    real_set_fan_mode = sim.async_set_fan_mode
    seen: dict[str, bool] = {}

    async def _cloud_round_trip(fan_mode):
        if not seen:
            seen["ca_command_pending"] = engine._fan_command_pending
            await house.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": _CLIMATE, "hvac_mode": target},
                blocking=True,
                context=house._occupant_context(),
            )
        await real_set_fan_mode(fan_mode)

    sim.async_set_fan_mode = _cloud_round_trip
    try:
        for _ in range(3600):  # the minimum-runtime cycle starts the blower within the hour
            await house.advance(seconds=1)
            if seen:
                break
    finally:
        sim.async_set_fan_mode = real_set_fan_mode
    assert seen.get("ca_command_pending") is True, "precondition: CA's real blower command was in flight"
    assert house.thermostat.mode == target


async def _change_after_whf_start(house, engine, target: str) -> None:
    """A window opens, the whole-house fan starts, its HVAC-off write opens the 3 s window; the change lands in it."""
    stamp = engine._hvac_command_time
    await house.open()
    for _ in range(900):
        await house.advance(seconds=1)
        if engine._hvac_command_time != stamp:
            break
    assert engine._hvac_command_time != stamp, "precondition: the fan start sent a real HVAC command"
    assert house.coordinator._is_recent_hvac_command(), "precondition: the occupant's change lands inside the window"
    await house.occupant_sets_thermostat(mode=target)
    assert house.thermostat.mode == target


_CASES = [("hvac_fan", _change_during_blower_command), ("whf", _change_after_whf_start)]


@pytest.mark.parametrize("target", ["cool", "heat"])
@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize(("fan", "change"), _CASES, ids=[c[0] for c in _CASES])
async def test_change_inside_ca_command_window_supersedes_the_off_override(make_house, fan, change, day, target):
    house, engine = await _confirmed_off(make_house, fan, day)
    await change(house, engine, target)

    await house.advance(seconds=10)  # the guard has cleared
    assert not (engine._manual_override_active and engine._manual_override_mode == "off"), (
        f"stale 'manual override: off' left over the occupant's {target}: {_state(house, engine)}"
    )
    assert engine._override_confirm_pending and engine._override_confirm_mode == target, (
        f"the occupant's {target} must start its own confirmation: {_state(house, engine)}"
    )

    await house.advance(seconds=_CONFIRM_S + 60)
    assert not (engine._manual_override_active and engine._manual_override_mode == "off"), _state(house, engine)
    if target == "heat":  # not what CA runs: the occupant's Heat is confirmed and protected
        assert engine._manual_override_active and engine._manual_override_mode == "heat", _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
async def test_heat_is_not_reverted_when_the_abandoned_off_grace_would_have_ended(make_house, fan, day) -> None:
    """Revert-proof of the occupant outcome: an hour later the house is still on the Heat the person chose."""
    house, engine = await _confirmed_off(make_house, fan, day)
    change = _change_during_blower_command if fan == "hvac_fan" else _change_after_whf_start
    await change(house, engine, "heat")
    if fan == "whf":
        await house.close_contact()
    await house.advance(minutes=58)  # past the end of the abandoned Off override's grace
    assert engine._manual_override_mode != "off", _state(house, engine)
    assert engine._manual_override_active and engine._manual_override_mode == "heat", _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
async def test_cas_own_mode_echo_inside_the_window_is_not_the_occupant(make_house, fan, day) -> None:
    """CA's own mode write echoing back (2 s cloud delay) inside the guard window must not supersede the override."""
    house, engine = await _confirmed_off(make_house, fan, day)
    _sim(house).set_echo_delay(2)
    await engine._set_hvac_mode("cool", reason="#1603 test: CA's own write")
    assert house.thermostat.mode == "off", "precondition: the echo has not arrived yet"
    await house.advance(seconds=2)
    assert house.thermostat.mode == "cool", "precondition: CA's echo arrived"
    assert house.coordinator._is_recent_hvac_command(), "precondition: the echo landed inside the guard window"
    _sim(house).set_echo_delay(0)
    await house.advance(seconds=20)
    assert engine._manual_override_active and engine._manual_override_mode == "off", (
        f"CA's own echo was read as the occupant: {_state(house, engine)}"
    )
    assert not engine._override_confirm_pending, _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
async def test_control_change_outside_the_window_is_superseded_at_once(make_house, fan, day) -> None:
    house, engine = await _confirmed_off(make_house, fan, day)
    assert not house.coordinator._is_recent_hvac_command()
    await house.occupant_sets_thermostat(mode="heat")
    assert not engine._manual_override_active, _state(house, engine)
    assert engine._override_confirm_pending and engine._override_confirm_mode == "heat", _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
async def test_recheck_waits_while_ca_commands_keep_the_guard_set(make_house, day) -> None:
    """The re-check does not act inside a later guard window; it supersedes once that window has cleared too."""
    from homeassistant.util import dt as dt_util

    house, engine = await _confirmed_off(make_house, "whf", day)
    await _change_after_whf_start(house, engine, "heat")
    await house.advance(seconds=3)
    engine._hvac_command_time = dt_util.now()  # another CA command just went out (mechanism check only)
    await house.advance(seconds=2)  # the first re-check (4 s) falls inside the new window
    assert engine._manual_override_active and engine._manual_override_mode == "off", _state(house, engine)
    await house.advance(seconds=10)
    assert not engine._manual_override_active, _state(house, engine)
    assert engine._override_confirm_pending and engine._override_confirm_mode == "heat", _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
async def test_a_later_ca_mode_echo_before_the_recheck_is_not_the_occupant(make_house, day) -> None:
    """The dropped Heat is replaced on the thermostat by CA's own Cool write (its echo consumed by the listener)
    before the re-check: the re-check must not take the live Cool for the occupant's choice."""
    house, engine = await _confirmed_off(make_house, "hvac_fan", day)
    await _change_during_blower_command(house, engine, "heat")
    await engine._set_hvac_mode("cool", reason="#1603 test: CA's own later write")
    assert house.thermostat.mode == "cool"
    await house.advance(seconds=10)
    assert engine._override_confirm_mode != "cool" or not engine._override_confirm_pending, _state(house, engine)
