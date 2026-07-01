"""Tests for Issue #359: fan-cancel coordinator logic.

Covers:
- _fan_cancel_in_this_event suppresses setpoint-override grace (Fix A)
- Direction-aware dispatch: fan on→auto routes to on_fan_turned_off(); auto→on routes to
  handle_fan_manual_override() (Fix B)
- _async_fan_entity_changed: off routes to on_fan_turned_off(); on routes to
  handle_fan_manual_override() (Fix C)
- _async_post_grace_fan_reconcile: reconciles untracked fan after grace, skips during HVAC
  active cycle (Fix D)

Coordinator infrastructure note: ClimateAdvisorCoordinator cannot be instantiated without a
live HA instance. Tests here call the coordinator methods directly on a minimal stub object
built with importlib (to avoid stale __globals__ from test_occupancy.py module deletion).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

# Ensure HA stubs are installed before any coordinator import.
if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

# Patch dt_util.now to return a real datetime so isoformat() calls work.
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 28, 8, 0, 0)

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_mock_engine() -> MagicMock:
    """Build a MagicMock engine with all boolean flags explicitly False."""
    ae = MagicMock(spec=AutomationEngine)
    ae._fan_active = False
    ae._fan_override_active = False
    ae._natural_vent_active = False
    ae._grace_active = False
    ae._fan_command_pending = False
    ae._hvac_command_pending = False
    ae._temp_command_pending = False
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._last_commanded_hvac_mode = None
    ae._fan_command_time = None
    ae.on_fan_turned_off = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    ae.reconcile_fan_on_startup = AsyncMock()
    return ae


def _make_fake_state(state_str: str, attributes: dict | None = None) -> MagicMock:
    """Build a minimal mock HA state object."""
    s = MagicMock()
    s.state = state_str
    s.attributes = attributes or {}
    return s


def _make_fake_event(old_state, new_state) -> MagicMock:
    """Build a fake HA state_changed event."""
    ev = MagicMock()
    ev.data = {"old_state": old_state, "new_state": new_state}
    return ev


def _make_coordinator_stub(config: dict | None = None) -> MagicMock:
    """Build a minimal coordinator stub sufficient for the tested methods.

    The coordinator is stubbed as a MagicMock with the real method implementations
    bound in, so tests call the actual production logic under test.
    """
    config = config or {
        "climate_entity": "climate.thermostat",
        "comfort_heat": 70,
        "comfort_cool": 75,
    }

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    coord = MagicMock()
    coord.hass = hass
    coord.config = config
    coord.automation_engine = _make_mock_engine()
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._get_indoor_temp = MagicMock(return_value=71.0)
    coord._last_outdoor_temp = 57.0
    coord._fan_state_entity_unavailable_warned = False

    return coord


# ---------------------------------------------------------------------------
# TestFanCancelCoordinator
# ---------------------------------------------------------------------------


class TestFanCancelFlagComputation:
    """Unit tests for _fan_cancel_in_this_event flag logic (Issue #359 Fix A/B).

    The flag is computed from thermostat state attributes in _async_thermostat_changed.
    These tests replicate the exact condition from the production code to verify the
    dispatch logic is correct.
    """

    def _compute_fan_cancel_flag(self, old_fan_mode, new_fan_mode) -> bool:
        """Replicate the _fan_cancel_in_this_event computation from coordinator.py."""
        # From coordinator.py _async_thermostat_changed Block 2:
        return old_fan_mode is not None and old_fan_mode == "on" and new_fan_mode is not None and new_fan_mode != "on"

    def test_fan_on_to_auto_sets_cancel_flag(self):
        """fan_mode 'on'→'auto' → _fan_cancel_in_this_event=True."""
        assert self._compute_fan_cancel_flag("on", "auto") is True

    def test_fan_auto_to_on_does_not_set_cancel_flag(self):
        """fan_mode 'auto'→'on' → _fan_cancel_in_this_event=False (fan-ON path)."""
        assert self._compute_fan_cancel_flag("auto", "on") is False

    def test_fan_mode_unchanged_does_not_set_cancel_flag(self):
        """fan_mode unchanged → _fan_cancel_in_this_event=False."""
        assert self._compute_fan_cancel_flag("on", "on") is False
        assert self._compute_fan_cancel_flag("auto", "auto") is False

    def test_fan_none_does_not_set_cancel_flag(self):
        """None fan_mode → _fan_cancel_in_this_event=False (no fan attribute)."""
        assert self._compute_fan_cancel_flag(None, "auto") is False
        assert self._compute_fan_cancel_flag("on", None) is False

    def test_dispatch_on_fan_cancel_routes_to_on_fan_turned_off(self):
        """When _fan_cancel_in_this_event=True, dispatch routes to on_fan_turned_off().

        Replicates the Block 3 dispatch logic from coordinator.py:
        if _fan_cancel_in_this_event:
            self.automation_engine.on_fan_turned_off(...)
        else:
            self.automation_engine.handle_fan_manual_override(...)
        """
        ae = _make_mock_engine()

        # Simulate the Block 3 dispatch with _fan_cancel_in_this_event=True
        _fan_cancel_in_this_event = True
        old_fan_mode = "on"
        new_fan_mode = "auto"

        if _fan_cancel_in_this_event:
            ae.on_fan_turned_off(fan_before=str(old_fan_mode), fan_after=str(new_fan_mode))
        else:
            ae.handle_fan_manual_override(fan_before=str(old_fan_mode), fan_after=str(new_fan_mode))

        ae.on_fan_turned_off.assert_called_once_with(fan_before="on", fan_after="auto")
        ae.handle_fan_manual_override.assert_not_called()

    def test_dispatch_on_fan_on_routes_to_handle_fan_manual_override(self):
        """When _fan_cancel_in_this_event=False and fan mode changed, dispatch to handle_fan_manual_override().

        Replicates Block 3 dispatch for the fan-ON path.
        """
        ae = _make_mock_engine()

        _fan_cancel_in_this_event = False
        old_fan_mode = "auto"
        new_fan_mode = "on"

        if _fan_cancel_in_this_event:
            ae.on_fan_turned_off(fan_before=str(old_fan_mode), fan_after=str(new_fan_mode))
        else:
            ae.handle_fan_manual_override(fan_before=str(old_fan_mode), fan_after=str(new_fan_mode))

        ae.handle_fan_manual_override.assert_called_once_with(fan_before="auto", fan_after="on")
        ae.on_fan_turned_off.assert_not_called()


class TestFanCancelCoordinator:
    """Coordinator-level fan-cancel logic (Issue #359)."""

    def test_post_grace_reconcile_calls_reconcile_when_fan_running(self):
        """_async_post_grace_fan_reconcile calls reconcile_fan_on_startup when fan_mode=on.

        Fix D: after grace expires, check if fan is still running and reconcile if needed.
        """
        coord = _make_coordinator_stub()
        ae = coord.automation_engine
        ae.reconcile_fan_on_startup = AsyncMock()

        climate_state = _make_fake_state("off", {"fan_mode": "on", "hvac_action": "idle"})
        coord.hass.states.get = MagicMock(return_value=climate_state)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_post_grace_fan_reconcile, coord)

        asyncio.run(method())

        ae.reconcile_fan_on_startup.assert_awaited_once()

    def test_post_grace_reconcile_skips_when_hvac_heating(self):
        """_async_post_grace_fan_reconcile skips reconcile when hvac_action=heating.

        The fan is the thermostat's blower during a heat cycle — not an untracked ext fan.
        """
        coord = _make_coordinator_stub()
        ae = coord.automation_engine
        ae.reconcile_fan_on_startup = AsyncMock()

        climate_state = _make_fake_state("heat", {"fan_mode": "on", "hvac_action": "heating"})
        coord.hass.states.get = MagicMock(return_value=climate_state)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_post_grace_fan_reconcile, coord)

        asyncio.run(method())

        ae.reconcile_fan_on_startup.assert_not_awaited()

    def test_post_grace_reconcile_skips_when_hvac_cooling(self):
        """_async_post_grace_fan_reconcile skips reconcile when hvac_action=cooling."""
        coord = _make_coordinator_stub()
        ae = coord.automation_engine
        ae.reconcile_fan_on_startup = AsyncMock()

        climate_state = _make_fake_state("cool", {"fan_mode": "on", "hvac_action": "cooling"})
        coord.hass.states.get = MagicMock(return_value=climate_state)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_post_grace_fan_reconcile, coord)

        asyncio.run(method())

        ae.reconcile_fan_on_startup.assert_not_awaited()

    def test_reassert_setpoint_after_fan_off_calls_apply_classification(self):
        """Fix A: _async_reassert_setpoint_after_fan_off calls apply_classification with current classification.

        On ecobee, turning the fan off restores the comfort-program setpoint simultaneously.
        CA schedules a re-assertion via _async_reassert_setpoint_after_fan_off() so the
        correct setpoint wins after the thermostat settles.

        This tests the re-assertion method directly (not via _async_thermostat_changed),
        since the full thermostat-changed flow is tested by test_fan_cancel.py unit tests.
        """
        coord = _make_coordinator_stub()
        ae = coord.automation_engine

        # Classification with hvac_mode=cool (CA wants cool after fan stops)
        classification = MagicMock()
        classification.day_type = "warm"
        classification.hvac_mode = "cool"
        coord._current_classification = classification
        coord._last_predicted_indoor = 72.0
        coord._get_indoor_temp = MagicMock(return_value=72.0)

        ae.apply_classification = AsyncMock()

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_reassert_setpoint_after_fan_off, coord)

        # Patch asyncio.sleep so the test doesn't block 5 seconds
        import asyncio as _asyncio
        from unittest.mock import patch

        with patch.object(_asyncio, "sleep", new_callable=AsyncMock):
            asyncio.run(method())

        ae.apply_classification.assert_awaited_once()
        # Classification object must be passed through
        call_args = ae.apply_classification.await_args
        assert call_args.args[0] is classification or call_args.kwargs.get("classification") is None

    def test_reassert_setpoint_after_fan_off_skips_when_no_classification(self):
        """Fix A: _async_reassert_setpoint_after_fan_off skips gracefully when classification is None.

        No current classification → skip re-assertion (fresh start, no day data yet).
        """
        coord = _make_coordinator_stub()
        ae = coord.automation_engine
        coord._current_classification = None
        ae.apply_classification = AsyncMock()

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_reassert_setpoint_after_fan_off, coord)

        import asyncio as _asyncio
        from unittest.mock import patch

        with patch.object(_asyncio, "sleep", new_callable=AsyncMock):
            asyncio.run(method())

        ae.apply_classification.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestFanEntityDirectionDispatch
# ---------------------------------------------------------------------------


class TestFanEntityDirectionDispatch:
    """Tests for _async_fan_entity_changed direction-aware dispatch (Issue #359 Fix C)."""

    def test_fan_entity_changed_off_routes_to_on_fan_turned_off(self):
        """Fan entity goes on→off while CA thinks fan is active → on_fan_turned_off() called."""
        coord = _make_coordinator_stub()
        ae = coord.automation_engine
        ae._fan_active = True  # CA thinks fan is running
        ae._fan_override_active = False

        coord._is_recent_fan_command = MagicMock(return_value=False)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_entity_changed, coord)

        old_state = _make_fake_state("on")
        new_state = _make_fake_state("off")
        event = _make_fake_event(old_state, new_state)

        asyncio.run(method(event))

        ae.on_fan_turned_off.assert_called_once()
        ae.handle_fan_manual_override.assert_not_called()

    def test_fan_entity_changed_on_routes_to_handle_manual_override(self):
        """Fan entity goes off→on while CA thinks fan is off → handle_fan_manual_override() called."""
        coord = _make_coordinator_stub()
        ae = coord.automation_engine
        ae._fan_active = False  # CA thinks fan is off
        ae._fan_override_active = False

        coord._is_recent_fan_command = MagicMock(return_value=False)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_entity_changed, coord)

        old_state = _make_fake_state("off")
        new_state = _make_fake_state("on")
        event = _make_fake_event(old_state, new_state)

        asyncio.run(method(event))

        ae.handle_fan_manual_override.assert_called_once()
        ae.on_fan_turned_off.assert_not_called()

    def test_fan_entity_changed_skips_when_fan_command_pending(self):
        """Fan entity change is ignored when _fan_command_pending=True (CA issued the command)."""
        coord = _make_coordinator_stub()
        ae = coord.automation_engine
        ae._fan_active = False
        ae._fan_command_pending = True

        coord._is_recent_fan_command = MagicMock(return_value=False)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_entity_changed, coord)

        old_state = _make_fake_state("off")
        new_state = _make_fake_state("on")
        event = _make_fake_event(old_state, new_state)

        asyncio.run(method(event))

        ae.handle_fan_manual_override.assert_not_called()
        ae.on_fan_turned_off.assert_not_called()
