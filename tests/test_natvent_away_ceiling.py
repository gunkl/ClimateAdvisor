"""Tests for Issue #231: nat-vent away-mode ceiling exit.

When occupancy is AWAY and nat-vent is active, the engine must exit nat-vent
at the home comfort ceiling (comfort_cool) so the house cannot drift above the
comfort band with no mechanism to stop it.

Three cases:
  1. Indoor == comfort_cool while away → exit fires
  2. Indoor < comfort_cool while away → no exit
  3. Indoor == comfort_cool while home → no exit (ceiling exit is away-only)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_HOME

# ---------------------------------------------------------------------------
# Module-level HA stubs
# ---------------------------------------------------------------------------

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 7, 14, 0, 0)

import custom_components.climate_advisor.automation as _automation_mod  # noqa: E402


def _real_parse_datetime(dt_str: str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


_automation_mod.dt_util.parse_datetime = _real_parse_datetime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(
    comfort_heat: float = 70.0,
    comfort_cool: float = 74.0,
    indoor_f: float = 72.0,
    occupancy: str = OCCUPANCY_AWAY,
) -> AutomationEngine:
    """Create an AutomationEngine stub for away-mode ceiling tests."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = {"current_temperature": indoor_f}

    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": 3.0,
        "notify_service": "notify.notify",
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )

    # Pre-configure nat-vent state
    engine._natural_vent_active = True
    engine._paused_by_door = False
    engine._occupancy_mode = occupancy
    engine._last_outdoor_temp = 65.0  # cooler than indoor — no outdoor-warmth exit

    return engine


def _set_indoor(engine: AutomationEngine, indoor_f: float) -> None:
    engine.hass.states.get.return_value.attributes = {"current_temperature": indoor_f}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNatVentAwayCeilingExit:
    """Issue #231: away-mode ceiling exit fires when indoor >= comfort_cool."""

    def test_natvent_exits_at_comfort_cool_while_away(self):
        """Indoor at comfort_cool (74F) while away → nat_vent_away_ceiling_exit fires."""
        engine = _make_engine(comfort_cool=74.0, indoor_f=74.0, occupancy=OCCUPANCY_AWAY)

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is False, "nat_vent should be deactivated at ceiling"
        ceiling_events = [e for e in events if e[0] == "nat_vent_away_ceiling_exit"]
        assert len(ceiling_events) == 1, f"expected 1 nat_vent_away_ceiling_exit event, got {ceiling_events}"
        payload = ceiling_events[0][1]
        assert payload["indoor"] == 74.0
        assert payload["comfort_cool"] == 74.0

    def test_natvent_does_not_exit_below_ceiling_while_away(self):
        """Indoor below comfort_cool (73F < 74F) while away → nat_vent stays active."""
        engine = _make_engine(comfort_cool=74.0, indoor_f=73.0, occupancy=OCCUPANCY_AWAY)

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True, "nat_vent should remain active below ceiling"
        ceiling_events = [e for e in events if e[0] == "nat_vent_away_ceiling_exit"]
        assert not ceiling_events, "no ceiling exit event should fire below ceiling"

    def test_natvent_ceiling_exit_not_active_when_home(self):
        """Indoor at comfort_cool (74F) while home → ceiling exit does NOT fire (home mode)."""
        engine = _make_engine(comfort_cool=74.0, indoor_f=74.0, occupancy=OCCUPANCY_HOME)

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.check_natural_vent_conditions())

        assert engine._natural_vent_active is True, "nat_vent should remain active — ceiling exit is away-only"
        ceiling_events = [e for e in events if e[0] == "nat_vent_away_ceiling_exit"]
        assert not ceiling_events, "ceiling exit must not fire when occupancy is home"
