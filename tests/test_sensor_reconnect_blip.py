"""Tests for Issue #645 — a sensor reconnecting from unavailable/unknown while already
open must not be treated as a fresh debounce-pending open.

Root cause: a group/helper contact-sensor entity blips `unavailable -> on` during an HA
restart/integration reload (confirmed via live REST state history), stamping a fresh
`last_changed` on a window that has physically been open for hours. `_sensor_debounce_pending()`
previously read that fresh timestamp the same as a genuine open, letting a downstream guard in
`_apply_comfort_band()` (Issue #629) skip pausing and arm an active HVAC mode through the open
window. Fixed by recording reconnect-blip timestamps in `_async_door_window_changed()` and
excluding them in `_sensor_debounce_pending()` — while a GENUINE off->on transition (Issue #623's
own scenario: a user briefly opening a door) must still start debounce normally.

Pattern: coordinator direct instantiation via object.__new__, mirroring
tests/test_contact_status_refresh.py.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 8, 15, 8, 0, 0)

_SENSOR = "binary_sensor.group_contact_sensors_climate_advisor"
_PATCH_CALL_LATER = "custom_components.climate_advisor.coordinator.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.coordinator.callback"


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    coro.close()


def _make_state(state_value: str, last_changed: datetime) -> MagicMock:
    state = MagicMock()
    state.state = state_value
    state.last_changed = last_changed
    return state


def _make_event(entity_id: str, old_state: MagicMock | None, new_state: MagicMock) -> MagicMock:
    event = MagicMock()
    event.data = {"entity_id": entity_id, "old_state": old_state, "new_state": new_state}
    return event


def _make_coordinator_stub(*, sensor_debounce_seconds: int = 600) -> MagicMock:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {"sensor_debounce_seconds": sensor_debounce_seconds}

    ae = MagicMock()
    ae._is_within_planned_window_period = MagicMock(return_value=False)
    ae.handle_door_window_open = AsyncMock()
    ae.handle_all_doors_windows_closed = AsyncMock()
    ae._temp_command_pending = False
    coord.automation_engine = ae

    coord._current_classification = None
    coord._today_record = None
    coord._resolved_sensors = [_SENSOR]
    coord._door_open_timers = {}
    coord._door_open_timer_expiry = {}
    coord._sensor_reconnect_blip_last_changed = {}
    coord._async_save_state = AsyncMock()
    coord.async_request_refresh = AsyncMock()

    import types

    coord._async_door_window_changed = types.MethodType(ClimateAdvisorCoordinator._async_door_window_changed, coord)
    coord._sensor_debounce_pending = types.MethodType(ClimateAdvisorCoordinator._sensor_debounce_pending, coord)
    coord._is_sensor_open = types.MethodType(ClimateAdvisorCoordinator._is_sensor_open, coord)
    coord.sensor_polarity_inverted = False

    return coord


class TestReconnectBlipExcludedFromDebouncePending:
    """Issue #645: an unavailable/unknown -> on transition on an already-open sensor must
    not make _sensor_debounce_pending() read True."""

    def test_reconnect_from_unavailable_does_not_start_debounce(self):
        now = datetime(2026, 8, 15, 8, 0, 0)
        fresh_last_changed = now - timedelta(seconds=10)  # "just" stamped by HA
        old_state = _make_state("unavailable", now - timedelta(hours=2))
        new_state = _make_state("on", fresh_last_changed)

        coord = _make_coordinator_stub()
        coord.hass.states.get = MagicMock(return_value=new_state)

        event = _make_event(_SENSOR, old_state, new_state)
        asyncio.run(coord._async_door_window_changed(event))

        # No debounce timer registered for a reconnect blip.
        assert coord._door_open_timers == {}
        # And _sensor_debounce_pending() must not read this fresh last_changed as pending.
        assert coord._sensor_debounce_pending() is False, (
            "a sensor reconnecting from unavailable/unknown while already open must not read "
            "as debounce-pending (Issue #645) — the window has been open for hours, only HA's "
            "own state machine stamped a fresh timestamp on the availability blip"
        )

    def test_reconnect_from_unknown_does_not_start_debounce(self):
        now = datetime(2026, 8, 15, 8, 0, 0)
        fresh_last_changed = now - timedelta(seconds=5)
        old_state = _make_state("unknown", now - timedelta(hours=1))
        new_state = _make_state("on", fresh_last_changed)

        coord = _make_coordinator_stub()
        coord.hass.states.get = MagicMock(return_value=new_state)

        event = _make_event(_SENSOR, old_state, new_state)
        asyncio.run(coord._async_door_window_changed(event))

        assert coord._sensor_debounce_pending() is False

    def test_genuine_off_to_on_transition_still_starts_debounce(self):
        """Control (Issue #623 preserved): a REAL off->on transition must still be treated
        as debounce-pending — the whole point of the mechanism is to ignore a brief,
        transient open, and that protection must not be collateral damage of this fix."""
        now = datetime(2026, 8, 15, 8, 0, 0)
        fresh_last_changed = now
        old_state = _make_state("off", now - timedelta(minutes=30))
        new_state = _make_state("on", fresh_last_changed)

        coord = _make_coordinator_stub()
        coord.hass.states.get = MagicMock(return_value=new_state)

        event = _make_event(_SENSOR, old_state, new_state)
        with patch(_PATCH_CALL_LATER) as mock_call_later, patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            mock_call_later.return_value = MagicMock()
            asyncio.run(coord._async_door_window_changed(event))

        assert _SENSOR in coord._door_open_timers, "a genuine open must still start a debounce timer"
        assert coord._sensor_debounce_pending() is True, (
            "a genuine off->on transition must still read as debounce-pending (Issue #623) — "
            "this fix must only exclude reconnect blips, not weaken real transient-open protection"
        )

    def test_old_state_none_is_not_treated_as_blip(self):
        """A first-ever observed transition (old_state=None) is deliberately NOT exempted —
        only unavailable/unknown are confirmed blip signatures; treating None as a blip too
        would risk silently skipping debounce on a genuine first-time open."""
        now = datetime(2026, 8, 15, 8, 0, 0)
        new_state = _make_state("on", now)

        coord = _make_coordinator_stub()
        coord.hass.states.get = MagicMock(return_value=new_state)

        event = _make_event(_SENSOR, None, new_state)
        with patch(_PATCH_CALL_LATER) as mock_call_later, patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            mock_call_later.return_value = MagicMock()
            asyncio.run(coord._async_door_window_changed(event))

        assert _SENSOR in coord._door_open_timers
        assert coord._sensor_debounce_pending() is True
