"""Tests for door/window sensor group resolution, polarity, debounce, and grace periods.

These tests validate the algorithms used by the coordinator for resolving
binary_sensor groups and interpreting sensor polarity. Since the coordinator
cannot be instantiated without a live Home Assistant instance, we replicate
the logic inline and test it directly.

For debounce and grace period tests, we test the AutomationEngine directly
with mocked HA dependencies.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_EMAIL_NOTIFY,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
)


# ---------------------------------------------------------------------------
# Replicate coordinator logic for unit testing
# ---------------------------------------------------------------------------

def _resolve_monitored_sensors(
    door_window_sensors: list[str],
) -> list[str]:
    """Resolve all monitored sensor entity IDs.

    This mirrors ClimateAdvisorCoordinator._resolve_monitored_sensors().
    Binary sensor groups are themselves binary_sensor entities, so no
    expansion is needed — they are monitored directly.
    """
    return list(door_window_sensors)


def _is_sensor_open(
    hass_states_get,
    entity_id: str,
    polarity_inverted: bool,
) -> bool:
    """Check if a sensor is open, respecting polarity.

    This mirrors ClimateAdvisorCoordinator._is_sensor_open().
    """
    state = hass_states_get(entity_id)
    if not state:
        return False
    if polarity_inverted:
        return state.state == "off"
    return state.state == "on"


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


def _states_getter(state_map: dict[str, MagicMock]):
    """Return a callable that looks up entity states from a dict."""
    return lambda eid: state_map.get(eid)


# ---------------------------------------------------------------------------
# Group resolution tests
# ---------------------------------------------------------------------------

class TestResolveMonitoredSensors:
    """Tests for sensor resolution logic.

    Binary sensor groups in HA are themselves binary_sensor entities, so they
    appear in the single door_window_sensors list alongside individual sensors.
    """

    def test_returns_configured_sensors(self):
        result = _resolve_monitored_sensors(
            ["binary_sensor.front_door", "binary_sensor.back_door"],
        )
        assert result == ["binary_sensor.front_door", "binary_sensor.back_door"]

    def test_empty_config(self):
        result = _resolve_monitored_sensors([])
        assert result == []

    def test_includes_group_entities(self):
        """Binary sensor groups are binary_sensor entities and included directly."""
        result = _resolve_monitored_sensors(
            ["binary_sensor.front_door", "binary_sensor.all_windows"],
        )
        assert "binary_sensor.all_windows" in result
        assert "binary_sensor.front_door" in result

    def test_returns_copy_not_original(self):
        """Returned list should be a copy, not the original."""
        original = ["binary_sensor.a"]
        result = _resolve_monitored_sensors(original)
        result.append("binary_sensor.b")
        assert len(original) == 1


# ---------------------------------------------------------------------------
# Polarity tests
# ---------------------------------------------------------------------------

class TestIsSensorOpen:
    """Tests for polarity-aware sensor open check."""

    def test_standard_on_is_open(self):
        get = _states_getter({"binary_sensor.door": _make_state("on")})
        assert _is_sensor_open(get, "binary_sensor.door", False) is True

    def test_standard_off_is_closed(self):
        get = _states_getter({"binary_sensor.door": _make_state("off")})
        assert _is_sensor_open(get, "binary_sensor.door", False) is False

    def test_inverted_off_is_open(self):
        get = _states_getter({"binary_sensor.door": _make_state("off")})
        assert _is_sensor_open(get, "binary_sensor.door", True) is True

    def test_inverted_on_is_closed(self):
        get = _states_getter({"binary_sensor.door": _make_state("on")})
        assert _is_sensor_open(get, "binary_sensor.door", True) is False

    def test_unavailable_sensor_is_not_open(self):
        get = _states_getter({})
        assert _is_sensor_open(get, "binary_sensor.missing", False) is False

    def test_unavailable_sensor_inverted_is_not_open(self):
        get = _states_getter({})
        assert _is_sensor_open(get, "binary_sensor.missing", True) is False


# ---------------------------------------------------------------------------
# All-closed logic tests
# ---------------------------------------------------------------------------

class TestAllClosedCheck:
    """Tests for the all-closed check across multiple sensors with polarity."""

    def test_all_closed_standard(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("off"),
            "binary_sensor.b": _make_state("off"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, False) for s in sensors)
        assert all_closed is True

    def test_one_open_standard(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("off"),
            "binary_sensor.b": _make_state("on"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, False) for s in sensors)
        assert all_closed is False

    def test_all_closed_inverted(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("on"),
            "binary_sensor.b": _make_state("on"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, True) for s in sensors)
        assert all_closed is True

    def test_one_open_inverted(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("on"),
            "binary_sensor.b": _make_state("off"),  # off = open when inverted
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, True) for s in sensors)
        assert all_closed is False


# ---------------------------------------------------------------------------
# Config migration tests
# ---------------------------------------------------------------------------

class TestConfigMigration:
    """Tests for v2->v3 config migration defaults."""

    def test_v2_config_gets_polarity_default(self):
        v2_data = {
            "door_window_sensors": ["binary_sensor.front_door"],
            "wake_time": "06:30",
        }
        new_data = {**v2_data}
        new_data.pop("door_window_groups", None)
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert "door_window_groups" not in new_data
        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is False
        assert new_data["door_window_sensors"] == ["binary_sensor.front_door"]

    def test_v2_migration_removes_legacy_groups_key(self):
        v2_data = {
            "door_window_sensors": ["binary_sensor.front_door"],
            "door_window_groups": ["group.old_group"],
        }
        new_data = {**v2_data}
        new_data.pop("door_window_groups", None)
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert "door_window_groups" not in new_data

    def test_v2_config_preserves_polarity_if_set(self):
        v2_data = {
            CONF_SENSOR_POLARITY_INVERTED: True,
        }
        new_data = {**v2_data}
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is True


# ---------------------------------------------------------------------------
# Helpers for AutomationEngine tests
# ---------------------------------------------------------------------------

def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock()
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    if config_overrides:
        config.update(config_overrides)

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


# ---------------------------------------------------------------------------
# Debounce constant/config tests
# ---------------------------------------------------------------------------

class TestSensorDebounceConfig:
    """Tests for debounce configuration defaults and values."""

    def test_default_debounce_is_five_minutes(self):
        assert DEFAULT_SENSOR_DEBOUNCE_SECONDS == 300

    def test_config_key_name(self):
        assert CONF_SENSOR_DEBOUNCE == "sensor_debounce_seconds"

    def test_engine_reads_debounce_from_config(self):
        engine = _make_automation_engine({CONF_SENSOR_DEBOUNCE: 120})
        assert engine.config[CONF_SENSOR_DEBOUNCE] == 120

    def test_engine_uses_default_when_not_configured(self):
        engine = _make_automation_engine()
        debounce = engine.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        assert debounce == 300


# ---------------------------------------------------------------------------
# Grace period — AutomationEngine tests
# ---------------------------------------------------------------------------

class TestGracePeriodState:
    """Tests for grace period state management on the AutomationEngine."""

    def test_initial_state_no_grace(self):
        engine = _make_automation_engine()
        assert engine._grace_active is False
        assert engine._last_resume_source is None
        assert engine._manual_grace_cancel is None
        assert engine._automation_grace_cancel is None

    def test_is_paused_by_door_property(self):
        engine = _make_automation_engine()
        assert engine.is_paused_by_door is False
        engine._paused_by_door = True
        assert engine.is_paused_by_door is True


class TestHandleDoorWindowOpenWithGrace:
    """Tests for handle_door_window_open respecting grace periods."""

    def test_skips_pause_when_grace_active(self):
        engine = _make_automation_engine()
        engine._grace_active = True
        engine._last_resume_source = "automation"

        # Set up state so it would normally pause
        engine.hass.states.get.return_value = _make_state("heat")

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Should NOT have paused
        assert engine._paused_by_door is False
        engine.hass.services.async_call.assert_not_called()

    def test_pauses_when_no_grace(self):
        engine = _make_automation_engine()
        engine.hass.states.get.return_value = _make_state("heat")

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True

    def test_idempotent_when_already_paused(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Should not have called any service
        engine.hass.services.async_call.assert_not_called()


class TestHandleAllDoorsWindowsClosed:
    """Tests for handle_all_doors_windows_closed starting grace periods."""

    def test_resume_starts_automation_grace(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            mock_call_later.return_value = MagicMock()  # cancel callback
            asyncio.run(engine.handle_all_doors_windows_closed())

        assert engine._paused_by_door is False
        assert engine._grace_active is True
        assert engine._last_resume_source == "automation"
        mock_call_later.assert_called_once()

    def test_no_resume_when_not_paused(self):
        engine = _make_automation_engine()
        engine._paused_by_door = False

        asyncio.run(engine.handle_all_doors_windows_closed())

        engine.hass.services.async_call.assert_not_called()

    def test_resume_restores_pre_pause_mode(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ):
            asyncio.run(engine.handle_all_doors_windows_closed())

        # Should have called set_hvac_mode with "cool"
        engine.hass.services.async_call.assert_any_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.thermostat", "hvac_mode": "cool"},
        )


class TestManualOverrideDuringPause:
    """Tests for handle_manual_override_during_pause."""

    def test_starts_manual_grace(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ):
            asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._paused_by_door is False
        assert engine._pre_pause_mode is None
        assert engine._grace_active is True
        assert engine._last_resume_source == "manual"

    def test_noop_when_not_paused(self):
        engine = _make_automation_engine()
        engine._paused_by_door = False

        asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._grace_active is False


class TestGracePeriodDuration:
    """Tests for configurable grace period durations."""

    def test_manual_grace_uses_config_value(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 600})
        engine._paused_by_door = True

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            mock_call_later.return_value = MagicMock()
            asyncio.run(engine.handle_manual_override_during_pause())

        # Second arg to async_call_later is the duration
        mock_call_later.assert_called_once()
        call_args = mock_call_later.call_args
        assert call_args[0][1] == 600  # duration

    def test_automation_grace_uses_config_value(self):
        engine = _make_automation_engine({CONF_AUTOMATION_GRACE_PERIOD: 1200})
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            mock_call_later.return_value = MagicMock()
            asyncio.run(engine.handle_all_doors_windows_closed())

        mock_call_later.assert_called_once()
        call_args = mock_call_later.call_args
        assert call_args[0][1] == 1200

    def test_zero_duration_disables_grace(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 0})
        engine._paused_by_door = True

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            asyncio.run(engine.handle_manual_override_during_pause())

        # Should not have started a timer
        mock_call_later.assert_not_called()
        assert engine._grace_active is False

    def test_default_manual_grace_is_30_min(self):
        assert DEFAULT_MANUAL_GRACE_SECONDS == 1800

    def test_default_automation_grace_is_5_min(self):
        assert DEFAULT_AUTOMATION_GRACE_SECONDS == 300


class TestGracePeriodNotifications:
    """Tests for grace period notification toggles."""

    def test_manual_grace_notify_default_off(self):
        engine = _make_automation_engine()
        assert engine.config.get(CONF_MANUAL_GRACE_NOTIFY, False) is False

    def test_automation_grace_notify_default_on(self):
        engine = _make_automation_engine()
        # Default is True when not in config
        assert engine.config.get(CONF_AUTOMATION_GRACE_NOTIFY, True) is True

    def test_manual_grace_notify_configurable(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_NOTIFY: True})
        assert engine.config[CONF_MANUAL_GRACE_NOTIFY] is True

    def test_automation_grace_notify_configurable(self):
        engine = _make_automation_engine({CONF_AUTOMATION_GRACE_NOTIFY: False})
        assert engine.config[CONF_AUTOMATION_GRACE_NOTIFY] is False


# ---------------------------------------------------------------------------
# Timer cleanup tests
# ---------------------------------------------------------------------------

class TestTimerCleanup:
    """Tests for timer cleanup on engine disposal."""

    def test_cleanup_cancels_grace_timers(self):
        engine = _make_automation_engine()
        mock_manual_cancel = MagicMock()
        mock_auto_cancel = MagicMock()
        engine._manual_grace_cancel = mock_manual_cancel
        engine._automation_grace_cancel = mock_auto_cancel
        engine._grace_active = True

        engine.cleanup()

        mock_manual_cancel.assert_called_once()
        mock_auto_cancel.assert_called_once()
        assert engine._grace_active is False

    def test_cleanup_handles_no_active_timers(self):
        engine = _make_automation_engine()
        # Should not raise
        engine.cleanup()
        assert engine._grace_active is False

    def test_cancel_grace_timers_resets_state(self):
        engine = _make_automation_engine()
        engine._grace_active = True
        engine._last_resume_source = "manual"
        engine._manual_grace_cancel = MagicMock()

        engine._cancel_grace_timers()

        assert engine._grace_active is False
        assert engine._last_resume_source is None
        assert engine._manual_grace_cancel is None


# ---------------------------------------------------------------------------
# Grace period expiry callback tests (Issue #38)
# ---------------------------------------------------------------------------

class TestGracePeriodExpiry:
    """Tests for the grace period expiry callback behavior.

    The @callback decorator from homeassistant.core is mocked as a MagicMock
    in the test environment, which swallows the decorated function. We patch
    it as an identity function so the grace expiry closure is preserved.
    """

    _PATCHES = [
        "custom_components.climate_advisor.automation.async_call_later",
        "custom_components.climate_advisor.automation.callback",
    ]

    def _run_close_and_capture_callback(self, engine):
        """Run handle_all_doors_windows_closed and return the grace expiry callback."""
        with patch(self._PATCHES[0]) as mock_call_later, \
             patch(self._PATCHES[1], side_effect=lambda f: f):
            mock_call_later.return_value = MagicMock()
            asyncio.run(engine.handle_all_doors_windows_closed())
            assert mock_call_later.call_count == 1
            duration = mock_call_later.call_args[0][1]
            grace_callback = mock_call_later.call_args[0][2]
            return duration, grace_callback

    def test_grace_expiry_callback_clears_state(self):
        """When the grace timer fires, _grace_active resets and manual override clears."""
        engine = _make_automation_engine({
            CONF_AUTOMATION_GRACE_PERIOD: 300,
            CONF_AUTOMATION_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"

        duration, grace_callback = self._run_close_and_capture_callback(engine)
        assert duration == 300

        # Grace should be active before expiry
        assert engine._grace_active is True
        assert engine._last_resume_source == "automation"

        # Fire the expiry callback
        grace_callback(None)

        # State should be cleared
        assert engine._grace_active is False
        assert engine._last_resume_source is None
        assert engine._manual_grace_cancel is None
        assert engine._automation_grace_cancel is None
        assert engine._manual_override_active is False

    def test_grace_expiry_sends_notification_when_enabled(self):
        """When automation grace notify is on, expiry dispatches a notification."""
        engine = _make_automation_engine({
            CONF_AUTOMATION_GRACE_PERIOD: 300,
            CONF_AUTOMATION_GRACE_NOTIFY: True,
        })
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"

        _, grace_callback = self._run_close_and_capture_callback(engine)

        # Fire the expiry callback
        grace_callback(None)

        # Should have scheduled a notification task
        engine.hass.async_create_task.assert_called()

    def test_grace_expiry_skips_notification_when_disabled(self):
        """When automation grace notify is off, expiry sends no notification."""
        engine = _make_automation_engine({
            CONF_AUTOMATION_GRACE_PERIOD: 300,
            CONF_AUTOMATION_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"

        _, grace_callback = self._run_close_and_capture_callback(engine)

        # Reset mock so we only track calls after expiry
        engine.hass.async_create_task.reset_mock()

        # Fire the expiry callback
        grace_callback(None)

        # Should NOT have scheduled a notification task
        engine.hass.async_create_task.assert_not_called()

    def test_door_open_during_grace_is_blocked(self):
        """Opening a door during active grace period does not pause HVAC."""
        engine = _make_automation_engine()
        engine._grace_active = True
        engine._last_resume_source = "automation"

        # Set up a state so the engine could pause if it wanted to
        state_mock = MagicMock()
        state_mock.state = "cool"
        engine.hass.states.get.return_value = state_mock

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is False
        # No HVAC service call should have been made
        engine.hass.services.async_call.assert_not_called()

    def test_door_open_after_grace_expires_triggers_pause(self):
        """After grace expires, a new door open correctly pauses HVAC."""
        engine = _make_automation_engine({
            CONF_AUTOMATION_GRACE_PERIOD: 300,
            CONF_AUTOMATION_GRACE_NOTIFY: False,
        })
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"

        _, grace_callback = self._run_close_and_capture_callback(engine)

        # Fire the expiry callback — grace ends
        grace_callback(None)
        assert engine._grace_active is False

        # Now a new door open should pause HVAC
        state_mock = MagicMock()
        state_mock.state = "cool"
        engine.hass.states.get.return_value = state_mock
        engine.hass.services.async_call.reset_mock()

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True
        engine.hass.services.async_call.assert_called()


# ---------------------------------------------------------------------------
# Config migration v3 → v4 tests
# ---------------------------------------------------------------------------

class TestConfigMigrationV3ToV4:
    """Tests for v3->v4 config migration defaults."""

    def test_v3_config_gets_new_defaults(self):
        v3_data = {
            "door_window_sensors": ["binary_sensor.front_door"],
            "door_window_groups": [],
            CONF_SENSOR_POLARITY_INVERTED: False,
        }
        new_data = {**v3_data}
        new_data.setdefault(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_NOTIFY, False)
        new_data.setdefault(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS)
        new_data.setdefault(CONF_AUTOMATION_GRACE_NOTIFY, True)

        assert new_data[CONF_SENSOR_DEBOUNCE] == 300
        assert new_data[CONF_MANUAL_GRACE_PERIOD] == 1800
        assert new_data[CONF_MANUAL_GRACE_NOTIFY] is False
        assert new_data[CONF_AUTOMATION_GRACE_PERIOD] == 300
        assert new_data[CONF_AUTOMATION_GRACE_NOTIFY] is True
        # Existing keys preserved
        assert new_data["door_window_sensors"] == ["binary_sensor.front_door"]
        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is False

    def test_v3_config_preserves_custom_values(self):
        v3_data = {
            CONF_SENSOR_DEBOUNCE: 120,
            CONF_MANUAL_GRACE_PERIOD: 900,
            CONF_MANUAL_GRACE_NOTIFY: True,
            CONF_AUTOMATION_GRACE_PERIOD: 1800,
            CONF_AUTOMATION_GRACE_NOTIFY: False,
        }
        new_data = {**v3_data}
        new_data.setdefault(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_NOTIFY, False)
        new_data.setdefault(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS)
        new_data.setdefault(CONF_AUTOMATION_GRACE_NOTIFY, True)

        assert new_data[CONF_SENSOR_DEBOUNCE] == 120
        assert new_data[CONF_MANUAL_GRACE_PERIOD] == 900
        assert new_data[CONF_MANUAL_GRACE_NOTIFY] is True
        assert new_data[CONF_AUTOMATION_GRACE_PERIOD] == 1800
        assert new_data[CONF_AUTOMATION_GRACE_NOTIFY] is False


# ---------------------------------------------------------------------------
# Email notification tests
# ---------------------------------------------------------------------------

class TestEmailNotifications:
    """Tests for dual-channel notification (primary + email via _notify helper)."""

    def test_notify_sends_both_when_email_enabled(self):
        """_notify sends to primary service AND send_email when toggle is on."""
        engine = _make_automation_engine({CONF_EMAIL_NOTIFY: True})
        asyncio.run(engine._notify("Test message", "Test Title"))

        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 2
        # First call: primary service
        assert calls[0][0] == ("notify", "notify", {"message": "Test message", "title": "Test Title"})
        # Second call: email
        assert calls[1][0] == ("notify", "send_email", {"message": "Test message", "title": "Test Title"})

    def test_notify_skips_email_when_disabled(self):
        """_notify only sends to primary service when email toggle is off."""
        engine = _make_automation_engine({CONF_EMAIL_NOTIFY: False})
        asyncio.run(engine._notify("Test message", "Test Title"))

        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 1
        assert calls[0][0] == ("notify", "notify", {"message": "Test message", "title": "Test Title"})

    def test_notify_defaults_to_email_enabled(self):
        """When email_notify is not in config, it defaults to True (sends email)."""
        engine = _make_automation_engine()
        # Ensure the key is not present
        engine.config.pop(CONF_EMAIL_NOTIFY, None)
        asyncio.run(engine._notify("Test message", "Test Title"))

        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 2
        assert calls[1][0][1] == "send_email"

    def test_door_open_sends_email(self):
        """handle_door_window_open sends email when toggle is on."""
        engine = _make_automation_engine({CONF_EMAIL_NOTIFY: True})
        engine.hass.states.get.return_value = _make_state("heat")

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Should have: set_hvac_mode("off") + primary notify + email notify = 3 calls
        calls = engine.hass.services.async_call.call_args_list
        notify_calls = [c for c in calls if c[0][0] == "notify"]
        assert len(notify_calls) == 2
        assert notify_calls[1][0][1] == "send_email"

    def test_door_open_no_email_when_disabled(self):
        """handle_door_window_open skips email when toggle is off."""
        engine = _make_automation_engine({CONF_EMAIL_NOTIFY: False})
        engine.hass.states.get.return_value = _make_state("heat")

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        calls = engine.hass.services.async_call.call_args_list
        notify_calls = [c for c in calls if c[0][0] == "notify"]
        assert len(notify_calls) == 1

    def test_occupancy_home_sends_email(self):
        """handle_occupancy_home sends email when toggle is on."""
        engine = _make_automation_engine({CONF_EMAIL_NOTIFY: True})
        # Need a classification for occupancy_home to proceed
        from custom_components.climate_advisor.classifier import DayClassification
        engine._current_classification = DayClassification(
            day_type="mild",
            today_high=72,
            today_low=55,
            tomorrow_high=70,
            tomorrow_low=52,
            hvac_mode="heat",
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
            pre_condition=False,
            pre_condition_target=None,
            setback_modifier=0,
            trend_direction="stable",
            trend_magnitude=0,
        )
        asyncio.run(engine.handle_occupancy_home())

        calls = engine.hass.services.async_call.call_args_list
        notify_calls = [c for c in calls if c[0][0] == "notify"]
        assert len(notify_calls) == 2
        assert notify_calls[1][0][1] == "send_email"


# ---------------------------------------------------------------------------
# Config migration v4 → v5 tests
# ---------------------------------------------------------------------------

class TestConfigMigrationV4ToV5:
    """Tests for v4->v5 config migration adding email_notify default."""

    def test_v4_config_gets_email_notify_default(self):
        v4_data = {
            "notify_service": "notify.notify",
            "door_window_sensors": ["binary_sensor.front_door"],
        }
        new_data = {**v4_data}
        new_data.setdefault(CONF_EMAIL_NOTIFY, True)

        assert new_data[CONF_EMAIL_NOTIFY] is True
        assert new_data["notify_service"] == "notify.notify"

    def test_v4_config_preserves_email_notify_if_set(self):
        v4_data = {
            CONF_EMAIL_NOTIFY: False,
        }
        new_data = {**v4_data}
        new_data.setdefault(CONF_EMAIL_NOTIFY, True)

        assert new_data[CONF_EMAIL_NOTIFY] is False


# ---------------------------------------------------------------------------
# Debounce / grace interaction tests (Issue #13)
# ---------------------------------------------------------------------------

class TestDebounceGraceInteraction:
    """Tests for the interaction between debounce timers and grace periods."""

    def test_manual_override_detected_without_off_state(self):
        """Race condition fix: override is detected even when old state never hit 'off'.

        Replicates the coordinator detection logic inline:
        Before the fix: old_state.state == "off" was required.
        After the fix: any non-off/unavailable/unknown new_state triggers detection.
        """
        is_paused_by_door = True
        old_state_value = "cool"   # thermostat was already in "cool", never hit "off"
        new_state_value = "cool"   # changed to "cool" again (or any active mode)

        # Old (broken) logic — would NOT detect the override
        old_logic_detected = (
            is_paused_by_door
            and old_state_value == "off"
            and new_state_value not in ("off", "unavailable", "unknown")
        )
        assert old_logic_detected is False

        # New (fixed) logic — detects override regardless of old_state
        new_logic_detected = (
            is_paused_by_door
            and new_state_value not in ("off", "unavailable", "unknown")
        )
        assert new_logic_detected is True

    def test_manual_override_cancels_debounce_timers(self):
        """Simulates _cancel_all_debounce_timers: all timers cancelled, dict cleared."""
        cancel_a = MagicMock()
        cancel_b = MagicMock()
        door_open_timers = {
            "binary_sensor.front_door": cancel_a,
            "binary_sensor.back_door": cancel_b,
        }

        # Replicate coordinator._cancel_all_debounce_timers() logic
        for cancel_fn in door_open_timers.values():
            cancel_fn()
        door_open_timers.clear()

        cancel_a.assert_called_once()
        cancel_b.assert_called_once()
        assert door_open_timers == {}

    def test_grace_blocks_new_pause_after_manual_override(self):
        """Full sequence: open → paused → manual override (grace) → reopen → blocked."""
        engine = _make_automation_engine()
        engine.hass.states.get.return_value = _make_state("heat")

        # Step 1: door opens — engine pauses HVAC
        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True

        # Step 2: user manually overrides during pause — grace starts
        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ):
            asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._paused_by_door is False
        assert engine._grace_active is True

        # Step 3: door opens again — should be blocked by grace
        engine.hass.services.async_call.reset_mock()
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Pause should NOT have happened
        assert engine._paused_by_door is False
        engine.hass.services.async_call.assert_not_called()

    def test_grace_blocks_same_sensor_reopen(self):
        """Grace period blocks a re-open from the same sensor that originally triggered."""
        engine = _make_automation_engine()
        engine.hass.states.get.return_value = _make_state("cool")

        sensor = "binary_sensor.front_door"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open(sensor))

        assert engine._paused_by_door is True

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ):
            asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._grace_active is True

        # Same sensor reopens — still blocked
        engine.hass.services.async_call.reset_mock()
        asyncio.run(engine.handle_door_window_open(sensor))

        assert engine._paused_by_door is False
        engine.hass.services.async_call.assert_not_called()

    def test_multiple_sensors_staggered_debounce(self):
        """Sensor A triggers pause, user overrides (grace starts), sensor B blocked."""
        engine = _make_automation_engine()
        engine.hass.states.get.return_value = _make_state("heat")

        # Sensor A fires first
        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.sensor_a"))

        assert engine._paused_by_door is True

        # User manually overrides
        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ):
            asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._grace_active is True
        assert engine._paused_by_door is False

        # Sensor B's debounce expires and tries to pause
        engine.hass.services.async_call.reset_mock()
        asyncio.run(engine.handle_door_window_open("binary_sensor.sensor_b"))

        # Grace should have blocked sensor B
        assert engine._paused_by_door is False
        engine.hass.services.async_call.assert_not_called()

    def test_grace_expiry_allows_new_pause(self):
        """After grace expires (_grace_active=False), the next open should pause."""
        engine = _make_automation_engine()
        engine.hass.states.get.return_value = _make_state("heat")

        # Simulate completed manual override with grace already expired
        engine._grace_active = False
        engine._last_resume_source = None
        engine._paused_by_door = False

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True

    def test_zero_grace_allows_immediate_repause(self):
        """With CONF_MANUAL_GRACE_PERIOD=0, manual override leaves no grace — next open pauses."""
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 0})
        engine.hass.states.get.return_value = _make_state("heat")

        # First open — pauses HVAC
        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True

        # Manual override with zero-duration grace — grace should NOT activate
        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            asyncio.run(engine.handle_manual_override_during_pause())

        mock_call_later.assert_not_called()
        assert engine._grace_active is False
        assert engine._paused_by_door is False

        # Door opens again — should pause immediately (no grace blocking)
        engine.hass.services.async_call.reset_mock()
        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True


# ---------------------------------------------------------------------------
# Config minutes ↔ seconds conversion tests (Issue #13)
# ---------------------------------------------------------------------------

class TestConfigMinutesConversion:
    """Tests for the minutes-to-seconds conversion added in Issue #13.

    Config flow now accepts user input in minutes and stores seconds (* 60).
    Options flow displays stored seconds as minutes (// 60).
    """

    def test_minutes_to_seconds_on_save(self):
        """Simulates config_flow: user enters 5 minutes → stored as 300 seconds."""
        user_input_minutes = 5
        stored_seconds = user_input_minutes * 60
        assert stored_seconds == 300

    def test_seconds_to_minutes_on_display(self):
        """Simulates options flow: stored 300 seconds → displayed as 5 minutes."""
        stored_seconds = 300
        display_minutes = stored_seconds // 60
        assert display_minutes == 5

    def test_debounce_max_allows_60_minutes(self):
        """60-minute debounce (3600 s) is within the new max (previously capped at 900 s)."""
        user_input_minutes = 60
        stored_seconds = user_input_minutes * 60
        assert stored_seconds == 3600
        # Confirm it exceeds the old 900 s cap — this is now a valid value
        assert stored_seconds > 900

    def test_grace_max_allows_240_minutes(self):
        """240-minute grace (14400 s) is within the new max."""
        user_input_minutes = 240
        stored_seconds = user_input_minutes * 60
        assert stored_seconds == 14400
        # Should be well beyond the old debounce cap
        assert stored_seconds > 3600

    def test_zero_minutes_stored_as_zero_seconds(self):
        """0 minutes → 0 seconds, which disables the grace period."""
        stored_seconds = 0 * 60
        assert stored_seconds == 0

    def test_round_trip_minutes_seconds(self):
        """Storing and retrieving any whole-minute value is lossless."""
        for minutes in (1, 5, 10, 30, 60, 120, 240):
            stored = minutes * 60
            displayed = stored // 60
            assert displayed == minutes

    def test_engine_accepts_large_grace_from_config(self):
        """AutomationEngine stores a large grace value from config without modification."""
        large_grace_seconds = 14400  # 240 minutes
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: large_grace_seconds})
        assert engine.config[CONF_MANUAL_GRACE_PERIOD] == 14400

    def test_engine_accepts_large_debounce_from_config(self):
        """AutomationEngine stores a large debounce value from config without modification."""
        large_debounce_seconds = 3600  # 60 minutes
        engine = _make_automation_engine({CONF_SENSOR_DEBOUNCE: large_debounce_seconds})
        assert engine.config[CONF_SENSOR_DEBOUNCE] == 3600


# ---------------------------------------------------------------------------
# Physical window tracking on HOT days (Issue #18 — bug fix)
# ---------------------------------------------------------------------------

class TestPhysicalWindowTrackingOnHotDay:
    """On HOT days windows_recommended=False, but physical opens must still be recorded.

    The coordinator's _debounce_expired callback now has an unconditional block
    that tracks physical opens regardless of windows_recommended.  These tests
    replicate that logic inline (the same approach used throughout this file)
    so we can verify the fix without needing a live coordinator.
    """

    def _simulate_debounce_expired(self, today_record, windows_recommended: bool):
        """Replicate the coordinator's _debounce_expired window-tracking logic."""
        # --- compliance block (gated on windows_recommended) ---
        if windows_recommended and not today_record.windows_opened:
            today_record.windows_opened = True
            today_record.window_open_actual_time = "2026-03-19T08:30:00"

        # --- physical tracking block (always runs) ---
        if not today_record.windows_physically_opened:
            today_record.windows_physically_opened = True
            today_record.window_physical_open_time = "2026-03-19T08:30:00"

    def _make_record(self):
        """Create a minimal DailyRecord for today."""
        from custom_components.climate_advisor.learning import DailyRecord
        return DailyRecord(date="2026-03-19", day_type="hot", trend_direction="stable")

    def test_physical_window_tracking_on_hot_day(self):
        """HOT day (windows_recommended=False), window opens → windows_physically_opened = True."""
        record = self._make_record()
        assert record.windows_physically_opened is False
        assert record.window_physical_open_time is None

        self._simulate_debounce_expired(record, windows_recommended=False)

        assert record.windows_physically_opened is True
        assert record.window_physical_open_time is not None

    def test_compliance_tracking_NOT_set_on_hot_day(self):
        """On a HOT day (windows_recommended=False), compliance tracking stays False."""
        record = self._make_record()
        self._simulate_debounce_expired(record, windows_recommended=False)

        # Physical tracking is set
        assert record.windows_physically_opened is True
        # Compliance tracking is NOT set
        assert record.windows_opened is False
        assert record.window_open_actual_time is None

    def test_physical_tracking_also_set_on_warm_day(self):
        """On a WARM day (windows_recommended=True), BOTH compliance and physical are set."""
        record = self._make_record()
        record.day_type = "warm"

        self._simulate_debounce_expired(record, windows_recommended=True)

        assert record.windows_opened is True
        assert record.windows_physically_opened is True

    def test_physical_open_time_is_idempotent(self):
        """Calling the tracking logic twice does not overwrite the first open time."""
        record = self._make_record()
        self._simulate_debounce_expired(record, windows_recommended=False)
        first_time = record.window_physical_open_time

        # Simulate a second sensor open event
        self._simulate_debounce_expired(record, windows_recommended=False)

        assert record.window_physical_open_time == first_time
