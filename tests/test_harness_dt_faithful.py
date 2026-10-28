"""Issue #1117: the sim harness's ``dt_util.as_local`` stand-in must behave like HA's.

Real Home Assistant ``dt_util.as_local`` always returns an aware datetime in the
configured zone; a naive input is treated as already being local time. The harness
used to patch it with an identity lambda, so a naive input came back naive and the
regular coordinator refresh crashed (``TypeError: can't compare offset-naive and
offset-aware datetimes``) in the ODE/cosine forecast fallback, before ever reaching
the comfort-minute accumulation, the window-cooling check and the regular-cycle
nat-vent/whole-house-fan re-check. In the home nothing changed (production uses the
real HA function); the risk was that a regression in those regular-cycle fan paths
would pass every test (the #1102 class).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import (
    async_unload_entry_like_ha,
    build_headless_coordinator,
    build_headless_multi_zone,
)
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.fake_scheduler import FakeScheduler

_START = datetime(2024, 1, 15, 16, 0, tzinfo=UTC)
_WEATHER = "weather.test_forecast"


def _daily_forecast(start: datetime) -> list[dict]:
    return [
        {
            "datetime": (start + timedelta(days=i)).replace(hour=8).isoformat(),
            "temperature": 60 + i,
            "templow": 45,
            "condition": "sunny",
        }
        for i in range(5)
    ]


def _seed_weather(fake_hass, entity: str = _WEATHER) -> None:
    fake_hass.states.set(
        entity,
        FakeState(state="sunny", attributes={"temperature": 55, "forecast": _daily_forecast(_START)}),
    )


def _build_live():
    coord, fake_hass, scheduler, _events = build_headless_coordinator(
        start_time=_START,
        skip_startup_coalesce=True,
        climate_attributes={"current_temperature": 70},
    )
    _seed_weather(fake_hass)
    return coord, fake_hass, scheduler


# ---------------------------------------------------------------------------
# (a) the stand-in itself
# ---------------------------------------------------------------------------


def test_as_local_naive_input_returns_aware_local_time():
    """HA treats a naive datetime as local time and always returns an aware one."""
    from custom_components.climate_advisor import automation, coordinator

    scheduler = FakeScheduler(start=_START)
    naive = datetime(2024, 1, 15, 8, 0)
    with scheduler.installed():
        for module in (coordinator, automation):
            result = module.dt_util.as_local(naive)
            assert result.tzinfo is not None, f"{module.__name__}: as_local returned a naive datetime"
            assert result.replace(tzinfo=None) == naive  # same wall-clock, now in the local zone
            assert result.utcoffset() == _START.utcoffset()


def test_as_local_aware_input_converts_to_local_zone():
    """Aware input is converted (not relabelled) into the local zone — same instant."""
    from custom_components.climate_advisor import coordinator

    scheduler = FakeScheduler(start=_START)
    other_zone = datetime(2024, 1, 15, 8, 0, tzinfo=timezone(timedelta(hours=5)))
    with scheduler.installed():
        result = coordinator.dt_util.as_local(other_zone)
    assert result == other_zone  # same instant
    assert result.utcoffset() == _START.utcoffset()  # expressed in the harness's local zone
    assert result.hour == 3


def test_naive_clock_start_is_rejected():
    """A naive virtual clock would make ``now()`` naive while ``as_local`` is aware."""
    with pytest.raises(ValueError, match="aware"):
        FakeScheduler(start=datetime(2024, 1, 15, 8, 0))


# ---------------------------------------------------------------------------
# (b) a normal refresh with a daily-only forecast completes
# ---------------------------------------------------------------------------


def test_headless_refresh_with_daily_forecast_completes():
    coord, fake_hass, scheduler = _build_live()
    with scheduler.installed():
        run_coro(coord._async_update_data())
    assert coord._current_classification is not None


# ---------------------------------------------------------------------------
# (c) the regular cycle reaches the nat-vent/WHF re-check (positive control),
#     and an unloaded old instance's refresh commands nothing (#1102 follow-up)
# ---------------------------------------------------------------------------


def test_regular_cycle_reaches_nat_vent_check_with_sensor_open():
    coord, fake_hass, scheduler = _build_live()
    spy = AsyncMock()
    with (
        scheduler.installed(),
        patch.object(coord, "_any_sensor_open", return_value=True),
        patch.object(coord.automation_engine, "check_natural_vent_conditions", spy),
    ):
        run_coro(coord._async_update_data())
    assert spy.await_count == 1, "regular-cycle check_natural_vent_conditions() was never reached"


def test_unloaded_instance_refresh_commands_nothing():
    """Same conditions as the positive control above, but the old instance was unloaded."""
    zones, fake_hass, scheduler = build_headless_multi_zone(
        configs=[{"climate_entity": "climate.zone_a", "fan_mode": "both", "fan_entity": "fan.whole_house"}],
        start_time=_START,
    )
    zone = zones["zone_0"]
    coord, entry = zone["coordinator"], zone["entry"]
    _seed_weather(fake_hass, coord.config.get("weather_entity", _WEATHER))
    coord._startup_coalesce_active = False

    # Positive control on the live instance first: the refresh does reach the check.
    live_spy = AsyncMock()
    with (
        scheduler.installed(),
        patch.object(coord, "_any_sensor_open", return_value=True),
        patch.object(coord.automation_engine, "check_natural_vent_conditions", live_spy),
    ):
        run_coro(coord._async_update_data())
    assert live_spy.await_count == 1

    dead_spy = AsyncMock()
    with (
        scheduler.installed(),
        patch.object(coord, "_any_sensor_open", return_value=True),
        patch.object(coord.automation_engine, "check_natural_vent_conditions", dead_spy),
    ):
        run_coro(async_unload_entry_like_ha(fake_hass, entry))
        before = len(fake_hass.action_log)
        run_coro(coord._async_update_data())
        scheduler.advance_by(1)
    commands = [a for a in fake_hass.action_log[before:] if a["domain"] in ("fan", "climate", "switch")]
    assert commands == [], f"unloaded instance commanded equipment: {commands}"
    assert dead_spy.await_count == 0
