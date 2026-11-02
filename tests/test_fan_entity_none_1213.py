"""Issue #1213 (optional hardening, pinned): a stored ``fan_entity: None`` never reaches ``hass.states.get``.

Nothing changes for anyone at home: the real config flow never stores the key as ``None``. The 1ea2c19 hardening
(``self.config.get(CONF_FAN_ENTITY) or ""``) keeps the 5-minute fan-drift tick from raising AttributeError in real
Home Assistant (``hass.states.get(None)``) if such a config ever appears. This pins it.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE, FAN_MODE_HVAC  # noqa: E402


def test_drift_tick_with_a_none_fan_entity_never_asks_hass_for_entity_none():
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda c: c.close())
    seen: list = []

    def _get(entity_id):
        seen.append(entity_id)
        state = MagicMock()
        state.state = "cool"
        state.attributes = {"fan_mode": "auto", "hvac_action": "idle", "fan_modes": ["auto", "on"]}
        return state

    hass.states.get = MagicMock(side_effect=_get)
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config={"notify_service": "notify.notify", CONF_FAN_MODE: FAN_MODE_HVAC, CONF_FAN_ENTITY: None},
    )
    engine._fan_active = True  # a stranded blower session
    engine._reconcile_fan_physical_drift()
    assert seen, "the drift tick read state"
    assert None not in seen
