"""Real-HA tests for Issue #1211 (A3): a window opened inside the planned window period.

Occupant story: a warm morning, windows are recommended (06:00-10:00), the house is warmer than outdoors and
the whole-house fan is configured. The person opens a window. Before the fix the door/window handler returned
silently ("windows are planned, do nothing") and the fan only started because the coordinator's NEXT refresh
re-ran the comfort band, which paused the HVAC and then re-activated nat-vent (observed 14.5 minutes late with
the refresh suppressed, with the AC cooling into the open window). The handler must run the nat-vent entry gate
itself, start nat-vent when it passes, and pause right there when it refuses (owner decision O8b).

Every test suppresses the coordinator's post-handler refresh (``async_request_refresh``) so nothing but the
handler can act within the debounce.

Prove the tests are not vacuous with ``scripts/revert_proof.py <pre-fix commit> <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.house

_DEBOUNCE_S = 30
_HOUSE = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 7, 0, tzinfo=UTC),
    "initial_indoor_f": 79.0,
    "outdoor_f": 62.0,
    "high_f": 82.0,
    "low_f": 55.0,
    "initial_hvac_mode": "off",
    "ca_config": {"sensor_debounce_seconds": _DEBOUNCE_S, "fan_min_runtime_per_hour": 0},
}


async def _house_in_period(make_house, *, suppress_refresh: bool = True, **overrides):
    house = await make_house(**{**_HOUSE, **overrides})
    await house.advance(minutes=6)  # past the start-up window that suppresses override detection
    engine = house.coordinator.automation_engine
    c = engine._current_classification
    assert c is not None and c.windows_recommended and c.hvac_mode == "off", "precondition: a windows day"
    assert engine._is_within_planned_window_period(), "precondition: inside the planned window period"
    if suppress_refresh:
        house.coordinator.async_request_refresh = AsyncMock()
    return house, engine


async def test_window_in_period_starts_natvent_from_the_handler(make_house) -> None:
    house, engine = await _house_in_period(make_house)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)

    assert engine._natural_vent_active, "the handler itself must start nat-vent (no refresh ran)"
    assert house.fan.is_on, "the whole-house fan must be running"
    assert engine._paused_by_door is False
    assert house.thermostat.action not in ("cooling", "heating"), "rule 2: fan and HVAC never together"


async def test_positive_control_with_refresh_allowed_natvent_also_starts(make_house) -> None:
    """Control: the refresh path (the old route) still ends with the fan running, so the suppression above is
    what exposes the handler's behaviour, not a broken fixture."""
    house, engine = await _house_in_period(make_house, suppress_refresh=False)
    await house.open("window")
    await house.advance(minutes=3)
    assert engine._natural_vent_active
    assert house.fan.is_on


async def test_window_in_period_with_gate_refusing_pauses_in_the_handler(make_house) -> None:
    """Outdoor 71 is warmer than indoor 70 (both inside the 68-74 band, so the windows day stands), so nat-vent is
    refused: the handler pauses right there."""
    house, engine = await _house_in_period(make_house, initial_indoor_f=70.0, outdoor_f=71.0)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)

    assert engine._natural_vent_active is False
    assert house.fan.is_on is False
    assert engine._paused_by_door, "the handler must record the pause itself (O8b)"
    assert house.thermostat.mode == "off"


async def test_manual_override_still_prevents_the_start(make_house) -> None:
    """The occupant's own cooling wins over the open window: no nat-vent, no pause, the cooling survives."""
    house, engine = await _house_in_period(
        make_house, ca_config={**_HOUSE["ca_config"], "manual_grace_seconds": 0, "override_confirm_seconds": 60}
    )
    await house.occupant_sets_thermostat(mode="cool", setpoint_f=72.0)
    await house.advance(minutes=3)
    assert engine._manual_override_active, "precondition: the change was confirmed as an override"
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)

    assert engine._natural_vent_active is False
    assert engine._paused_by_door is False
    assert house.thermostat.mode == "cool"


async def test_cool_morning_in_a_cold_house_clears_the_windows_recommendation(make_house) -> None:
    """Issue #1211 (O3): indoor 66 is below the 68 floor and outdoor 58 is colder, so opening windows would chill
    the house further. Outdoor 58 is NOT under the old 'floor minus 15' cutoff (53), so only the indoor-aware gate
    clears the recommendation."""
    house = await make_house(**{**_HOUSE, "initial_indoor_f": 66.0, "outdoor_f": 58.0})
    await house.advance(minutes=6)  # includes a 5-minute outdoor tick, which runs the live gate
    engine = house.coordinator.automation_engine
    assert engine._current_classification.windows_recommended is False
    assert engine._current_classification.forecast_windows_recommended is True, "the forecast still says windows"


async def test_positive_control_cold_house_with_warmer_outdoor_keeps_the_recommendation(make_house) -> None:
    house = await make_house(**{**_HOUSE, "initial_indoor_f": 60.0, "outdoor_f": 62.0})
    await house.advance(minutes=6)
    assert house.coordinator.automation_engine._current_classification.windows_recommended is True
