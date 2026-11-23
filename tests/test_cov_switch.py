"""Coverage of the Automation switch (quality scale Silver, test-coverage).

Occupant view: this switch is the occupant's way to put Climate Advisor in observe-only mode (no thermostat changes,
no notifications). Turning it off must reach the coordinator and show at once; turning it on must resume control.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor import switch as switch_platform


def _switch():
    coord = MagicMock()
    coord.automation_enabled = True
    coord.async_set_automation_enabled = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.title = "Living Room"
    entry.runtime_data = coord
    added: list = []
    asyncio.run(switch_platform.async_setup_entry(MagicMock(), entry, added.extend))
    assert len(added) == 1
    switch = added[0]
    switch.async_write_ha_state = MagicMock()
    return coord, switch


def test_switch_identity():
    _, switch = _switch()
    assert switch._attr_unique_id == "zone1_automation_enabled"
    assert switch._attr_translation_key == "automation_enabled"


def test_state_follows_the_coordinator_flag():
    coord, switch = _switch()
    assert switch.is_on is True
    coord.automation_enabled = False
    assert switch.is_on is False


def test_turn_off_enters_observe_only_and_shows_it():
    coord, switch = _switch()
    asyncio.run(switch.async_turn_off())
    coord.async_set_automation_enabled.assert_awaited_once_with(False)
    switch.async_write_ha_state.assert_called_once()


def test_turn_on_resumes_control_and_shows_it():
    coord, switch = _switch()
    asyncio.run(switch.async_turn_on())
    coord.async_set_automation_enabled.assert_awaited_once_with(True)
    switch.async_write_ha_state.assert_called_once()


def test_always_available_even_after_a_failed_update():
    coord, switch = _switch()
    coord.last_update_success = False
    assert switch.available is True
