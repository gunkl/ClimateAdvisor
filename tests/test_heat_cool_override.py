"""Regression tests for Issue #269 — four heat_cool override visibility bugs.

Bug A: Fan mode change suppression window too short (30s) for cloud thermostats.
       Fix: also guard on _is_expected_confirmation (120s window).

Bug B: hvac_mode missing from coordinator.data dict.
       Fix: add "hvac_mode": hvac_mode entry to the result dict in _async_update_data.

Bug C: heat_cool → cool mode switch not detected as manual override.
       Fix: compare against _last_commanded_hvac_mode instead of classification.hvac_mode.

Bug D: Dual setpoint changes (target_temp_high/low) invisible in heat_cool mode.
       Fix: detect target_temp_high/low changes when state == "heat_cool".
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Provide a stable dt_util.now so isoformat() calls work
_NOW_BASE = datetime(2026, 6, 11, 14, 0, 0, tzinfo=UTC)
sys.modules["homeassistant.util.dt"].now = lambda: _NOW_BASE

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.learning import DailyRecord  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────

_THERMOSTAT_ID = "climate.thermostat"
_PATCH_CALLBACK = "custom_components.climate_advisor.coordinator.callback"
_PATCH_DT_UTIL = "custom_components.climate_advisor.coordinator.dt_util"


# ── Shared helpers ────────────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close a coroutine to prevent 'never awaited' RuntimeWarning."""
    if asyncio.iscoroutine(coro):
        coro.close()


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class (fresh import each call)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__ validation."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "hot",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 96,
        "today_low": 72,
        "tomorrow_high": 94,
        "tomorrow_low": 70,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _make_coordinator_stub(
    *,
    # Automation engine flags
    hvac_command_pending: bool = False,
    temp_command_pending: bool = False,
    fan_command_pending: bool = False,
    fan_override_active: bool = False,
    manual_override_active: bool = False,
    override_confirm_pending: bool = False,
    # _last_commanded_hvac_mode / time (for _is_expected_confirmation and Bug C/D)
    last_commanded_hvac_mode: str | None = None,
    last_commanded_hvac_time: datetime | None = None,
    # Classification
    classification: DayClassification | None = None,
    # _is_recent_hvac_command behaviour
    hvac_command_age_seconds: float | None = None,
):
    """Build a minimal coordinator-like object for testing _async_thermostat_changed."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": _THERMOSTAT_ID,
        "weather_entity": "weather.forecast_home",
        "comfort_heat": 70,
        "comfort_cool": 75,
    }

    # Automation engine — MagicMock (NOT AsyncMock) per project convention
    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = hvac_command_pending
    ae._manual_override_active = manual_override_active
    ae._override_confirm_pending = override_confirm_pending
    ae._pause_active = False
    ae._fan_command_pending = fan_command_pending
    ae._fan_override_active = fan_override_active
    ae._temp_command_pending = temp_command_pending
    ae._temp_command_time = None
    ae._fan_command_time = None  # no recent fan command by default
    ae._fan_active = False
    ae._natural_vent_active = False
    # Bug A / C / D — explicit values (not truthy MagicMock defaults)
    ae._last_commanded_hvac_mode = last_commanded_hvac_mode
    ae._last_commanded_hvac_time = last_commanded_hvac_time
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._current_classification = classification if classification is not None else _make_classification()
    coord._today_record = DailyRecord(date="2026-06-11", day_type="hot", trend_direction="stable")
    coord._async_save_state = AsyncMock()

    coord._is_recent_temp_command = MagicMock(return_value=False)
    coord._is_recent_fan_command = MagicMock(return_value=False)

    if hvac_command_age_seconds is None:
        coord._is_recent_hvac_command = MagicMock(return_value=False)
    else:

        def _is_recent(threshold_seconds: float = 3.0) -> bool:
            return hvac_command_age_seconds < threshold_seconds

        coord._is_recent_hvac_command = _is_recent

    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    # _end_hvac_active_phase and _abandon_observation are sync calls (not awaited)
    coord._end_hvac_active_phase = MagicMock()
    coord._abandon_observation = MagicMock()
    coord._get_indoor_temp = MagicMock(return_value=76.0)
    coord._get_outdoor_temp = MagicMock(return_value=96.0)
    coord._chart_log = MagicMock()
    # Suppress chart helpers — wrapped in contextlib.suppress in coordinator anyway
    coord._read_chart_hvac_action = MagicMock(return_value="cool")
    coord._fan_is_running = MagicMock(return_value=False)
    coord._any_sensor_open = MagicMock(return_value=False)

    # Bind the real method under test
    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)

    return coord


def _make_event(
    old_hvac_mode: str = "heat_cool",
    new_hvac_mode: str = "heat_cool",
    old_temp: float | None = None,
    new_temp: float | None = None,
    old_target_high: float | None = None,
    new_target_high: float | None = None,
    old_target_low: float | None = None,
    new_target_low: float | None = None,
    old_fan_mode: str | None = None,
    new_fan_mode: str | None = None,
):
    """Build a minimal HA state-change event for thermostat changes."""
    old_attrs: dict = {"hvac_action": ""}
    new_attrs: dict = {"hvac_action": ""}

    if old_temp is not None:
        old_attrs["temperature"] = old_temp
    if new_temp is not None:
        new_attrs["temperature"] = new_temp
    if old_target_high is not None:
        old_attrs["target_temp_high"] = old_target_high
    if new_target_high is not None:
        new_attrs["target_temp_high"] = new_target_high
    if old_target_low is not None:
        old_attrs["target_temp_low"] = old_target_low
    if new_target_low is not None:
        new_attrs["target_temp_low"] = new_target_low
    if old_fan_mode is not None:
        old_attrs["fan_mode"] = old_fan_mode
    if new_fan_mode is not None:
        new_attrs["fan_mode"] = new_fan_mode

    old_state = MagicMock()
    old_state.state = old_hvac_mode
    old_state.attributes = old_attrs

    new_state = MagicMock()
    new_state.state = new_hvac_mode
    new_state.attributes = new_attrs

    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}
    return event


# ═══════════════════════════════════════════════════════════════════════════════
# Bug A — Fan mode change suppression window (120s expected-confirmation guard)
# ═══════════════════════════════════════════════════════════════════════════════


class TestBugAFanOverrideSuppression:
    """Fan mode changes arriving within 120s of CA's HVAC command are CA side-effects,
    not user overrides. Changes arriving after 120s are genuine user overrides.

    Without the fix, a cloud thermostat's fan_mode side-effect arriving at 35–90s
    (after the 30s _is_recent_hvac_command window) incorrectly fires handle_fan_manual_override.
    The fix adds `and not _is_expected_confirmation` to the fan_mode guard.
    """

    def test_fan_mode_change_within_120s_of_hvac_command_suppressed(self):
        """Fan mode change arriving 60s after CA HVAC command → NOT a manual override.

        _is_expected_confirmation is True (new_state.state == last_commanded_mode,
        within 120s), so handle_fan_manual_override must NOT be called.
        """
        # HVAC command 60s ago; dt_util.now() patched so time math works
        cmd_time = _NOW_BASE - timedelta(seconds=60)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
            hvac_command_age_seconds=60.0,  # past 30s _is_recent_hvac_command window
        )

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="heat_cool",  # matches last_commanded → _is_expected_confirmation True
            old_fan_mode="auto",
            new_fan_mode="on",
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_fan_manual_override.call_count == 0, (
            "handle_fan_manual_override() must NOT be called when the fan_mode change "
            "arrives within 120s of a CA HVAC command (cloud thermostat side-effect). "
            "Bug A fix: add `and not _is_expected_confirmation` to the fan guard."
        )

    def test_fan_mode_change_after_120s_is_genuine_override(self):
        """Fan mode change arriving 130s after CA HVAC command → IS a manual override.

        _is_expected_confirmation is False (>120s), handle_fan_manual_override must fire.
        """
        cmd_time = _NOW_BASE - timedelta(seconds=130)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
            hvac_command_age_seconds=130.0,  # also past _is_recent_hvac_command 30s window
        )

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="heat_cool",
            old_fan_mode="auto",
            new_fan_mode="on",
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_fan_manual_override.call_count == 1, (
            "handle_fan_manual_override() SHOULD be called when fan_mode changes 130s "
            "after a CA HVAC command (past the 120s _is_expected_confirmation window). "
            "Bug A fix must not suppress genuine overrides outside the window."
        )

    def test_fan_mode_change_with_no_prior_command_fires_override(self):
        """Fan mode change with no prior CA command at all → genuine override fires."""
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode=None,
            last_commanded_hvac_time=None,
        )

        event = _make_event(
            old_hvac_mode="cool",
            new_hvac_mode="cool",
            old_fan_mode="auto",
            new_fan_mode="on",
        )

        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_fan_manual_override.call_count == 1, (
            "handle_fan_manual_override() must fire when there is no prior CA command "
            "and no recent HVAC command (pure manual fan override)."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Bug B — hvac_mode in coordinator.data
# ═══════════════════════════════════════════════════════════════════════════════


class TestBugBHvacModeInData:
    """hvac_mode must appear in coordinator.data after _async_update_data.

    Without the fix, _detect_and_emit_incidents() reads current_data.get("hvac_mode") → None,
    making HVAC mode incidents invisible.
    """

    def test_hvac_mode_key_present_in_result_dict(self):
        """The result dict built in _async_update_data must include 'hvac_mode'."""
        # We test this by reading the coordinator.py source and confirming the key is set,
        # and by building a minimal result dict replica to verify key presence.
        # (Full coordinator instantiation requires live HA; we use source inspection instead.)
        import ast
        import pathlib

        coordinator_path = pathlib.Path(__file__).parent.parent / "custom_components/climate_advisor/coordinator.py"
        source = coordinator_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Walk the AST looking for the result dict assignment
        found = False
        for node in ast.walk(tree):
            # Look for: result = { ..., "hvac_mode": hvac_mode, ... }
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "result" and isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant) and key.value == "hvac_mode":
                                found = True
        assert found, (
            "The 'result' dict in _async_update_data must include the key 'hvac_mode'. "
            'Bug B fix: add `"hvac_mode": hvac_mode` between ATTR_HVAC_ACTION and '
            "ATTR_HVAC_RUNTIME_TODAY entries."
        )

    def test_hvac_mode_key_is_string_not_attr_constant(self):
        """The hvac_mode key must be the literal string 'hvac_mode', not a module constant.

        _detect_and_emit_incidents() reads current_data.get('hvac_mode') by string key.
        """
        import ast
        import pathlib

        coordinator_path = pathlib.Path(__file__).parent.parent / "custom_components/climate_advisor/coordinator.py"
        source = coordinator_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "result" and isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant) and key.value == "hvac_mode":
                                return  # found string literal key — pass
        # If we reach here without finding it as a string literal:
        raise AssertionError(
            "The key 'hvac_mode' in the result dict must be a plain string literal "
            "so that current_data.get('hvac_mode') works in _detect_and_emit_incidents()."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Bug C — heat_cool → cool mode switch detected as override
# ═══════════════════════════════════════════════════════════════════════════════


class TestBugCHeatCoolModeSwitchDetection:
    """heat_cool → cool transition must be detected as a manual override.

    Without the fix: new_state.state ("cool") != classification.hvac_mode ("cool") = False
    → no override.

    With the fix: compare against _last_commanded_hvac_mode ("heat_cool"),
    so "cool" != "heat_cool" = True → override fires.
    """

    def test_heat_cool_to_cool_fires_override_when_ca_commanded_heat_cool(self):
        """User switches heat_cool → cool while CA last commanded heat_cool.

        Occupant experience: housemate turns off the heating band on a 96°F day
        and CA must recognize this as an intentional override.
        """
        cmd_time = _NOW_BASE - timedelta(minutes=10)  # outside 120s window
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
            classification=_make_classification(hvac_mode="cool"),
        )
        # The mode change is old enough that _is_expected_confirmation is False
        # (new_state.state == "cool" != "heat_cool" == last_commanded_mode)
        coord._is_recent_hvac_command = MagicMock(return_value=False)

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="cool",
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_manual_override.call_count == 1, (
            "handle_manual_override() must fire when the thermostat switches from "
            "heat_cool to cool while CA last commanded heat_cool. "
            "Bug C fix: compare against _last_commanded_hvac_mode, not classification.hvac_mode. "
            "Without the fix, 'cool' == classification.hvac_mode ('cool') → no override detected."
        )

    def test_heat_cool_to_cool_no_prior_command_uses_classification_fallback(self):
        """When no prior CA command exists, fall back to classification.hvac_mode for comparison.

        If classification says 'cool' and thermostat switches to 'cool', no override
        (the thermostat is doing what CA would want). This verifies the fallback logic.
        """
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode=None,
            last_commanded_hvac_time=None,
            classification=_make_classification(hvac_mode="cool"),
        )
        coord._is_recent_hvac_command = MagicMock(return_value=False)

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="cool",  # matches classification fallback "cool"
        )

        # _last_cmd_mode is None → _is_expected_confirmation short-circuits, no now() call needed
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn):
            asyncio.run(coord._async_thermostat_changed(event))

        # new_state.state "cool" == classification.hvac_mode "cool" → no override
        assert coord.automation_engine.handle_manual_override.call_count == 0, (
            "When no prior CA command exists and thermostat switches to the mode "
            "classification already wants ('cool'), no override should be detected. "
            "The fallback to classification.hvac_mode must work correctly."
        )

    def test_cool_to_off_fires_override_when_ca_commanded_heat_cool(self):
        """User turns thermostat off while CA last commanded heat_cool → override."""
        cmd_time = _NOW_BASE - timedelta(minutes=10)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
            classification=_make_classification(hvac_mode="cool"),
        )
        coord._is_recent_hvac_command = MagicMock(return_value=False)

        event = _make_event(
            old_hvac_mode="cool",
            new_hvac_mode="off",
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord.automation_engine.handle_manual_override.call_count == 1, (
            "Turning thermostat off while CA commanded heat_cool must register as override."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Bug D — Dual setpoint changes visible in heat_cool mode
# ═══════════════════════════════════════════════════════════════════════════════


class TestBugDHeatCoolSetpointDetection:
    """target_temp_high / target_temp_low changes in heat_cool mode must be detected.

    Without the fix, the guard reads attributes.get("temperature") which is None
    in heat_cool mode → None != None = False → no override detected.

    The occupant experience: a housemate on a 96°F day raises the cooling setpoint
    from 72 to 74, and CA never notices — no grace period, no learning signal.
    """

    def test_target_temp_high_change_increments_override_count(self):
        """target_temp_high 72 → 74 in heat_cool mode → manual_overrides incremented."""
        cmd_time = _NOW_BASE - timedelta(minutes=5)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
        )
        initial = coord._today_record.manual_overrides

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="heat_cool",
            old_target_high=72.0,
            new_target_high=74.0,
            old_target_low=68.0,
            new_target_low=68.0,
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial + 1, (
            f"Expected manual_overrides={initial + 1}, got {coord._today_record.manual_overrides}. "
            "Bug D fix: detect target_temp_high changes when state == 'heat_cool'. "
            "Without the fix, the guard reads 'temperature' (None in heat_cool) → no override."
        )

    def test_target_temp_low_change_increments_override_count(self):
        """target_temp_low 68 → 66 in heat_cool mode → manual_overrides incremented."""
        cmd_time = _NOW_BASE - timedelta(minutes=5)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
        )
        initial = coord._today_record.manual_overrides

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="heat_cool",
            old_target_high=72.0,
            new_target_high=72.0,
            old_target_low=68.0,
            new_target_low=66.0,
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial + 1, (
            f"Expected manual_overrides={initial + 1}, got {coord._today_record.manual_overrides}. "
            "target_temp_low change in heat_cool mode must also be detected."
        )

    def test_heat_cool_setpoint_fires_handle_manual_override(self):
        """target_temp_high change in heat_cool mode → handle_manual_override(source='setpoint') called.

        CA last commanded 'heat_cool', no override active, no pending flags.
        """
        cmd_time = _NOW_BASE - timedelta(minutes=5)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
            classification=_make_classification(hvac_mode="cool"),
        )

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="heat_cool",
            old_target_high=72.0,
            new_target_high=74.0,
            old_target_low=68.0,
            new_target_low=68.0,
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        ae = coord.automation_engine
        assert ae.handle_manual_override.call_count == 1, (
            "handle_manual_override() must be called when target_temp_high changes in "
            "heat_cool mode and CA is actively controlling heat_cool. "
            "Bug D fix: use _last_commanded_hvac_mode for the mode comparison guard."
        )
        call_kwargs = ae.handle_manual_override.call_args
        assert call_kwargs is not None
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        assert kwargs.get("source") == "setpoint", (
            f"Expected source='setpoint', got: {kwargs}. "
            "The override must be tagged as a setpoint change, not a mode change."
        )

    def test_heat_cool_setpoint_no_change_not_counted(self):
        """target_temp_high and target_temp_low both unchanged → no override."""
        cmd_time = _NOW_BASE - timedelta(minutes=5)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
        )
        initial = coord._today_record.manual_overrides

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="heat_cool",
            old_target_high=72.0,
            new_target_high=72.0,  # unchanged
            old_target_low=68.0,
            new_target_low=68.0,  # unchanged
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial, (
            "No setpoint change → manual_overrides must not be incremented."
        )

    def test_non_heat_cool_mode_uses_temperature_attribute(self):
        """In 'cool' mode (not heat_cool), the guard uses 'temperature' attribute as before."""
        cmd_time = _NOW_BASE - timedelta(minutes=5)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="cool",
            last_commanded_hvac_time=cmd_time,
            classification=_make_classification(hvac_mode="cool"),
        )
        initial = coord._today_record.manual_overrides

        event = _make_event(
            old_hvac_mode="cool",
            new_hvac_mode="cool",
            old_temp=72.0,
            new_temp=74.0,
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial + 1, (
            "In non-heat_cool mode, 'temperature' attribute change must still be detected. "
            "The Bug D fix must not break single-setpoint thermostat detection."
        )

    def test_heat_cool_setpoint_suppressed_when_temp_command_pending(self):
        """target_temp_high change when _temp_command_pending=True → not counted (CA did it)."""
        cmd_time = _NOW_BASE - timedelta(minutes=5)
        coord = _make_coordinator_stub(
            last_commanded_hvac_mode="heat_cool",
            last_commanded_hvac_time=cmd_time,
            temp_command_pending=True,
        )
        initial = coord._today_record.manual_overrides

        event = _make_event(
            old_hvac_mode="heat_cool",
            new_hvac_mode="heat_cool",
            old_target_high=72.0,
            new_target_high=74.0,
            old_target_low=68.0,
            new_target_low=68.0,
        )

        mock_dt = MagicMock()
        mock_dt.now.return_value = _NOW_BASE
        with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
            asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == initial, (
            "Setpoint change when _temp_command_pending=True must NOT be counted "
            "(CA issued the setpoint command itself)."
        )
