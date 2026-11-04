"""Issue #1299 (real HA): the Next Automation card never promises free cooling a dark window cannot start.

Occupant story: on a cool evening the window is open and the AC is paused for it. The window sensor then goes dark
(unavailable, last reading open). The pause is held (rule 3, #1181), but a dark sensor never STARTS a fan (#1181,
#1289). Before the fix the card still said "Natural ventilation at 8:00 PM" and the occupant waited for free cooling
that never came.

Control: the same evening with the window reading open. Free cooling can start, so the card keeps promising it.
"""

from __future__ import annotations

import pytest
from test_quiet_pause_dark_sensor_1298 import _window_opened_in_observe_only

pytestmark = pytest.mark.house


def _card(house) -> tuple[str, str]:
    c = house.coordinator
    return c._compute_next_automation_action(c._current_classification)


async def _paused_for_the_window(make_house):
    """Window open (house at the comfort floor), automation back on: the AC is paused for the window, no fan."""
    house, engine = await _window_opened_in_observe_only(make_house)
    await house.set_automation(True)
    for _ in range(4):
        await house.advance(seconds=30)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the AC is paused for the window"
    assert not engine._natural_vent_active and not house.fan.is_on, "precondition: no fan session yet"
    return house, engine


@pytest.mark.parametrize("dark", [True, False], ids=["dark_window", "live_window_control"])
async def test_card_promises_free_cooling_only_when_a_window_can_start_it(make_house, dark) -> None:
    house, engine = await _paused_for_the_window(make_house)
    if dark:
        house.hass.states.async_set("binary_sensor.house_window", "unavailable")  # last reading open
    await house.advance(seconds=30)

    # The house warms slightly; outdoor stays cooler. Hold it there across a coordinator cycle so the card's
    # predicted-indoor curve is refreshed.
    label = ""
    for _ in range(8):
        house.set_indoor(73.0)
        await house.advance(minutes=5)
        if engine._natural_vent_active:
            break
        label, _when = _card(house)
        if not dark and label == "Natural ventilation":
            break

    if dark:
        assert engine._paused_by_door and not engine._natural_vent_active and not house.fan.is_on, (
            "#1181: the dark window holds the pause and never starts the fan"
        )
        assert label != "Natural ventilation", f"card promises free cooling a dark window can never start: {label!r}"
    else:
        assert engine._natural_vent_active or label == "Natural ventilation", (
            f"control: a live open window must keep the free-cooling promise (card={label!r})"
        )
