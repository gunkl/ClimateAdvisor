"""Tests for Issue #59 — Welcome home notification debounce and temperature proximity check."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_WELCOME_HOME_DEBOUNCE,
    DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_SENSOR,
)

# ── Helpers ──────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_hass(climate_temp: float | None = None, sensor_state: str | None = None) -> MagicMock:
    """Create a mock HA instance with optional climate/sensor state."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    def _states_get(entity_id: str):
        if entity_id == "climate.thermostat":
            if climate_temp is not None:
                state = MagicMock()
                state.state = "heat"
                state.attributes = {"current_temperature": climate_temp}
                return state
            return None
        if entity_id == "sensor.indoor_temp" and sensor_state is not None:
            state = MagicMock()
            state.state = sensor_state
            state.attributes = {}
            return state
        return None

    hass.states.get = _states_get
    return hass


def _make_engine(config_overrides: dict | None = None, hass: MagicMock | None = None) -> AutomationEngine:
    """Create an AutomationEngine with standard test config."""
    if hass is None:
        hass = _make_hass()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "indoor_temp_source": TEMP_SOURCE_CLIMATE_FALLBACK,
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_classification(hvac_mode: str = "heat") -> DayClassification:
    """Create a minimal DayClassification for testing."""
    obj = object.__new__(DayClassification)
    obj.day_type = "cold" if hvac_mode == "heat" else "warm"
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 0
    obj.today_high = 75.0
    obj.today_low = 55.0
    obj.tomorrow_high = 75.0
    obj.tomorrow_low = 55.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    obj.window_opportunity_morning = False
    obj.window_opportunity_evening = False
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


def _run(coro):
    """Run a coroutine in the event loop."""
    return asyncio.run(coro)


# ── _get_indoor_temp_f helper ─────────────────────────────────────


class TestWelcomeHomeGetIndoorTempHelper:
    """Tests for _get_indoor_temp_f() on AutomationEngine."""

    def test_reads_climate_fallback_temperature(self):
        hass = _make_hass(climate_temp=72.0)
        engine = _make_engine(hass=hass)
        assert engine._get_indoor_temp_f() == 72.0

    def test_returns_none_when_climate_entity_missing(self):
        hass = _make_hass(climate_temp=None)
        engine = _make_engine(hass=hass)
        assert engine._get_indoor_temp_f() is None

    def test_reads_sensor_entity(self):
        hass = _make_hass(sensor_state="68")
        engine = _make_engine(
            hass=hass,
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
        )
        assert engine._get_indoor_temp_f() == 68.0

    def test_returns_none_for_non_numeric_sensor_state(self):
        hass = _make_hass(sensor_state="unavailable")
        engine = _make_engine(
            hass=hass,
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                "indoor_temp_entity": "sensor.indoor_temp",
            },
        )
        assert engine._get_indoor_temp_f() is None

    def test_returns_none_when_sensor_entity_id_missing(self):
        hass = _make_hass(sensor_state="68")
        engine = _make_engine(
            hass=hass,
            config_overrides={
                "indoor_temp_source": TEMP_SOURCE_SENSOR,
                # no indoor_temp_entity key
            },
        )
        assert engine._get_indoor_temp_f() is None

    def test_converts_celsius_to_fahrenheit(self):
        # 20°C = 68°F
        hass = _make_hass(climate_temp=20.0)
        engine = _make_engine(hass=hass, config_overrides={"temp_unit": "celsius"})
        result = engine._get_indoor_temp_f()
        assert result is not None
        assert abs(result - 68.0) < 0.1


# ── Temperature proximity check ───────────────────────────────────


class TestWelcomeHomeTemperatureProximityCheck:
    """Notification suppressed when indoor temp is already near comfort."""

    def test_notifies_when_indoor_near_setback_heat(self):
        """62°F: dist_to_comfort=8, dist_to_setback=2 — near setback → notify."""
        hass = _make_hass(climate_temp=62.0)
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="heat")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        _run(engine.handle_occupancy_home())
        notify_mock.assert_called_once()

    def test_suppresses_when_indoor_near_comfort_heat(self):
        """69°F: dist_to_comfort=1, dist_to_setback=9 — near comfort → suppress."""
        hass = _make_hass(climate_temp=69.0)
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="heat")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        _run(engine.handle_occupancy_home())
        notify_mock.assert_not_called()

    def test_notifies_when_indoor_near_setback_cool(self):
        """78°F: dist_to_comfort=3, dist_to_setback=2 — near setback → notify."""
        hass = _make_hass(climate_temp=78.0)
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="cool")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        _run(engine.handle_occupancy_home())
        notify_mock.assert_called_once()

    def test_suppresses_when_indoor_near_comfort_cool(self):
        """76°F: dist_to_comfort=1, dist_to_setback=4 — near comfort → suppress."""
        hass = _make_hass(climate_temp=76.0)
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="cool")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        _run(engine.handle_occupancy_home())
        notify_mock.assert_not_called()

    def test_notifies_when_equidistant(self):
        """65°F exactly midway between setback=60 and comfort=70: dist=5==5, < is False → notify."""
        hass = _make_hass(climate_temp=65.0)
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="heat")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        _run(engine.handle_occupancy_home())
        notify_mock.assert_called_once()

    def test_notifies_when_indoor_temp_unavailable(self):
        """No indoor temp → proximity check skipped → notify."""
        hass = _make_hass(climate_temp=None)
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="heat")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        _run(engine.handle_occupancy_home())
        notify_mock.assert_called_once()

    def test_temperature_restored_regardless_of_suppression(self):
        """Climate service called even when notification is suppressed by proximity check."""
        hass = _make_hass(climate_temp=69.0)  # near comfort → suppress
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="heat")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        _run(engine.handle_occupancy_home())
        # HVAC service was called to restore temperature
        hass.services.async_call.assert_called()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        # Notification suppressed
        notify_mock.assert_not_called()

    def test_proximity_suppression_updates_timestamp(self):
        """Proximity suppression sets _last_welcome_home_notified to prevent debounce reset."""
        hass = _make_hass(climate_temp=69.0)  # near comfort
        engine = _make_engine(hass=hass)
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._notify = AsyncMock()
        assert engine._last_welcome_home_notified is None
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        assert engine._last_welcome_home_notified == _FIXED_NOW

    def test_hvac_off_label_gets_the_proximity_check_of_its_band_family(self):
        """hvac_mode='off' (warm/mild day) is judged on the armed band's family, like a hot day (Issue #1208).

        Rewritten from ``test_no_proximity_check_for_hvac_off``: that premise ("off means nothing is armed, so
        notify regardless of temp") was the bug. Climate Advisor arms the comfort band on "off" days and the arrival
        restore now writes it, so a house already near its comfort edge needs no "Restoring" push, and a house far
        from it still gets one."""
        near = _make_engine(hass=_make_hass(climate_temp=72.0))
        near._current_classification = _make_classification(hvac_mode="off")
        near._notify = AsyncMock()
        _run(near.handle_occupancy_home())
        near._notify.assert_not_called()

        far = _make_engine(hass=_make_hass(climate_temp=85.0))
        far._current_classification = _make_classification(hvac_mode="off")
        far._notify = AsyncMock()
        _run(far.handle_occupancy_home())
        far._notify.assert_called_once()


# ── Debounce check ────────────────────────────────────────────────


_DT_UTIL_NOW = "custom_components.climate_advisor.automation.dt_util.now"
_FIXED_NOW = datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC)


class TestWelcomeHomeDebounce:
    """Notification suppressed when sent within the debounce window."""

    def _engine_with_no_temp(self) -> AutomationEngine:
        """Engine with no indoor temp so debounce check is reached."""
        hass = _make_hass(climate_temp=None)
        return _make_engine(hass=hass)

    def test_notifies_when_no_prior_notification(self):
        engine = self._engine_with_no_temp()
        engine._current_classification = _make_classification(hvac_mode="heat")
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        notify_mock.assert_called_once()

    def test_suppresses_within_debounce_window(self):
        """30 min ago, window=3600s → still within window → suppress."""
        engine = self._engine_with_no_temp()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._last_welcome_home_notified = _FIXED_NOW - timedelta(minutes=30)
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        notify_mock.assert_not_called()

    def test_notifies_after_debounce_expires(self):
        """90 min ago, window=3600s → expired → notify."""
        engine = self._engine_with_no_temp()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._last_welcome_home_notified = _FIXED_NOW - timedelta(minutes=90)
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        notify_mock.assert_called_once()

    def test_debounce_zero_always_notifies(self):
        """CONF_WELCOME_HOME_DEBOUNCE=0 disables debounce entirely."""
        hass = _make_hass(climate_temp=None)
        engine = _make_engine(hass=hass, config_overrides={CONF_WELCOME_HOME_DEBOUNCE: 0})
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._last_welcome_home_notified = _FIXED_NOW - timedelta(seconds=1)
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        notify_mock.assert_called_once()

    def test_debounce_timestamp_updated_on_send(self):
        engine = self._engine_with_no_temp()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._notify = AsyncMock()
        assert engine._last_welcome_home_notified is None
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        assert engine._last_welcome_home_notified == _FIXED_NOW

    def test_debounce_timestamp_not_updated_on_suppression(self):
        """Original timestamp unchanged when suppressed by debounce."""
        engine = self._engine_with_no_temp()
        engine._current_classification = _make_classification(hvac_mode="heat")
        original_time = _FIXED_NOW - timedelta(minutes=30)
        engine._last_welcome_home_notified = original_time
        engine._notify = AsyncMock()
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        assert engine._last_welcome_home_notified == original_time

    def test_custom_debounce_window_respected(self):
        """Custom 10-min window: 5 min ago → suppress, 15 min ago → notify."""
        # 5 min ago — within custom 10-min window → suppress
        hass = _make_hass(climate_temp=None)
        engine = _make_engine(hass=hass, config_overrides={CONF_WELCOME_HOME_DEBOUNCE: 600})
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine._last_welcome_home_notified = _FIXED_NOW - timedelta(minutes=5)
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine.handle_occupancy_home())
        notify_mock.assert_not_called()

        # 15 min ago — outside custom 10-min window → notify
        engine2 = _make_engine(hass=_make_hass(climate_temp=None), config_overrides={CONF_WELCOME_HOME_DEBOUNCE: 600})
        engine2._current_classification = _make_classification(hvac_mode="heat")
        engine2._last_welcome_home_notified = _FIXED_NOW - timedelta(minutes=15)
        notify_mock2 = AsyncMock()
        engine2._notify = notify_mock2
        with patch(_DT_UTIL_NOW, return_value=_FIXED_NOW):
            _run(engine2.handle_occupancy_home())
        notify_mock2.assert_called_once()


# ── No classification ─────────────────────────────────────────────


class TestWelcomeHomeNoClassification:
    """Edge cases with missing classification."""

    def test_returns_safely_with_no_classification(self):
        engine = _make_engine()
        engine._current_classification = None
        notify_mock = AsyncMock()
        engine._notify = notify_mock
        # Must not raise
        _run(engine.handle_occupancy_home())
        notify_mock.assert_not_called()


# ── State persistence ─────────────────────────────────────────────


class TestWelcomeHomeStatePersistence:
    """_last_welcome_home_notified survives serialization/restore cycles."""

    def test_serializable_state_includes_timestamp(self):
        engine = _make_engine()
        ts = datetime(2026, 3, 27, 10, 0, 0, tzinfo=UTC)
        engine._last_welcome_home_notified = ts
        state = engine.get_serializable_state()
        assert state["last_welcome_home_notified"] == ts.isoformat()

    def test_serializable_state_none_when_not_set(self):
        engine = _make_engine()
        engine._last_welcome_home_notified = None
        state = engine.get_serializable_state()
        assert state["last_welcome_home_notified"] is None

    def test_restore_state_sets_timestamp(self):
        engine = _make_engine()
        engine.restore_state({"last_welcome_home_notified": "2026-03-27T10:00:00+00:00"})
        assert engine._last_welcome_home_notified is not None
        assert engine._last_welcome_home_notified.year == 2026

    def test_restore_state_handles_missing_key(self):
        engine = _make_engine()
        engine._last_welcome_home_notified = datetime.now(UTC)  # set something first
        engine.restore_state({})  # key absent
        assert engine._last_welcome_home_notified is None

    def test_restore_state_handles_invalid_iso(self):
        engine = _make_engine()
        engine.restore_state({"last_welcome_home_notified": "not-a-date"})
        assert engine._last_welcome_home_notified is None

    def test_default_welcome_home_debounce_is_3600(self):
        assert DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS == 3600
