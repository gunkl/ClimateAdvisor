"""Tests for Issue #361: WHF command-only mode (fan_state_feedback=False).

When fan_state_feedback=False the fan entity only echoes the last command; it cannot
signal physical overrides. The coordinator must:
  - Suppress _async_fan_entity_changed override dispatch (echo, not an override signal)
  - Idempotently re-assert the desired fan state each 30-min cycle via _async_command_fan_entity
  - Reset _last_commanded_fan_state in post-grace so the next cycle re-asserts cleanly
  - Return None from _get_fan_physical_state (no physical feedback available)

Coordinator infrastructure: ClimateAdvisorCoordinator cannot be instantiated without a
live HA instance.  Real methods are bound to minimal MagicMock stubs via types.MethodType
(same pattern as test_fan_cancel.py and test_whf_dual_entity.py).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 28, 8, 0, 0)

from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_FAN_STATE_FEEDBACK,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
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


def _make_mock_engine() -> MagicMock:
    """MagicMock engine with all boolean flags explicitly False (unset MagicMock attrs are truthy)."""
    ae = MagicMock()
    ae._fan_active = False
    ae._fan_override_active = False
    ae._natural_vent_active = False
    ae._grace_active = False
    ae._fan_command_pending = False
    ae._hvac_command_pending = False
    ae._manual_override_active = False
    ae._fan_command_time = None
    ae.on_fan_turned_off = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    ae.reconcile_fan_on_startup = AsyncMock()
    return ae


def _make_coord_stub(config: dict | None = None) -> MagicMock:
    """Minimal coordinator stub for command-only mode tests.

    _fan_state_feedback_enabled is bound as a real method so production guards that
    call self._fan_state_feedback_enabled() read from coord.config, not a truthy MagicMock.
    """
    if config is None:
        config = {
            "climate_entity": "climate.thermostat",
            CONF_FAN_ENTITY: "switch.whf",
            CONF_FAN_MODE: "fan_only",
            CONF_FAN_STATE_FEEDBACK: False,
        }
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    coord = MagicMock()
    coord.hass = hass
    coord.config = config
    coord.automation_engine = _make_mock_engine()
    coord._last_commanded_fan_state = None
    coord._fan_state_entity_unavailable_warned = False
    coord._is_recent_fan_command = MagicMock(return_value=False)

    # Bind the real implementation so guards that call self._fan_state_feedback_enabled()
    # get the actual config value rather than a truthy MagicMock.
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord._fan_state_feedback_enabled = types.MethodType(
        mod.ClimateAdvisorCoordinator._fan_state_feedback_enabled, coord
    )

    return coord


def _bind(method_name: str, coord: MagicMock):
    """Bind a real coordinator method to a stub coord."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return types.MethodType(getattr(mod.ClimateAdvisorCoordinator, method_name), coord)


# ---------------------------------------------------------------------------
# TestFanStateFeedbackHelper
# ---------------------------------------------------------------------------


class TestFanStateFeedbackHelper:
    """Unit tests for _fan_state_feedback_enabled() and _get_fan_physical_state()."""

    def test_feedback_disabled_by_default(self):
        """No fan_state_feedback key in config → _fan_state_feedback_enabled() returns False."""
        coord = _make_coord_stub({"climate_entity": "climate.t", CONF_FAN_ENTITY: "switch.whf"})
        method = _bind("_fan_state_feedback_enabled", coord)
        assert method() is False

    def test_feedback_enabled_when_true(self):
        """fan_state_feedback=True → _fan_state_feedback_enabled() returns True."""
        coord = _make_coord_stub(
            {"climate_entity": "climate.t", CONF_FAN_ENTITY: "switch.whf", CONF_FAN_STATE_FEEDBACK: True}
        )
        method = _bind("_fan_state_feedback_enabled", coord)
        assert method() is True

    def test_get_fan_physical_state_returns_none_when_disabled(self):
        """fan_state_feedback=False → _get_fan_physical_state() returns None (no feedback available)."""
        coord = _make_coord_stub()
        method = _bind("_get_fan_physical_state", coord)
        result = method()
        assert result is None


# ---------------------------------------------------------------------------
# TestFanEntityChangedCommandOnly
# ---------------------------------------------------------------------------


class TestFanEntityChangedCommandOnly:
    """_async_fan_entity_changed must be a no-op when fan_state_feedback=False."""

    def _run_changed(self, coord: MagicMock, old_state_str: str, new_state_str: str):
        method = _bind("_async_fan_entity_changed", coord)
        old_state = _make_fake_state(old_state_str)
        new_state = _make_fake_state(new_state_str)
        event = MagicMock()
        event.data = {"old_state": old_state, "new_state": new_state}
        asyncio.run(method(event))

    def test_fan_entity_changed_suppressed_when_feedback_disabled(self):
        """Entity state changes are command echoes when feedback=False; no override dispatch."""
        coord = _make_coord_stub()
        ae = coord.automation_engine
        ae._fan_active = False

        self._run_changed(coord, "off", "on")

        ae.handle_fan_manual_override.assert_not_called()
        ae.on_fan_turned_off.assert_not_called()

    def test_fan_entity_changed_dispatches_when_feedback_enabled(self):
        """fan_state_feedback=True: off→on with CA fan off → handle_fan_manual_override called."""
        config = {
            "climate_entity": "climate.t",
            CONF_FAN_ENTITY: "switch.whf",
            CONF_FAN_MODE: "fan_only",
            CONF_FAN_STATE_FEEDBACK: True,
        }
        coord = _make_coord_stub(config)
        ae = coord.automation_engine
        ae._fan_active = False
        ae._fan_override_active = False
        ae._fan_command_pending = False
        coord._is_recent_fan_command = MagicMock(return_value=False)

        self._run_changed(coord, "off", "on")

        ae.handle_fan_manual_override.assert_called_once()


# ---------------------------------------------------------------------------
# TestCommandOnlyReconcile
# ---------------------------------------------------------------------------


class TestCommandOnlyReconcile:
    """Tests for the command-only reconcile block inside _async_update_data().

    _async_update_data is too complex to run fully in a stub environment.  We test
    _async_command_fan_entity directly and verify _last_commanded_fan_state transitions
    by calling it with the same conditions the reconcile block uses.
    """

    def test_command_only_commands_on_when_fan_active_and_no_last_command(self):
        """desired=on, last_commanded=None, no grace → turn_on issued and _last_commanded_fan_state=True."""
        coord = _make_coord_stub()
        ae = coord.automation_engine
        ae._fan_active = True
        ae._grace_active = False
        ae._fan_override_active = False
        coord._last_commanded_fan_state = None

        desired_on = bool(ae._fan_active)
        grace_on = bool(ae._grace_active)
        override_on = bool(ae._fan_override_active)
        last_cmd = coord._last_commanded_fan_state

        async def _run():
            if desired_on and last_cmd is not True and not grace_on and not override_on:
                method = _bind("_async_command_fan_entity", coord)
                await method(on=True)
                coord._last_commanded_fan_state = True

        asyncio.run(_run())

        coord.hass.services.async_call.assert_awaited_once()
        call_kwargs = coord.hass.services.async_call.await_args
        assert call_kwargs.args[1] == "turn_on"
        assert coord._last_commanded_fan_state is True

    def test_command_only_no_command_when_already_commanded_on(self):
        """desired=on, last_commanded=True → idempotent: no new command issued."""
        coord = _make_coord_stub()
        ae = coord.automation_engine
        ae._fan_active = True
        ae._grace_active = False
        ae._fan_override_active = False
        coord._last_commanded_fan_state = True

        desired_on = bool(ae._fan_active)
        grace_on = bool(ae._grace_active)
        override_on = bool(ae._fan_override_active)
        last_cmd = coord._last_commanded_fan_state

        async def _run():
            if desired_on and last_cmd is not True and not grace_on and not override_on:
                method = _bind("_async_command_fan_entity", coord)
                await method(on=True)
                coord._last_commanded_fan_state = True

        asyncio.run(_run())

        coord.hass.services.async_call.assert_not_awaited()

    def test_command_only_commands_off_when_fan_inactive_and_no_last_command(self):
        """desired=off, last_commanded=None, no grace → turn_off issued and _last_commanded_fan_state=False."""
        coord = _make_coord_stub()
        ae = coord.automation_engine
        ae._fan_active = False
        ae._grace_active = False
        ae._fan_override_active = False
        coord._last_commanded_fan_state = None

        desired_on = bool(ae._fan_active)
        grace_on = bool(ae._grace_active)
        override_on = bool(ae._fan_override_active)
        last_cmd = coord._last_commanded_fan_state

        async def _run():
            if not desired_on and last_cmd is not False and not grace_on and not override_on:
                method = _bind("_async_command_fan_entity", coord)
                await method(on=False)
                coord._last_commanded_fan_state = False

        asyncio.run(_run())

        coord.hass.services.async_call.assert_awaited_once()
        call_kwargs = coord.hass.services.async_call.await_args
        assert call_kwargs.args[1] == "turn_off"
        assert coord._last_commanded_fan_state is False

    def test_command_only_skips_when_grace_active(self):
        """Grace active → command-only reconcile must not issue any command."""
        coord = _make_coord_stub()
        ae = coord.automation_engine
        ae._fan_active = True
        ae._grace_active = True
        ae._fan_override_active = False
        coord._last_commanded_fan_state = None

        desired_on = bool(ae._fan_active)
        grace_on = bool(ae._grace_active)
        override_on = bool(ae._fan_override_active)
        last_cmd = coord._last_commanded_fan_state

        async def _run():
            if desired_on and last_cmd is not True and not grace_on and not override_on:
                method = _bind("_async_command_fan_entity", coord)
                await method(on=True)
                coord._last_commanded_fan_state = True

        asyncio.run(_run())

        coord.hass.services.async_call.assert_not_awaited()

    def test_command_only_skips_when_feedback_enabled(self):
        """fan_state_feedback=True: command-only reconcile path is not taken."""
        config = {
            "climate_entity": "climate.t",
            CONF_FAN_ENTITY: "switch.whf",
            CONF_FAN_MODE: "fan_only",
            CONF_FAN_STATE_FEEDBACK: True,
        }
        coord = _make_coord_stub(config)
        ae = coord.automation_engine
        ae._fan_active = True
        ae._grace_active = False
        ae._fan_override_active = False
        coord._last_commanded_fan_state = None

        # The reconcile block only executes when _fan_state_feedback_enabled() is False
        feedback_method = _bind("_fan_state_feedback_enabled", coord)
        assert feedback_method() is True, "Precondition: feedback is enabled"

        # With feedback enabled the block is not entered; no command should be issued
        async def _run():
            if not feedback_method():
                method = _bind("_async_command_fan_entity", coord)
                await method(on=True)
                coord._last_commanded_fan_state = True

        asyncio.run(_run())

        coord.hass.services.async_call.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestPostGraceCommandOnly
# ---------------------------------------------------------------------------


class TestPostGraceCommandOnly:
    """_async_post_grace_fan_reconcile must reset _last_commanded_fan_state when feedback=False."""

    def test_post_grace_resets_last_commanded_when_feedback_false(self):
        """Post-grace with command-only: _last_commanded_fan_state becomes None for next cycle."""
        coord = _make_coord_stub()
        coord._last_commanded_fan_state = True

        method = _bind("_async_post_grace_fan_reconcile", coord)
        asyncio.run(method())

        assert coord._last_commanded_fan_state is None

    def test_post_grace_does_not_call_reconcile_on_startup_when_feedback_false(self):
        """Post-grace command-only path must NOT call reconcile_fan_on_startup (no state to read)."""
        coord = _make_coord_stub()
        ae = coord.automation_engine
        coord._last_commanded_fan_state = True

        method = _bind("_async_post_grace_fan_reconcile", coord)
        asyncio.run(method())

        ae.reconcile_fan_on_startup.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestWHFModeInData
# ---------------------------------------------------------------------------


class TestWHFModeInData:
    """whf_mode field in coordinator data reflects command-only vs state-feedback vs disabled."""

    def _whf_mode(self, config: dict, fan_mode_val: str | None = "fan_only") -> str | None:
        """Replicate the whf_mode ternary from _async_update_data using the real helper."""
        if fan_mode_val is not None:
            config = {**config, CONF_FAN_MODE: fan_mode_val}
        coord = _make_coord_stub(config)
        feedback_method = _bind("_fan_state_feedback_enabled", coord)
        fan_mode_cfg = coord.config.get(CONF_FAN_MODE, "")
        if fan_mode_cfg in ("", "none", None, FAN_MODE_DISABLED):
            return "disabled"
        return "state-feedback" if feedback_method() else "command-only"

    def test_whf_mode_command_only_when_feedback_false(self):
        """fan_mode active + fan_state_feedback=False → whf_mode='command-only'."""
        config = {CONF_FAN_ENTITY: "switch.whf", CONF_FAN_STATE_FEEDBACK: False}
        assert self._whf_mode(config, fan_mode_val="fan_only") == "command-only"

    def test_whf_mode_state_feedback_when_feedback_true(self):
        """fan_mode active + fan_state_feedback=True → whf_mode='state-feedback'."""
        config = {CONF_FAN_ENTITY: "switch.whf", CONF_FAN_STATE_FEEDBACK: True}
        assert self._whf_mode(config, fan_mode_val="fan_only") == "state-feedback"

    def test_whf_mode_disabled_when_fan_mode_disabled(self):
        """fan_mode=disabled → whf_mode='disabled' regardless of fan_state_feedback."""
        config = {CONF_FAN_ENTITY: "switch.whf", CONF_FAN_STATE_FEEDBACK: False}
        assert self._whf_mode(config, fan_mode_val=FAN_MODE_DISABLED) == "disabled"


# ---------------------------------------------------------------------------
# TestWarningBannerScope
# ---------------------------------------------------------------------------


class TestWarningBannerScope:
    """Warning banner in build_event_timeline_table must only appear for WHF modes."""

    def _build_table(self, config: dict) -> str:
        import datetime as _dt

        from custom_components.climate_advisor.ai_skills_activity import (
            build_event_timeline_table,
        )

        return build_event_timeline_table([], config, hours=12, now=_dt.datetime(2026, 6, 30, 12, 0, 0))

    def test_warning_shown_for_whole_house_fan_with_entity_and_no_feedback(self):
        """fan_mode=whole_house_fan + fan_entity set + fan_state_feedback=False → warning shown."""
        config = {
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "switch.whf",
            CONF_FAN_STATE_FEEDBACK: False,
        }
        table = self._build_table(config)
        assert "Whole house fan state feedback disabled" in table

    def test_warning_shown_for_both_mode_with_entity(self):
        """fan_mode=both + fan_entity set + fan_state_feedback=False → warning shown."""
        config = {
            CONF_FAN_MODE: FAN_MODE_BOTH,
            CONF_FAN_ENTITY: "switch.whf",
            CONF_FAN_STATE_FEEDBACK: False,
        }
        table = self._build_table(config)
        assert "Whole house fan state feedback disabled" in table

    def test_warning_not_shown_for_hvac_fan_mode(self):
        """fan_mode=hvac_fan → warning must NOT appear (fan_state_feedback is irrelevant)."""
        config = {
            CONF_FAN_MODE: FAN_MODE_HVAC,
            CONF_FAN_STATE_FEEDBACK: False,
        }
        table = self._build_table(config)
        assert "disabled" not in table

    def test_warning_not_shown_when_fan_entity_missing(self):
        """fan_mode=whole_house_fan but no fan_entity → warning must NOT appear."""
        config = {
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "",
            CONF_FAN_STATE_FEEDBACK: False,
        }
        table = self._build_table(config)
        assert "disabled" not in table

    def test_warning_not_shown_when_feedback_enabled(self):
        """fan_state_feedback=True → warning must NOT appear even with WHF entity."""
        config = {
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "switch.whf",
            CONF_FAN_STATE_FEEDBACK: True,
        }
        table = self._build_table(config)
        assert "disabled" not in table
