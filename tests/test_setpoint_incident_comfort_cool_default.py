"""Regression test: the setpoint-mode-inconsistency incident detector in
_set_temperature() must use the SAME comfort_cool default (DEFAULT_COMFORT_COOL)
as every other site in the codebase, not a stray literal (architecture-reset
latent-bug fix).

Before the fix, `mode == "heat" and temperature > (config.get("comfort_cool", 76) + 1.0)`
used a fallback of 76 while every other comfort_cool default read in
automation.py/coordinator.py used 75 — a 1F band difference that only mattered
for installs relying on the default (comfort_cool not explicitly configured).
The shared default was later reformatted to match a real tuned installation
(DEFAULT_COMFORT_COOL=74); this test asserts against that named constant
rather than a hardcoded literal so it can't drift again.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import DEFAULT_COMFORT_COOL


def _make_engine_no_comfort_cool_configured():
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    hass.states = MagicMock()
    climate_state = MagicMock()
    climate_state.state = "heat"
    climate_state.attributes = {}
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": 70,
        # comfort_cool intentionally NOT configured -- exercises the default fallback.
        "notify_service": "notify.notify",
    }
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )


def test_heat_mode_incident_threshold_uses_shared_default_not_a_stray_literal():
    """temperature=76.5 in heat mode: with DEFAULT_COMFORT_COOL(74), threshold is
    75 (74+1), so 76.5 > 75 SHOULD trigger the incident -- proving the incident
    detector reads the same shared default every other comfort_cool site uses."""
    engine = _make_engine_no_comfort_cool_configured()
    events: list[tuple] = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))

    asyncio.run(engine._set_temperature(76.5, reason="test", mode="heat"))

    incident_events = [e for e in events if e[0] == "incident_detected"]
    assert len(incident_events) == 1, (
        f"expected the setpoint-mode-inconsistency incident to fire at 76.5F in heat mode "
        f"with the shared comfort_cool default (threshold={DEFAULT_COMFORT_COOL + 1}); got events: {events}"
    )
    assert incident_events[0][1]["comfort_cool"] == DEFAULT_COMFORT_COOL, (
        f"incident payload must report the same comfort_cool default ({DEFAULT_COMFORT_COOL}) other code paths use; "
        f"got {incident_events[0][1]['comfort_cool']}"
    )
