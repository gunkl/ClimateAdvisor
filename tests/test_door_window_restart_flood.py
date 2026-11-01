"""Issue #1048 change 19 -- HA-restart "all sensors closed" log flood.

Occupant impact: after an HA restart every contact sensor re-registers and reports
"closed" again, which wrote ~48 identical "all sensors closed" rows to the Activity Log
(3 of 4 restarts in the reporter's log). Only a *real* close should be logged -- but a
pause persisted across the restart must still clear, and a missing prior state must
still be processed.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

_SENSOR = "binary_sensor.front_door"


def _cls():
    return importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator


def _state(value: str) -> MagicMock:
    s = MagicMock()
    s.state = value
    s.last_changed = None
    return s


def _event(old: str | None, new: str) -> MagicMock:
    ev = MagicMock()
    ev.data = {"entity_id": _SENSOR, "new_state": _state(new), "old_state": None if old is None else _state(old)}
    return ev


def _make(*, live: str, paused: bool = False, nat_vent: bool = False, inverted: bool = False):
    """Real _state_is_open/_is_sensor_open/_async_door_window_changed on a partial coordinator."""
    cls = _cls()

    def _consume(coro):
        coro.close()

    coord = object.__new__(cls)
    coord.hass = MagicMock()
    coord.hass.async_create_task = MagicMock(side_effect=_consume)
    coord.hass.states.get = MagicMock(return_value=_state(live))
    coord.config = {"sensor_polarity_inverted": inverted}
    ae = MagicMock()
    ae._paused_by_door = paused
    ae._natural_vent_active = nat_vent
    ae._fan_active = False
    ae._fan_override_active = False
    ae.handle_all_doors_windows_closed = AsyncMock()
    coord.automation_engine = ae
    coord._today_record = None
    coord._resolved_sensors = [_SENSOR]
    coord._door_open_timers = {}
    coord._door_open_timer_expiry = {}
    coord._sensor_reconnect_blip_last_changed = {}
    coord._async_save_state = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    for name in ("_state_is_open", "_is_sensor_open", "_async_door_window_changed"):
        setattr(coord, name, types.MethodType(getattr(cls, name), coord))
    return coord


def _run(coord, ev) -> int:
    asyncio.run(coord._async_door_window_changed(ev))
    return coord.automation_engine.handle_all_doors_windows_closed.await_count


class TestRestartFlood:
    def test_plain_closed_to_closed_is_skipped(self):
        assert _run(_make(live="off"), _event("off", "off")) == 0

    def test_unavailable_to_closed_is_skipped_when_idle(self):
        assert _run(_make(live="off"), _event("unavailable", "off")) == 0

    def test_old_state_none_still_proceeds(self):
        assert _run(_make(live="off"), _event(None, "off")) == 1

    def test_real_close_open_to_closed_proceeds(self):
        assert _run(_make(live="off"), _event("on", "off")) == 1

    def test_open_to_unavailable_holds_the_pause(self):
        """open -> unavailable still runs the handler, but the dark sensor last read open so nothing resumes.

        Issue #1181: this used to resume HVAC (an unavailable sensor read closed) with the window still open.
        The real close afterwards (test_real_close_open_to_closed_proceeds) still resumes.
        """
        assert _run(_make(live="unavailable"), _event("on", "unavailable")) == 0

    def test_restart_while_paused_still_clears(self):
        """A pause persisted across the restart must be cleared by the re-reported close."""
        assert _run(_make(live="off", paused=True), _event("unavailable", "off")) == 1

    def test_nat_vent_active_still_proceeds(self):
        assert _run(_make(live="off", nat_vent=True), _event("off", "off")) == 1

    def test_inverted_polarity_old_state_open_proceeds(self):
        """With inverted polarity 'off' is OPEN, so off -> on is a real close."""
        assert _run(_make(live="on", inverted=True), _event("off", "on")) == 1
        assert _run(_make(live="on", inverted=True), _event("on", "on")) == 0

    def test_state_is_open_shared_with_is_sensor_open(self):
        coord = _make(live="on")
        assert coord._is_sensor_open(_SENSOR) is True
        assert coord._state_is_open(None) is False
        assert coord._state_is_open(_state("unavailable")) is False
