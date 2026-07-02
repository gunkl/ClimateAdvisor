"""Tests for Issue #359 Fix E: WHF Type 2 dual-entity support in _get_fan_physical_state().

WHF Type 1: single fan_entity for both command and state reading.
WHF Type 2: separate fan_entity (command) and fan_state_entity (physical on/off reading).

_get_fan_physical_state() must:
  - When fan_state_entity is configured: read that entity's state for physical on/off.
  - When fan_state_entity is unavailable/unknown: fall back to fan_entity and log a WARNING
    (once per unavailability, using _fan_state_entity_unavailable_warned flag).
  - When only fan_entity is configured (Type 1): read fan_entity state directly.

Coordinator infrastructure: ClimateAdvisorCoordinator cannot be instantiated in test stubs.
We test _get_fan_physical_state() and _async_fan_entity_changed() by binding them to a
minimal stub coordinator (same pattern as test_fan_cancel.py).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 28, 8, 0, 0)

from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_FAN_STATE_ENTITY,
    CONF_FAN_STATE_FEEDBACK,
    FAN_MODE_WHOLE_HOUSE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    coro.close()


def _make_fake_state(state_str: str, attributes: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.state = state_str
    s.attributes = attributes or {}
    return s


def _make_coord_stub(config: dict) -> MagicMock:
    """Build a minimal coordinator stub with the given config."""
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    coord = MagicMock()
    coord.hass = hass
    coord.config = config
    coord._fan_state_entity_unavailable_warned = False

    ae = MagicMock()
    ae._fan_active = False
    ae._fan_override_active = False
    ae._fan_command_pending = False
    ae.on_fan_turned_off = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._is_recent_fan_command = MagicMock(return_value=False)

    return coord


# ---------------------------------------------------------------------------
# TestWHFDualEntity
# ---------------------------------------------------------------------------


class TestWHFDualEntity:
    """Tests for _get_fan_physical_state() WHF Type 1 and Type 2 support (Issue #359 Fix E)."""

    def test_type1_get_fan_physical_state_reads_fan_entity(self):
        """Type 1 (no fan_state_entity): _get_fan_physical_state reads fan_entity state."""
        config = {CONF_FAN_ENTITY: "fan.whole_house"}
        coord = _make_coord_stub(config)

        fan_state = _make_fake_state("on")
        coord.hass.states.get = MagicMock(return_value=fan_state)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)

        result = method()
        assert result is True

    def test_type1_fan_entity_off_returns_false(self):
        """Type 1: fan_entity state 'off' → _get_fan_physical_state returns False."""
        config = {CONF_FAN_ENTITY: "fan.whole_house"}
        coord = _make_coord_stub(config)

        fan_state = _make_fake_state("off")
        coord.hass.states.get = MagicMock(return_value=fan_state)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)

        result = method()
        assert result is False

    def test_type2_get_fan_physical_state_uses_state_entity(self):
        """Type 2 (fan_state_entity configured): physical state comes from fan_state_entity."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
        }
        coord = _make_coord_stub(config)

        def _states_get(entity_id):
            if entity_id == "binary_sensor.whf_running":
                return _make_fake_state("on")
            if entity_id == "switch.whf_command":
                return _make_fake_state("off")  # command entity is off, state entity is on
            return None

        coord.hass.states.get = MagicMock(side_effect=_states_get)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)

        result = method()
        assert result is True, "Type 2 must read from fan_state_entity, not fan_entity"

    def test_type2_state_entity_off_returns_false(self):
        """Type 2: fan_state_entity='off' returns False even if fan_entity is on."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
        }
        coord = _make_coord_stub(config)

        def _states_get(entity_id):
            if entity_id == "binary_sensor.whf_running":
                return _make_fake_state("off")
            return _make_fake_state("on")  # command entity is on

        coord.hass.states.get = MagicMock(side_effect=_states_get)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)

        result = method()
        assert result is False

    def test_type2_state_entity_unavailable_falls_back_to_fan_entity(self):
        """Type 2: fan_state_entity='unavailable' falls back to fan_entity state."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
        }
        coord = _make_coord_stub(config)
        coord._fan_state_entity_unavailable_warned = False

        def _states_get(entity_id):
            if entity_id == "binary_sensor.whf_running":
                return _make_fake_state("unavailable")
            if entity_id == "switch.whf_command":
                return _make_fake_state("on")  # fallback entity is on
            return None

        coord.hass.states.get = MagicMock(side_effect=_states_get)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)

        result = method()
        assert result is True, "Should fall back to fan_entity when state_entity is unavailable"
        # Warning flag should be set so subsequent calls don't re-log
        assert coord._fan_state_entity_unavailable_warned is True

    def test_type2_state_entity_unknown_falls_back_to_fan_entity(self):
        """Type 2: fan_state_entity='unknown' falls back to fan_entity state."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
        }
        coord = _make_coord_stub(config)
        coord._fan_state_entity_unavailable_warned = False

        def _states_get(entity_id):
            if entity_id == "binary_sensor.whf_running":
                return _make_fake_state("unknown")
            if entity_id == "switch.whf_command":
                return _make_fake_state("off")
            return None

        coord.hass.states.get = MagicMock(side_effect=_states_get)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)

        result = method()
        assert result is False, "Fallback to fan_entity='off' → False"

    def test_type2_warned_flag_prevents_double_log(self):
        """Type 2 fallback: _fan_state_entity_unavailable_warned prevents redundant log."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
        }
        coord = _make_coord_stub(config)
        coord._fan_state_entity_unavailable_warned = True  # already warned

        def _states_get(entity_id):
            if entity_id == "binary_sensor.whf_running":
                return _make_fake_state("unavailable")
            return _make_fake_state("on")

        coord.hass.states.get = MagicMock(side_effect=_states_get)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)

        # Should still return fallback value without crashing
        result = method()
        assert result is True

    def test_type1_fan_entity_change_on_routes_to_handle_manual_override(self):
        """Type 1: fan_entity changes off→on while CA expects fan off → handle_fan_manual_override."""
        config = {CONF_FAN_ENTITY: "fan.whole_house"}
        coord = _make_coord_stub(config)
        ae = coord.automation_engine
        ae._fan_active = False
        ae._fan_override_active = False

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_entity_changed, coord)

        old_state = _make_fake_state("off")
        new_state = _make_fake_state("on")
        event = MagicMock()
        event.data = {"old_state": old_state, "new_state": new_state}

        asyncio.run(method(event))

        ae.handle_fan_manual_override.assert_called_once()
        ae.on_fan_turned_off.assert_not_called()

    def test_type2_state_entity_change_off_routes_to_on_fan_turned_off(self):
        """Type 2: fan_state_entity changes on→off while CA expects fan on → on_fan_turned_off.

        Note: _async_fan_entity_changed is registered for BOTH fan_entity and fan_state_entity.
        When fan_state_entity goes off→on or on→off with _fan_active=True, it should dispatch
        the same way as fan_entity changes.
        """
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
        }
        coord = _make_coord_stub(config)
        ae = coord.automation_engine
        ae._fan_active = True  # CA expects fan is on
        ae._fan_override_active = False

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_fan_entity_changed, coord)

        old_state = _make_fake_state("on")
        new_state = _make_fake_state("off")
        event = MagicMock()
        event.data = {"old_state": old_state, "new_state": new_state}

        asyncio.run(method(event))

        ae.on_fan_turned_off.assert_called_once()
        ae.handle_fan_manual_override.assert_not_called()


# ---------------------------------------------------------------------------
# TestComputeFanStatusWHF
# ---------------------------------------------------------------------------


class TestComputeFanStatusWHF:
    """Tests for WHF ground-truth fallback in _compute_fan_status() (Issue #363)."""

    def _make_whf_coord(self, config: dict) -> MagicMock:
        """Build a coord stub for _compute_fan_status tests with CA flags all clear."""
        coord = _make_coord_stub(config)
        ae = coord.automation_engine
        ae._fan_active = False
        ae._fan_override_active = False
        ae._natural_vent_active = False
        ae.config = config
        return coord

    def test_type2_status_running_untracked_when_state_entity_on(self):
        """Type 2: binary_sensor on + CA flags clear → 'running (untracked)'."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_STATE_FEEDBACK: True,
        }
        coord = self._make_whf_coord(config)

        def _states_get(entity_id):
            if entity_id == "binary_sensor.whf_running":
                return _make_fake_state("on")
            if entity_id == "switch.whf_command":
                return _make_fake_state("off")
            return None

        coord.hass.states.get = MagicMock(side_effect=_states_get)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        compute = types.MethodType(mod.ClimateAdvisorCoordinator._compute_fan_status, coord)
        get_physical = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)
        feedback_enabled = types.MethodType(mod.ClimateAdvisorCoordinator._fan_state_feedback_enabled, coord)
        coord._compute_fan_status = compute
        coord._get_fan_physical_state = get_physical
        coord._fan_state_feedback_enabled = feedback_enabled

        result = compute()
        assert result == "running (untracked)"

    def test_type2_status_inactive_when_state_entity_off(self):
        """Type 2: binary_sensor off + CA flags clear → 'inactive'."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_STATE_ENTITY: "binary_sensor.whf_running",
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_STATE_FEEDBACK: True,
        }
        coord = self._make_whf_coord(config)

        def _states_get(entity_id):
            if entity_id == "binary_sensor.whf_running":
                return _make_fake_state("off")
            return _make_fake_state("off")

        coord.hass.states.get = MagicMock(side_effect=_states_get)

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        compute = types.MethodType(mod.ClimateAdvisorCoordinator._compute_fan_status, coord)
        get_physical = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)
        feedback_enabled = types.MethodType(mod.ClimateAdvisorCoordinator._fan_state_feedback_enabled, coord)
        coord._compute_fan_status = compute
        coord._get_fan_physical_state = get_physical
        coord._fan_state_feedback_enabled = feedback_enabled

        result = compute()
        assert result == "inactive"

    def test_type1_status_running_untracked_when_fan_entity_on(self):
        """Type 1 (no fan_state_entity): fan_entity on + CA flags clear → 'running (untracked)'."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_STATE_FEEDBACK: True,
        }
        coord = self._make_whf_coord(config)

        coord.hass.states.get = MagicMock(return_value=_make_fake_state("on"))

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        compute = types.MethodType(mod.ClimateAdvisorCoordinator._compute_fan_status, coord)
        get_physical = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)
        feedback_enabled = types.MethodType(mod.ClimateAdvisorCoordinator._fan_state_feedback_enabled, coord)
        coord._compute_fan_status = compute
        coord._get_fan_physical_state = get_physical
        coord._fan_state_feedback_enabled = feedback_enabled

        result = compute()
        assert result == "running (untracked)"

    def test_command_only_mode_returns_inactive(self):
        """command-only mode (fan_state_feedback=False): _get_fan_physical_state returns None → 'inactive'."""
        config = {
            CONF_FAN_ENTITY: "switch.whf_command",
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_STATE_FEEDBACK: False,
        }
        coord = self._make_whf_coord(config)

        # fan_entity appears on, but feedback is disabled — should not count as running
        coord.hass.states.get = MagicMock(return_value=_make_fake_state("on"))

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        compute = types.MethodType(mod.ClimateAdvisorCoordinator._compute_fan_status, coord)
        get_physical = types.MethodType(mod.ClimateAdvisorCoordinator._get_fan_physical_state, coord)
        feedback_enabled = types.MethodType(mod.ClimateAdvisorCoordinator._fan_state_feedback_enabled, coord)
        coord._compute_fan_status = compute
        coord._get_fan_physical_state = get_physical
        coord._fan_state_feedback_enabled = feedback_enabled

        result = compute()
        assert result == "inactive"
