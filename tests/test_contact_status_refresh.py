"""Tests for Issue #489 — Doors/Windows status card refresh symmetry.

`_async_door_window_changed()` in coordinator.py must request a coordinator refresh
on every raw sensor transition (open or closed) so `contact_status` displays live
state promptly, decoupled from the debounce that gates the HVAC pause/resume and
nat-vent decision. It must also request a post-decision refresh after
`handle_all_doors_windows_closed()` when all sensors are confirmed closed, mirroring
the existing post-decision refresh on the open path (after `handle_door_window_open()`).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from datetime import datetime

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 4, 5, 10, 0, 0)


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


_SENSOR_A = "binary_sensor.front_door"
_SENSOR_B = "binary_sensor.back_door"

_PATCH_CALL_LATER = "custom_components.climate_advisor.coordinator.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.coordinator.callback"


def _consume_coroutine(coro):
    coro.close()


def _make_event(entity_id: str, state_value: str) -> MagicMock:
    state = MagicMock()
    state.state = state_value
    event = MagicMock()
    event.data = {"entity_id": entity_id, "new_state": state}
    return event


def _make_coordinator_stub(*, resolved_sensors: list[str], states: dict[str, str]) -> MagicMock:
    """Build a minimal coordinator-like object for testing refresh symmetry.

    `states` maps entity_id -> "on"/"off" and is read live by `_is_sensor_open`,
    same pattern as test_daily_record_accuracy.py's stub.
    """
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {"sensor_debounce_seconds": 0}

    ae = MagicMock()
    ae._is_within_planned_window_period = MagicMock(return_value=False)
    ae.handle_door_window_open = AsyncMock()
    ae.handle_all_doors_windows_closed = AsyncMock()
    ae._temp_command_pending = False
    coord.automation_engine = ae

    coord._current_classification = None
    coord._today_record = None
    coord._resolved_sensors = list(resolved_sensors)
    coord._door_open_timers = {}
    coord._door_open_timer_expiry = {}
    coord._async_save_state = AsyncMock()

    def _is_sensor_open(entity_id: str) -> bool:
        return states.get(entity_id) == "on"

    coord._is_sensor_open = _is_sensor_open

    # Overrides the base DataUpdateCoordinator method for this instance — lets us
    # assert call count directly instead of inspecting closed coroutines. Calling an
    # AsyncMock() records the call immediately, before the returned coroutine is ever
    # awaited/closed, so this is safe to use with hass.async_create_task consuming it.
    coord.async_request_refresh = AsyncMock()

    coord._async_door_window_changed = types.MethodType(ClimateAdvisorCoordinator._async_door_window_changed, coord)

    return coord


class TestImmediateRefreshOnTransition:
    """The top-of-function refresh must fire on every raw transition, open or closed,
    regardless of debounce state or whether other sensors are still open."""

    def test_sensor_opens_triggers_immediate_refresh(self):
        coord = _make_coordinator_stub(resolved_sensors=[_SENSOR_A], states={_SENSOR_A: "on"})
        event = _make_event(_SENSOR_A, "on")

        with patch(_PATCH_CALL_LATER) as mock_call_later, patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            mock_call_later.return_value = MagicMock()
            asyncio.run(coord._async_door_window_changed(event))

        assert coord.async_request_refresh.call_count == 1

    def test_sensor_closes_triggers_immediate_refresh(self):
        coord = _make_coordinator_stub(resolved_sensors=[_SENSOR_A], states={_SENSOR_A: "off"})
        event = _make_event(_SENSOR_A, "off")

        asyncio.run(coord._async_door_window_changed(event))

        # Immediate (top-of-function) + post-decision (all_closed) refresh = 2.
        assert coord.async_request_refresh.call_count == 2

    def test_sensor_closes_while_another_still_open_still_refreshes_display(self):
        """Regression guard for the original bug: a close on one of several monitored
        sensors must still refresh the display even though all_closed is False and no
        automation decision is made."""
        coord = _make_coordinator_stub(
            resolved_sensors=[_SENSOR_A, _SENSOR_B],
            states={_SENSOR_A: "off", _SENSOR_B: "on"},
        )
        event = _make_event(_SENSOR_A, "off")

        asyncio.run(coord._async_door_window_changed(event))

        # Only the immediate top-of-function refresh — no post-decision refresh,
        # since handle_all_doors_windows_closed() is gated behind all_closed.
        assert coord.async_request_refresh.call_count == 1
        coord.automation_engine.handle_all_doors_windows_closed.assert_not_called()


class TestPostDecisionRefreshOnClose:
    """The close branch must request a second refresh after
    handle_all_doors_windows_closed() completes, mirroring the open branch's
    post-handle_door_window_open refresh — so a real pause/resume outcome (HVAC mode,
    grace period) is reflected promptly, not just the raw contact_status."""

    def test_all_sensors_closed_calls_handler_then_refreshes(self):
        coord = _make_coordinator_stub(resolved_sensors=[_SENSOR_A], states={_SENSOR_A: "off"})
        event = _make_event(_SENSOR_A, "off")

        asyncio.run(coord._async_door_window_changed(event))

        coord.automation_engine.handle_all_doors_windows_closed.assert_called_once()
        assert coord.async_request_refresh.call_count == 2
