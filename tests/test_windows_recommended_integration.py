"""Tests for windows-recommended + door/window interaction.

Issue #51: Door/window pause should NOT fire during planned window-open periods.
Issue #53: Comprehensive automation logic table test coverage.

These tests validate that when the classifier recommends windows (warm/mild day)
AND the current time is within the window period, door/window sensor events
do NOT trigger HVAC pause, grace periods, or notifications — even when the
thermostat is currently in a non-"off" mode (e.g., due to a manual override or
a prior classification that set heating/cooling).

TDD approach: tests that exercise the suppression logic are expected to FAIL
until `_is_within_planned_window_period()` is implemented in automation.py
and `handle_door_window_open` is updated to call it before pausing.

Key insight on the existing guard:
    Line 355 in handle_door_window_open checks `_pre_pause_mode != "off"`.
    For warm/mild days the classification sets HVAC to "off", so without a
    prior manual override the pause already won't fire.  The new suppression
    method is needed for the case where the thermostat is NOT currently "off"
    but windows are still recommended — e.g. the user manually engaged cooling
    on a warm morning but the classifier still says "windows period, don't
    fight the user".  Tests 1, 2, and 4 use HVAC in "cool" mode to expose this
    real-world gap.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    WARM_WINDOW_CLOSE_HOUR,
    WARM_WINDOW_OPEN_HOUR,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close a coroutine to prevent 'never awaited' RuntimeWarnings."""
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config: dict = {
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


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


def _warm_day_classification_with_windows() -> DayClassification:
    """Return a warm-day classification that has windows_recommended=True.

    Warm day: today_high in [75, 85), today_low <= 72 triggers windows_recommended.
    Window period: WARM_WINDOW_OPEN_HOUR (6 AM) to WARM_WINDOW_CLOSE_HOUR (10 AM).
    hvac_mode computed by _compute_recommendations() will be "off".
    """
    return DayClassification(
        day_type=DAY_TYPE_WARM,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=80.0,
        today_low=62.0,  # <= 72 => windows_recommended = True
        tomorrow_high=82.0,
        tomorrow_low=64.0,
    )


def _mild_day_classification() -> DayClassification:
    """Return a mild-day classification (always windows_recommended=True).

    Window period: 10:00 AM to 5:00 PM.
    hvac_mode computed by _compute_recommendations() will be "off".
    """
    return DayClassification(
        day_type=DAY_TYPE_MILD,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=68.0,
        today_low=55.0,
        tomorrow_high=70.0,
        tomorrow_low=57.0,
    )


def _hot_day_classification() -> DayClassification:
    """Return a hot-day classification (windows_recommended=False, hvac_mode='cool')."""
    return DayClassification(
        day_type=DAY_TYPE_HOT,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=95.0,
        today_low=75.0,
        tomorrow_high=93.0,
        tomorrow_low=74.0,
    )


def _make_dt(hour: int, minute: int = 0) -> datetime:
    """Return a timezone-aware datetime for today at the given hour/minute."""
    return datetime(2026, 3, 21, hour, minute, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Class: TestWindowsRecommendedPauseSuppression
# ---------------------------------------------------------------------------


class TestWindowsRecommendedPauseSuppression:
    """Validate that door/window open events do NOT pause HVAC when the
    classifier has recommended windows and the current time falls within
    the planned window period.

    The gap being tested: if the user manually put HVAC into "cool" mode on a
    warm morning (before the classifier ran, or via an override), the thermostat
    state is "cool" while windows_recommended=True and the time is inside the
    window period.  Without the new suppression method, `handle_door_window_open`
    would incorrectly turn HVAC off and send a notification.

    Tests 1, 2, 4, 7, and the first part of 6 are expected to FAIL until
    `_is_within_planned_window_period()` is implemented and wired into
    `handle_door_window_open`.
    """

    # ------------------------------------------------------------------
    # Test 1 — warm day, HVAC=cool (manual override), inside window period
    # ------------------------------------------------------------------

    def test_no_pause_when_windows_recommended_warm_day(self):
        """Warm day, windows_recommended=True, HVAC currently 'cool' (manual override),
        time inside window period (8 AM, window is 6–10 AM).

        Without suppression: the existing code sees _pre_pause_mode='cool',
        fires the pause, and turns HVAC off.
        With suppression: handle_door_window_open detects the window period
        and skips the pause entirely.

        FAILS until _is_within_planned_window_period() is implemented.
        """
        engine = _make_automation_engine()
        classification = _warm_day_classification_with_windows()

        # Sanity-check the dataclass computed the right values
        assert classification.windows_recommended is True
        assert classification.hvac_mode == "off"
        assert classification.window_open_time == time(WARM_WINDOW_OPEN_HOUR, 0)
        assert classification.window_close_time == time(WARM_WINDOW_CLOSE_HOUR, 0)

        # Set classification without triggering apply_classification's HVAC call,
        # so we can place the thermostat in 'cool' mode independently.
        engine._current_classification = classification

        # Thermostat is in 'cool' — e.g. user manually turned it on this morning
        engine.hass.states.get.return_value = _make_state("cool")
        engine.hass.services.async_call.reset_mock()

        # 8:00 AM is well inside the warm-day window (6–10 AM)
        mock_now = _make_dt(8, 0)
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util:
            mock_dt_util.now.return_value = mock_now
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Suppression must have prevented the pause
        assert engine._paused_by_door is False, (
            "HVAC should NOT be paused during a planned warm-day window period "
            "even when the thermostat is in 'cool' mode"
        )
        engine.hass.services.async_call.assert_not_called()

    # ------------------------------------------------------------------
    # Test 2 — mild day, HVAC=heat (manual override), inside window period
    # ------------------------------------------------------------------

    def test_no_pause_when_windows_recommended_mild_day(self):
        """Mild day, windows_recommended=True, HVAC currently 'heat' (manual override),
        time inside window period (12 PM, window is 10 AM–5 PM).

        FAILS until _is_within_planned_window_period() is implemented.
        """
        engine = _make_automation_engine()
        classification = _mild_day_classification()

        assert classification.windows_recommended is True
        assert classification.hvac_mode == "off"
        assert classification.window_open_time == time(10, 0)
        assert classification.window_close_time == time(17, 0)

        # Inject classification directly; thermostat is in 'heat' (manual override)
        engine._current_classification = classification
        engine.hass.states.get.return_value = _make_state("heat")
        engine.hass.services.async_call.reset_mock()

        # 12:00 PM is inside the mild-day window
        mock_now = _make_dt(12, 0)
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util:
            mock_dt_util.now.return_value = mock_now
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is False, (
            "HVAC should NOT be paused during a planned mild-day window period "
            "even when the thermostat is in 'heat' mode"
        )
        engine.hass.services.async_call.assert_not_called()

    # ------------------------------------------------------------------
    # Test 3 — hot day (regression guard): pause DOES fire
    # ------------------------------------------------------------------

    def test_pause_still_fires_for_hot_day(self):
        """Hot day, windows_recommended=False, HVAC=cool — pause must still fire.

        This is a regression guard: the existing pause logic must remain intact
        for days where windows are NOT recommended. This test SHOULD PASS with
        the existing code and must continue to pass after the feature is added.
        """
        engine = _make_automation_engine()
        classification = _hot_day_classification()

        assert classification.windows_recommended is False
        assert classification.hvac_mode == "cool"

        asyncio.run(engine.apply_classification(classification))

        # Thermostat is currently in "cool" mode
        engine.hass.states.get.return_value = _make_state("cool")
        engine.hass.services.async_call.reset_mock()

        mock_now = _make_dt(8, 0)
        with (
            patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util,
            patch("custom_components.climate_advisor.automation.async_call_later"),
        ):
            mock_dt_util.now.return_value = mock_now
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True, "HVAC MUST be paused on a hot day when a door/window opens"
        engine.hass.services.async_call.assert_any_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.thermostat", "hvac_mode": "off"},
        )

    # ------------------------------------------------------------------
    # Test 4 — warm day, HVAC=cool, inside window period: _grace_active stays False
    # ------------------------------------------------------------------

    def test_no_grace_when_windows_recommended(self):
        """When suppression is active, handle_door_window_open must NOT start
        a grace period (since it never paused in the first place).

        FAILS until _is_within_planned_window_period() is implemented.
        """
        engine = _make_automation_engine()
        classification = _warm_day_classification_with_windows()

        # Inject classification; thermostat in 'cool' (manual override scenario)
        engine._current_classification = classification
        engine.hass.states.get.return_value = _make_state("cool")
        engine.hass.services.async_call.reset_mock()

        mock_now = _make_dt(7, 30)  # inside warm-day window (6–10 AM)
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util:
            mock_dt_util.now.return_value = mock_now
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is False, "No pause should occur during the planned window period"
        assert engine._grace_active is False, "No grace period should be started when window suppression is active"

    # ------------------------------------------------------------------
    # Test 5 — grace expiry with sensor open during window period: no re-pause
    # ------------------------------------------------------------------

    def test_grace_expiry_no_repause_during_window_period(self):
        """When grace expires and sensor is still open, _re_pause_for_open_sensor
        must NOT re-pause if _is_within_planned_window_period() is True.

        Scenario:
        1. Engine has an active grace period (from a prior close-then-reopen).
        2. The classification is warm/windows_recommended=True.
        3. Grace expires; sensor is still open; time is inside window period.
        4. Expected: no re-pause, _paused_by_door stays False.

        FAILS until _is_within_planned_window_period() is implemented and
        the grace-expired callback (in _start_grace_period) respects it.
        """
        engine = _make_automation_engine()

        # Sensor is still open when grace expires
        engine._sensor_check_callback = MagicMock(return_value=True)

        # Warm day with windows recommended
        classification = _warm_day_classification_with_windows()
        engine._current_classification = classification

        captured_callbacks: list = []

        def _capture_call_later(hass_arg, duration, cb):
            captured_callbacks.append(cb)
            return MagicMock()

        mock_now = _make_dt(8, 0)  # inside warm-day window (6–10 AM)
        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=_capture_call_later,
            ),
            patch(
                "custom_components.climate_advisor.automation.callback",
                side_effect=lambda fn: fn,
            ),
            patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util,
        ):
            mock_dt_util.now.return_value = mock_now
            engine._start_grace_period("automation")

        assert len(captured_callbacks) == 1, "Expected exactly one async_call_later call"
        grace_expired_cb = captured_callbacks[0]

        # Reset tracking after grace setup
        engine.hass.services.async_call.reset_mock()
        engine.hass.async_create_task.reset_mock()

        # Fire the grace-expired callback; sensor is open; time is in window
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util:
            mock_dt_util.now.return_value = mock_now
            grace_expired_cb(mock_now)

        # Grace should be cleared (callback ran to completion)
        assert engine._grace_active is False

        # Must NOT have enqueued _re_pause_for_open_sensor
        assert engine._paused_by_door is False, (
            "_re_pause_for_open_sensor must be suppressed during a planned window period"
        )
        engine.hass.async_create_task.assert_not_called()

    # ------------------------------------------------------------------
    # Test 6 — classification changes warm→hot: pause re-enabled
    # ------------------------------------------------------------------

    def test_classification_change_warm_to_hot_enables_pause(self):
        """After changing classification from warm to hot, opening a sensor
        must pause HVAC.

        Part A (warm, inside window, HVAC='cool'): suppression must prevent pause.
          -> FAILS until _is_within_planned_window_period() is implemented.

        Part B (hot, HVAC='cool'): pause must fire.
          -> SHOULD PASS with existing code; must remain passing after the feature.
        """
        engine = _make_automation_engine()

        # --- Part A: Warm day, suppression active ---
        warm_cls = _warm_day_classification_with_windows()
        engine._current_classification = warm_cls
        engine.hass.states.get.return_value = _make_state("cool")
        engine.hass.services.async_call.reset_mock()

        mock_now_inside = _make_dt(8, 0)  # inside warm-day window
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util:
            mock_dt_util.now.return_value = mock_now_inside
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is False, (
            "Part A: Should NOT pause on warm day inside window period when thermostat is 'cool'"
        )

        # --- Part B: Hot day, suppression inactive ---
        hot_cls = _hot_day_classification()
        asyncio.run(engine.apply_classification(hot_cls))
        engine.hass.services.async_call.reset_mock()

        # Thermostat in "cool" (set by apply_classification above)
        engine.hass.states.get.return_value = _make_state("cool")

        mock_now_hot = _make_dt(8, 0)  # time irrelevant for hot day
        with (
            patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util,
            patch("custom_components.climate_advisor.automation.async_call_later"),
        ):
            mock_dt_util.now.return_value = mock_now_hot
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True, "Part B: Must pause on hot day when sensor opens"

    # ------------------------------------------------------------------
    # Test 7 — method existence and return value
    # ------------------------------------------------------------------

    def test_engine_has_is_within_planned_window_period_method(self):
        """AutomationEngine must expose _is_within_planned_window_period().

        FAILS until the method is added to AutomationEngine.
        """
        engine = _make_automation_engine()
        assert hasattr(engine, "_is_within_planned_window_period"), (
            "AutomationEngine must implement _is_within_planned_window_period()"
        )
        assert callable(engine._is_within_planned_window_period), "_is_within_planned_window_period must be callable"

    def test_is_within_planned_window_period_returns_true_for_warm_day_inside_window(self):
        """_is_within_planned_window_period() returns True when all conditions met:
        - _current_classification.windows_recommended is True
        - _current_classification.hvac_mode == "off"
        - current time is between window_open_time and window_close_time

        FAILS until the method is implemented.
        """
        engine = _make_automation_engine()
        classification = _warm_day_classification_with_windows()
        engine._current_classification = classification

        assert engine._current_classification.windows_recommended is True
        assert engine._current_classification.hvac_mode == "off"

        mock_now = _make_dt(8, 0)  # inside 6–10 AM warm window
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util:
            mock_dt_util.now.return_value = mock_now
            result = engine._is_within_planned_window_period()

        assert result is True, (
            "_is_within_planned_window_period() must return True at 8 AM "
            "during a warm-day classification with windows_recommended=True"
        )

    # ------------------------------------------------------------------
    # Test 8 — outside window period: suppression inactive
    # ------------------------------------------------------------------

    def test_pause_fires_outside_window_period_with_active_hvac(self):
        """Warm day, windows_recommended=True, but time is OUTSIDE window period.

        At 11:00 AM — after warm-day window closes at 10:00 AM — the
        suppression must NOT be active.  With HVAC in 'cool' mode, the
        existing pause logic should fire normally.

        Part A: _is_within_planned_window_period() must return False at 11 AM.
          -> FAILS until the method is implemented (AttributeError).

        Part B: handle_door_window_open must pause HVAC at 11 AM when HVAC='cool'.
          -> SHOULD PASS with existing code (no suppression, existing guard allows it).
        """
        engine = _make_automation_engine()
        classification = _warm_day_classification_with_windows()
        engine._current_classification = classification

        # Thermostat is in 'cool' — user has HVAC running
        engine.hass.states.get.return_value = _make_state("cool")
        engine.hass.services.async_call.reset_mock()

        # 11:00 AM — outside warm-day window (closed at 10 AM)
        mock_now = _make_dt(11, 0)
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt_util:
            mock_dt_util.now.return_value = mock_now

            # Part A: method must report False outside the window
            if hasattr(engine, "_is_within_planned_window_period"):
                result = engine._is_within_planned_window_period()
                assert result is False, (
                    "_is_within_planned_window_period() must return False at 11 AM (warm-day window closes at 10 AM)"
                )

            # Part B: pause must fire (suppression not active, HVAC is 'cool')
            with patch("custom_components.climate_advisor.automation.async_call_later"):
                asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True, "Pause must fire outside the window period when HVAC is active"
