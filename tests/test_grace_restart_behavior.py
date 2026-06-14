"""Tests for grace-restart behavior (Issue #282).

Fix A: async_restore_state must NOT reschedule a grace timer on restart.
Fix D: A new mode change during active grace must restart the override
       (clear old override + register new one).

These are coordinator-level tests that bind the real _async_thermostat_changed
and async_restore_state methods against a minimal stub coordinator.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

# Patch dt_util.now to a stable datetime
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 12, 14, 0, 0)

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.learning import DailyRecord  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THERMOSTAT_ID = "climate.thermostat"

_PATCH_CALL_LATER = "custom_components.climate_advisor.coordinator.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.coordinator.callback"


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class (avoids stale __globals__)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__ validation."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 78,
        "today_low": 58,
        "tomorrow_high": 79,
        "tomorrow_low": 59,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _make_today_record(**overrides) -> DailyRecord:
    kwargs = dict(date="2026-06-12", day_type="warm", trend_direction="stable")
    kwargs.update(overrides)
    return DailyRecord(**kwargs)


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.state = state_value
    s.attributes = attributes or {}
    return s


def _make_event(data: dict) -> MagicMock:
    event = MagicMock()
    event.data = data
    return event


def _make_thermostat_coord_stub(**ae_overrides):
    """Build a minimal coordinator stub for _async_thermostat_changed tests."""
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

    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = False
    ae._manual_override_active = False
    ae._manual_override_mode = None
    ae._fan_command_pending = False
    ae._fan_override_active = False
    ae._temp_command_pending = False
    ae._temp_command_time = None
    ae._last_commanded_hvac_mode = None
    ae._last_commanded_hvac_time = None
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.clear_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    ae._natural_vent_active = False
    ae._fan_override_active = False
    for k, v in ae_overrides.items():
        setattr(ae, k, v)
    coord.automation_engine = ae

    coord._current_classification = _make_classification()
    coord._today_record = _make_today_record()
    coord._async_save_state = AsyncMock()

    coord._is_recent_hvac_command = MagicMock(return_value=False)
    coord._is_recent_temp_command = MagicMock(return_value=False)
    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    coord._end_hvac_active_phase = AsyncMock()
    coord._abandon_observation = AsyncMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)

    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)

    return coord


def _make_restore_coord_stub(ae_mock=None):
    """Build a minimal coordinator stub for async_restore_state tests."""
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

    # Automation engine — real mock with explicit grace fields
    ae = ae_mock if ae_mock is not None else MagicMock()
    ae._natural_vent_active = False
    ae._fan_override_active = False
    coord.automation_engine = ae

    # State/learning stubs
    coord._rejection_log = {}
    coord._current_classification = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord._today_record = None
    coord._briefing_sent_today = False
    coord._last_briefing = ""
    coord._last_briefing_short = ""
    coord._briefing_day_type = None
    coord._automation_enabled = True
    coord._occupancy_mode = "home"
    coord._occupancy_away_since = None
    coord._pred_archive = {}
    coord.claude_client = None
    coord._event_log = []
    coord._passive_k_backfilled = False
    coord._vent_k_backfilled = False
    coord._passive_k_backfill_v2 = False
    coord._vent_k_backfill_v2 = False
    coord._solar_phase_backfill = False
    coord._last_solar_phase_fit_date = None  # Issue #310

    coord._async_save_state = AsyncMock()
    coord._set_occupancy_mode = MagicMock()
    coord._emit_event = MagicMock()

    coord.async_restore_state = types.MethodType(ClimateAdvisorCoordinator.async_restore_state, coord)

    return coord


# ---------------------------------------------------------------------------
# Fix A: Grace timer must NOT be rescheduled on restart
# ---------------------------------------------------------------------------


_PATCH_DT_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"
_STABLE_NOW = datetime(2026, 6, 12, 14, 0, 0)


def _run_restore_with_state(state_data: dict, ae: MagicMock) -> MagicMock:
    """Run async_restore_state on a stub coord with given persisted state and AE.

    Returns the coord so callers can inspect it.

    dt_util.now must be patched on the coordinator module object — the HA stubs
    install dt as a MagicMock on homeassistant.util, so the sys.modules["homeassistant.util.dt"]
    assignment used elsewhere does NOT affect dt_util inside coordinator.py after import.
    """
    coord = _make_restore_coord_stub(ae_mock=ae)

    learning_mock = MagicMock()
    learning_mock._state = MagicMock()
    learning_mock._state.rejection_log = {}
    coord.learning = learning_mock
    coord._state_persistence = MagicMock()

    with (
        patch(_PATCH_DT_NOW, return_value=_STABLE_NOW),
        patch.object(coord.hass, "async_add_executor_job", new_callable=AsyncMock) as mock_exec,
    ):
        mock_exec.side_effect = [
            None,  # learning.load_state()
            state_data,  # _state_persistence.load()
        ]
        asyncio.run(coord.async_restore_state())

    return coord


class TestGraceRestartNoReschedule:
    """async_restore_state must not touch grace state at all after restore.

    With the clean-slate design (Fix A), the coordinator removes the grace-timer
    reschedule block entirely. AE.restore_state() is responsible for clearing
    grace on restart. The coordinator must not re-add grace logic on top.
    """

    def test_no_reschedule_when_grace_active_future_end_time(self):
        """grace_active=True with future end_time: coordinator must not reschedule timer.

        FAILS before Fix A: coordinator calls ae._reschedule_grace_timer(remaining).
        PASSES after Fix A: reschedule block removed.
        """
        # Build an AE mock whose restore_state() sets _grace_active=True (old path).
        # We use a real future timestamp so old code would compute remaining > 0.
        future_ts = datetime(2099, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat()
        ae = MagicMock()
        ae._natural_vent_active = False
        ae._fan_override_active = False
        ae._reschedule_grace_timer = MagicMock()
        ae.clear_manual_override = MagicMock()

        # restore_state sets ae._grace_active=True and ae._grace_end_time=future_ts
        def _restore_with_grace(s):
            ae._grace_active = True
            ae._grace_end_time = future_ts

        ae.restore_state = MagicMock(side_effect=_restore_with_grace)

        state_data = {
            "date": "2026-06-12",
            "automation_state": {
                "paused_by_door": False,
                "pre_pause_mode": None,
                "grace_active": True,
                "grace_end_time": future_ts,
                "grace_duration_seconds": 300,
            },
            "automation_enabled": True,
            "occupancy_mode": "home",
        }

        _run_restore_with_state(state_data, ae)

        # After Fix A: _reschedule_grace_timer must NOT have been called
        ae._reschedule_grace_timer.assert_not_called()

    def test_no_clear_override_when_grace_expired_during_restart(self):
        """grace_active=True with past end_time: coordinator must not call clear_manual_override.

        FAILS before Fix A: coordinator calls ae.clear_manual_override(reason="grace_expired_on_restart").
        PASSES after Fix A: block removed; AE.restore_state() owns clearing.
        """
        past_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat()
        ae = MagicMock()
        ae._natural_vent_active = False
        ae._fan_override_active = False
        ae._reschedule_grace_timer = MagicMock()
        ae.clear_manual_override = MagicMock()

        def _restore_with_expired_grace(s):
            ae._grace_active = True
            ae._grace_end_time = past_ts

        ae.restore_state = MagicMock(side_effect=_restore_with_expired_grace)

        state_data = {
            "date": "2026-06-12",
            "automation_state": {
                "paused_by_door": False,
                "pre_pause_mode": None,
                "grace_active": True,
                "grace_end_time": past_ts,
                "grace_duration_seconds": 300,
            },
            "automation_enabled": True,
            "occupancy_mode": "home",
        }

        _run_restore_with_state(state_data, ae)

        # After Fix A: coordinator must NOT call clear_manual_override
        ae.clear_manual_override.assert_not_called()
        ae._reschedule_grace_timer.assert_not_called()


# ---------------------------------------------------------------------------
# Fix D: New mode change during active grace → restart confirmation
# ---------------------------------------------------------------------------


class TestNewOverrideDuringGrace:
    """A mode change to a DIFFERENT mode during active grace restarts confirmation."""

    def test_different_mode_during_grace_calls_clear_and_handle(self):
        """Mode changes heat→cool while _manual_override_active=True, _manual_override_mode=heat.

        Expected: clear_manual_override() + handle_manual_override() both called.
        FAILS before Fix D: the normal elif guard has `not _manual_override_active`,
        so the change is silently dropped.
        PASSES after Fix D: new branch fires before the elif.
        """
        coord = _make_thermostat_coord_stub(
            _manual_override_active=True,
            _manual_override_mode="heat",
        )

        old_state = _make_state("heat", {"hvac_action": ""})
        new_state = _make_state("cool", {"hvac_action": ""})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.clear_manual_override.assert_called_once()
        coord.automation_engine.handle_manual_override.assert_called_once()

    def test_same_mode_during_grace_not_refired(self):
        """Mode change to SAME mode as the current override → nothing fired.

        If user is already overriding cool and thermostat reports cool again,
        we don't re-clear and re-register.
        """
        coord = _make_thermostat_coord_stub(
            _manual_override_active=True,
            _manual_override_mode="cool",
        )

        old_state = _make_state("heat", {"hvac_action": ""})
        new_state = _make_state("cool", {"hvac_action": ""})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.clear_manual_override.assert_not_called()
        # handle_manual_override should NOT be called for same-mode confirmation
        coord.automation_engine.handle_manual_override.assert_not_called()

    def test_mode_change_without_grace_uses_normal_path(self):
        """Mode change when NOT in active grace goes through the normal elif path.

        The new branch must not interfere with the existing detection logic.
        """
        coord = _make_thermostat_coord_stub(
            _manual_override_active=False,
            _manual_override_mode=None,
        )
        # Classification wants "cool"; user switches to "heat" — normal override
        coord._current_classification = _make_classification(hvac_mode="cool")

        old_state = _make_state("cool", {"hvac_action": ""})
        new_state = _make_state("heat", {"hvac_action": ""})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        # Normal path: handle_manual_override called (clear NOT called before it)
        coord.automation_engine.handle_manual_override.assert_called_once()
        coord.automation_engine.clear_manual_override.assert_not_called()

    def test_different_mode_during_grace_suppressed_by_automation_command(self):
        """Mode change during grace is NOT treated as new override if CA issued the command."""
        coord = _make_thermostat_coord_stub(
            _manual_override_active=True,
            _manual_override_mode="heat",
            _hvac_command_pending=True,  # CA command in flight
        )

        old_state = _make_state("heat", {"hvac_action": ""})
        new_state = _make_state("cool", {"hvac_action": ""})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord.automation_engine.handle_manual_override.assert_not_called()
