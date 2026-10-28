"""Issue #1113: coordinator._spawn ties state-save tasks to the config entry.

Occupant view: during a settings save (zone reload) the old instance's state save or
thermal-observation commit must finish before the new instance loads, or a learned
observation can be lost. HA awaits a config entry's tasks on unload; a bare
``hass.async_create_task`` it does not.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402


def _coord(entry):
    coord = types.SimpleNamespace(hass=MagicMock(), config_entry=entry)
    coord._spawn = types.MethodType(ClimateAdvisorCoordinator._spawn, coord)
    return coord


async def _work():
    return None


def test_spawn_uses_entry_task_when_entry_present():
    entry = MagicMock()
    coord = _coord(entry)
    coro = _work()
    try:
        coord._spawn(coro)
        entry.async_create_task.assert_called_once()
        args = entry.async_create_task.call_args.args
        assert args[0] is coord.hass and args[1] is coro
        entry.async_create_background_task.assert_not_called()
        coord.hass.async_create_task.assert_not_called()
    finally:
        coro.close()


def test_spawn_background_uses_entry_background_task():
    entry = MagicMock()
    coord = _coord(entry)
    coro = _work()
    try:
        coord._spawn(coro, background=True)
        entry.async_create_background_task.assert_called_once()
        entry.async_create_task.assert_not_called()
    finally:
        coro.close()


def test_spawn_falls_back_to_hass_task_without_entry():
    coord = _coord(None)
    coro = _work()
    try:
        coord._spawn(coro)
        coord.hass.async_create_task.assert_called_once_with(coro)
    finally:
        coro.close()


def test_set_automation_enabled_save_goes_through_spawn():
    """Group A site: the observe-only toggle's save is entry-tied."""
    coord = types.SimpleNamespace(automation_engine=MagicMock(), _spawn=MagicMock())
    coord._async_save_state = MagicMock(return_value="save-coro")
    ClimateAdvisorCoordinator.set_automation_enabled(coord, False)
    coord._spawn.assert_called_once_with("save-coro")
