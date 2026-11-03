"""Issue #1229 (real HA, two sensors): a window open past its delay is ignored while a second door is within its delay.

Occupant story: window A has been open long past its delay (Climate Advisor paused the HVAC). Someone opens door B
briefly, for less than its delay. Rule 3: while window A is open, heating/cooling stays off unless the occupant's
own manual change is protecting it -- and a brief door open must never change the HVAC (no-thrash rule).

Case 1 (manual grace): the occupant pressed Cool during the pause; their manual grace runs out while door B is
briefly open. The refresh door B causes adopts the override early ("(pre-expiry)") and cancels the grace with no
re-pause, so the AC keeps cooling into window A.

Case 2 (shared guard): ``decide_open_sensor_hold()`` takes a house-wide ``debounce_pending``. While door B is inside
its delay, window A (open past its delay) is hidden, so a comfort-band arm / routine re-check
(``_sync_paused_by_door_with_live_sensors``) arms heat/cool into window A.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

DEB = 300  # door/window delay: door B's brief open stays well inside it
GRACE = 1200  # the occupant's manual grace (20 min)
CONFIRM = 60
_HOT = {
    "contacts": ["window", "door"],
    "profile": "scripted",
    "startup": "settled",
    "start": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 82.0,
    "outdoor_f": 96.0,  # far too hot for nat-vent: an open window means "pause"
    "high_f": 98.0,
    "low_f": 78.0,
    "initial_hvac_mode": "cool",
    "ca_config": {
        "fan_min_runtime_per_hour": 0,
        "sensor_debounce_seconds": DEB,
        "manual_grace_seconds": GRACE,
        "override_confirm_seconds": CONFIRM,
    },
}


async def _window_a_paused(make_house):
    house = await make_house(**_HOT)
    ae = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window that suppresses override detection
    await house.open("window")
    await house.advance(seconds=DEB + 60)
    assert ae._paused_by_door, "precondition: window A (open past its delay) paused the HVAC"
    assert house.thermostat.mode == "off"
    return house, ae


# --------------------------------------------------------------------------------------------- case 1: manual grace


@pytest.mark.parametrize("door_b", [False, True], ids=["window_only", "door_b_brief_open"])
async def test_manual_grace_ending_while_door_b_is_briefly_open_re_pauses_for_window_a(
    make_house, door_b, caplog
) -> None:
    caplog.set_level("INFO", logger="custom_components.climate_advisor")
    house, ae = await _window_a_paused(make_house)

    await house.occupant_sets_thermostat(mode="cool")
    await house.advance(seconds=CONFIRM + 60)
    assert ae._manual_override_active, "precondition: the occupant's Cool press was confirmed as an override"
    assert ae._grace_active, "precondition: the occupant's manual grace is running"
    assert house.thermostat.mode == "cool", "rule 3: the occupant's press holds during their grace"

    # Run up to ~90 s before the grace would end.
    await house.advance(seconds=GRACE - CONFIRM - 60 - 90)
    assert ae._grace_active and house.thermostat.mode == "cool"

    if door_b:
        await house.open("door")  # door B: a brief open, well inside its delay
        await house.advance(seconds=150)  # the grace's own end passes while door B is open (~150 s < DEB)
        await house.close_contact("door")
    else:
        await house.advance(seconds=150)

    await house.advance(minutes=5)  # a couple of 30 s slices plus a 5-minute revisit; well before the 30-min cycle
    adopted = [r.getMessage() for r in caplog.records if "pre-expiry" in r.getMessage()]
    assert house.thermostat.mode == "off", (
        f"rule 3: window A is still open and the occupant's grace is over, yet HVAC is {house.thermostat.mode!r}"
        f" (paused_by_door={ae._paused_by_door}, override={ae._manual_override_active}, adopted={adopted})"
    )
    assert ae._paused_by_door


# --------------------------------------------------------------------------------------------- case 2: shared guard


@pytest.mark.parametrize("door_b", [False, True], ids=["window_only", "door_b_brief_open"])
async def test_cancelled_override_while_door_b_debounces_re_pauses_for_window_a(make_house, door_b, caplog) -> None:
    """Window A paused the HVAC. The occupant turned the heat on (rule 3: their override holds), then presses
    Cancel on the dashboard while door B is briefly open. Automated control resumes ~10 s later through
    ``apply_classification()`` -> ``_sync_paused_by_door_with_live_sensors()`` -> ``_apply_comfort_band()``: both
    read ``decide_open_sensor_hold()``, which must still see window A (open past its own delay).

    A heat press on a hot (cool) day never matches automation's decision, so the case-1 pre-expiry adoption cannot
    fire here: this isolates the shared guard.
    """
    from custom_components.climate_advisor.api import _schedule_reclassify_after_cancel

    caplog.set_level("INFO", logger="custom_components.climate_advisor")
    house, ae = await _window_a_paused(make_house)

    await house.occupant_sets_thermostat(mode="heat")
    await house.advance(seconds=CONFIRM + 60)
    assert ae._manual_override_active, "precondition: the occupant's Heat press was confirmed as an override"
    assert house.thermostat.mode == "heat"

    if door_b:
        await house.open("door")  # door B: a brief open, well inside its delay
        await house.advance(seconds=30)
        assert ae._manual_override_active, "precondition: door B's refresh did not adopt/cancel the override"
    # The dashboard's Cancel override (ClimateAdvisorCancelOverrideView): cancel + re-run the decision in 10 s.
    assert ae.cancel_override(reason="user_cancel_override")
    _schedule_reclassify_after_cancel(house.hass, house.coordinator, ae)
    await house.advance(seconds=30)
    mode_while_b_open = house.thermostat.mode
    if door_b:
        await house.close_contact("door")  # closes before its delay: door B never counted as open
    await house.advance(minutes=5)

    refused = [r.getMessage() for r in caplog.records if "refusing to arm" in r.getMessage()]
    assert mode_while_b_open == "off", (
        f"rule 3: window A is open past its delay, yet automation armed {mode_while_b_open!r} after Cancel"
        f" (paused_by_door={ae._paused_by_door}, guard_refusals={len(refused)})"
    )
    assert house.thermostat.mode == "off", f"HVAC {house.thermostat.mode!r} with window A open"
    assert ae._paused_by_door
