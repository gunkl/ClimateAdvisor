"""Issue #1107 (option B): the Comfort Score measures against the target range in effect.

Occupant experience being guarded: the person sets a cooler sleep (or away) temperature
on purpose, Climate Advisor lets the house drift down to it overnight exactly as asked,
and the next morning the Comfort Score says the house was "uncomfortable" for those
hours. On the owner's install that planned setback time was ~40 of the ~46 lost points,
and the same minutes can trigger the ``comfort_violations`` suggestion, which nudges the
person toward weaker setbacks. After the fix a minute only counts when indoor is outside
the band Climate Advisor is actually holding at that moment (the same band the chart
draws), falling back to the static comfort range until a band has been resolved.

These run the real regular refresh end to end (reachable headless since #1117).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState

_NIGHT = datetime(2024, 1, 15, 3, 0, tzinfo=UTC)  # harness local zone is UTC → inside the sleep window
_DAY = datetime(2024, 1, 15, 14, 0, tzinfo=UTC)  # awake band
_BASE = {
    "comfort_heat": 68.0,
    "comfort_cool": 74.0,
    "sleep_heat": 64.0,
    "sleep_cool": 74.0,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}


def _refresh_minutes(start: datetime, indoor: float, *, hourly: bool = True, **config) -> tuple[float, tuple]:
    """Run one regular refresh at *start* with *indoor*; return (minutes added, band in effect)."""
    coord, fake_hass, scheduler, _events = build_headless_coordinator(
        config={**_BASE, **config},
        start_time=start,
        skip_startup_coalesce=True,
        climate_attributes={"current_temperature": indoor},
    )
    daily = [
        {"datetime": (start + timedelta(days=i)).isoformat(), "temperature": 60 + i, "templow": 45} for i in range(5)
    ]
    fake_hass.states.set(
        "weather.test_forecast", FakeState(state="sunny", attributes={"temperature": 55, "forecast": daily})
    )
    hourly_rows = (
        [{"datetime": (start + timedelta(hours=k)).isoformat(), "temperature": 50.0} for k in range(-2, 30)]
        if hourly
        else []
    )
    coord._get_hourly_forecast_data = AsyncMock(return_value=hourly_rows)
    coord._last_violation_check = None  # first check of the day counts a full 30 min
    before = coord._today_record.comfort_violations_minutes if coord._today_record else 0.0
    with scheduler.installed():
        run_coro(coord._async_update_data())
        band = coord._target_band_lower_upper_now()
    assert coord._today_record is not None
    return coord._today_record.comfort_violations_minutes - before, band


def test_planned_sleep_setback_is_not_a_violation():
    """66 °F at 03:00 inside the 64-74 sleep band: CA is doing what was asked → 0 minutes."""
    added, band = _refresh_minutes(_NIGHT, 66.0)
    assert band == (64.0, 74.0)  # precondition: sleep band in effect
    assert added == 0.0


def test_below_the_sleep_floor_still_counts():
    """63 °F is colder than even the planned sleep floor → a real violation."""
    added, _ = _refresh_minutes(_NIGHT, 63.0)
    assert added == 30.0


def test_awake_band_behaviour_unchanged():
    """During the awake band the result matches the static comfort range, as before."""
    below, band = _refresh_minutes(_DAY, 66.0)
    assert band == (68.0, 74.0)
    assert below == 30.0
    inside, _ = _refresh_minutes(_DAY, 70.0)
    assert inside == 0.0
    above, _ = _refresh_minutes(_DAY, 75.0)
    assert above == 30.0


def test_falls_back_to_static_comfort_range_without_a_resolved_band():
    """J4: no hourly forecast → no target band yet ((None, None)) → static comfort range."""
    added, band = _refresh_minutes(_NIGHT, 66.0, hourly=False)
    assert band == (None, None)  # precondition
    assert added == 30.0  # 66 < comfort_heat 68
    inside, _ = _refresh_minutes(_NIGHT, 70.0, hourly=False)
    assert inside == 0.0


def test_planned_cool_side_setback_is_not_a_violation():
    """Mirror: a sleep cooling setback above the day ceiling (77 °F in a 64-79 band) adds 0."""
    added, band = _refresh_minutes(_NIGHT, 77.0, sleep_cool=79.0)
    assert band == (64.0, 79.0)
    assert added == 0.0
    above, _ = _refresh_minutes(_NIGHT, 80.0, sleep_cool=79.0)
    assert above == 30.0


def test_nat_vent_tolerance_uses_the_same_band():
    """J4: the nat-vent tolerance gate is judged against the same resolved band."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    real = ClimateAdvisorCoordinator._is_nat_vent_tolerated_deviation
    with patch.object(
        ClimateAdvisorCoordinator, "_is_nat_vent_tolerated_deviation", autospec=True, side_effect=real
    ) as spy:
        _refresh_minutes(_NIGHT, 63.0)
    # Both the score accumulation and the incident scan (which already uses the active
    # band, #481) consult the gate this cycle; neither may judge 63 °F against the static
    # 68-74 day range while the 64-74 sleep band is in effect.
    calls = [tuple(c.args[1:]) for c in spy.call_args_list]
    assert calls, "tolerance gate never consulted"
    assert all(c[1:] == (64.0, 74.0) for c in calls), calls
