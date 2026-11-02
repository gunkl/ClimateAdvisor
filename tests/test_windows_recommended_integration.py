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
import importlib as _importlib
from datetime import UTC, datetime, time
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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
    """Door/window events inside the planned window period (Issue #1211, A3, owner decision O8b).

    The planned window period is no longer an exemption in the door/window EVENT handlers: a window opened (past
    the debounce) inside the period gets the same decision as any other -- the nat-vent entry gate starts
    nat-vent when it passes, and the HVAC pauses right there when it refuses (these engines have no fan, so it
    refuses). Only the lazy ``_sync_paused_by_door_with_live_sensors()`` reconcile, which has no event and no
    occupant action, is still exempt inside the period; when the period ends,
    ``async_sync_after_planned_window_exit()`` runs it once.

    The tests in this class were written for the pre-#1211 design (the handler returned silently inside the
    period) and now pin the post-#1211 outcomes; the class name is kept so history and references still
    resolve. Original scenario for context: the occupant put HVAC in "cool" on a warm morning with
    windows_recommended=True inside the window period; the open window must be dealt with in the handler, not
    left to the next coordinator refresh.
    """

    # ------------------------------------------------------------------
    # Test 1 — warm day, HVAC=cool (manual override), inside window period
    # ------------------------------------------------------------------

    def test_pauses_inside_warm_planned_window_when_gate_refuses(self):
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

        # Issue #1211: no exemption -- the open window pauses the cooling (rule 3), nat-vent being refused
        assert engine._paused_by_door is True, (
            "HVAC must be paused by an open window inside a planned warm-day period when nat-vent is refused"
        )
        engine.hass.services.async_call.assert_any_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.thermostat", "hvac_mode": "off"},
            blocking=True,
            context=ANY,
        )

    # ------------------------------------------------------------------
    # Test 2 — mild day, HVAC=heat (manual override), inside window period
    # ------------------------------------------------------------------

    def test_pauses_inside_mild_planned_window_when_gate_refuses(self):
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

        assert engine._paused_by_door is True, (
            "HVAC must be paused by an open window inside a planned mild-day period when nat-vent is refused"
        )

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
            blocking=True,
            context=ANY,
        )

    # ------------------------------------------------------------------
    # Test 4 — warm day, HVAC=cool, inside window period: _grace_active stays False
    # ------------------------------------------------------------------

    def test_pause_without_grace_inside_planned_window(self):
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

        assert engine._paused_by_door is True, "Issue #1211: the planned window period no longer suppresses the pause"
        assert engine._grace_active is False, "A pause does not start a grace period"

    # ------------------------------------------------------------------
    # Test 5 — grace expiry with sensor open during window period: no re-pause
    # ------------------------------------------------------------------

    def test_grace_expiry_repauses_during_window_period(self):
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

        # Issue #1211 (A3): the re-pause IS enqueued inside the planned window period (it runs the nat-vent gate
        # and pauses when it refuses); the planned window is no longer a grace-expiry exemption.
        names = []
        for call_args in engine.hass.async_create_task.call_args_list:
            coro = call_args[0][0]
            names.append(getattr(coro, "__qualname__", "") or getattr(coro, "__name__", ""))
            close = getattr(coro, "close", None)
            if close:
                close()  # never awaited here; avoid the "never awaited" warning
        assert any("_re_pause_for_open_sensor" in n for n in names), names

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

        assert engine._paused_by_door is True, (
            "Part A: Issue #1211 -- pauses on a warm day inside the window period when nat-vent is refused"
        )
        engine._paused_by_door = False  # reset for part B
        engine._pre_pause_mode = None

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


# ---------------------------------------------------------------------------
# Stub factory for _apply_outdoor_windows_gate() tests
# ---------------------------------------------------------------------------


def _make_gate_coord_stub(
    outdoor_temp,
    *,
    day_type=DAY_TYPE_MILD,
    windows_recommended=True,
    today_low=55.0,
    comfort_cool=75.0,
    comfort_heat=70.0,
    indoor=None,
):
    """Create a minimal coordinator stub for _apply_outdoor_windows_gate() tests.

    ``indoor`` is the live indoor reading (None = no sensor): Issue #1211 made the gate indoor-aware.
    """
    coord_mod = _importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(coord_mod.ClimateAdvisorCoordinator)

    cls_obj = DayClassification(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=80.0 if day_type == DAY_TYPE_WARM else 68.0,
        today_low=today_low,
        tomorrow_high=82.0 if day_type == DAY_TYPE_WARM else 70.0,
        tomorrow_low=today_low + 2.0,
    )
    cls_obj.windows_recommended = windows_recommended

    coord._current_classification = cls_obj
    coord._last_outdoor_temp = outdoor_temp
    coord._get_indoor_temp = MagicMock(return_value=indoor)
    coord._schedule_planned_window_exit_check = MagicMock()
    coord.config = {
        "comfort_cool": comfort_cool,
        "comfort_heat": comfort_heat,
    }
    return coord


# ---------------------------------------------------------------------------
# Issue #111: outdoor temperature gate for windows_recommended
# ---------------------------------------------------------------------------


class TestOutdoorTempGate:
    """Validate _apply_outdoor_windows_gate() on ClimateAdvisorCoordinator.

    Issue #111: windows_recommended must account for current outdoor conditions.
    The gate clears windows_recommended=True when outdoor temp is:
      - above comfort_cool (would push indoor over comfort ceiling), OR
      - below comfort_heat - 15 degrees F (extreme cold, not comfortable to open)

    All 7 tests FAIL before _apply_outdoor_windows_gate() is implemented
    (AttributeError: 'ClimateAdvisorCoordinator' object has no attribute
    '_apply_outdoor_windows_gate').
    """

    def test_outdoor_above_comfort_cool_disables(self):
        """Outdoor temp above comfort_cool clears windows_recommended to False."""
        coord = _make_gate_coord_stub(80.0, comfort_cool=75.0)
        assert coord._current_classification.windows_recommended is True

        coord._apply_outdoor_windows_gate()

        assert coord._current_classification.windows_recommended is False, (
            "outdoor=80 > comfort_cool=75 should disable windows_recommended"
        )

    def test_outdoor_at_comfort_cool_allows(self):
        """Outdoor at exactly comfort_cool stays True (> is the disable condition, not >=)."""
        coord = _make_gate_coord_stub(75.0, comfort_cool=75.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True, (
            "outdoor=75 == comfort_cool=75 should NOT disable windows_recommended"
        )

    def test_outdoor_in_range_allows(self):
        """Outdoor temp well within comfort range keeps windows_recommended True."""
        coord = _make_gate_coord_stub(68.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True

    def test_outdoor_extreme_cold_disables(self):
        """Outdoor below comfort_heat - 15 clears windows_recommended to False."""
        coord = _make_gate_coord_stub(52.0, comfort_heat=70.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is False, (
            "outdoor=52 < threshold=55 (comfort_heat=70 - 15) should disable windows_recommended"
        )

    def test_outdoor_at_cold_threshold_allows(self):
        """Outdoor at exactly comfort_heat - 15 stays True (< is the disable condition)."""
        coord = _make_gate_coord_stub(55.0, comfort_heat=70.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True, (
            "outdoor=55 == threshold=55 should NOT disable windows_recommended"
        )

    def test_outdoor_none_preserves_classifier(self):
        """No outdoor temp data keeps the classifier's recommendation unchanged."""
        coord = _make_gate_coord_stub(None)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True, (
            "outdoor=None should keep windows_recommended=True from classifier"
        )

    def test_warm_day_outdoor_above_comfort_cool(self):
        """WARM day eligible classification with outdoor above comfort_cool is disabled."""
        coord = _make_gate_coord_stub(
            80.0,
            day_type=DAY_TYPE_WARM,
            today_low=68.0,
            windows_recommended=True,
            comfort_cool=75.0,
        )
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is False, (
            "WARM day with outdoor=80 > comfort_cool=75 should disable windows_recommended"
        )


class TestOutdoorGateIsIndoorAware:
    """Issue #1211 (owner decision O3): with an indoor reading the gate asks which way the air moves the house,
    instead of only "outdoor more than 15 F below the floor"."""

    def test_cold_house_and_colder_outdoor_clears(self):
        # indoor 65 < floor 70; outdoor 60 is colder -> windows would chill it further. (60 is not under the old
        # floor-15 = 55 cutoff, so this is the new behaviour.)
        coord = _make_gate_coord_stub(60.0, comfort_heat=70.0, indoor=65.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is False
        coord._schedule_planned_window_exit_check.assert_called_once()

    def test_cold_house_and_warmer_outdoor_keeps(self):
        coord = _make_gate_coord_stub(68.0, comfort_heat=70.0, indoor=65.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True
        coord._schedule_planned_window_exit_check.assert_not_called()

    def test_hot_house_and_warmer_outdoor_clears(self):
        coord = _make_gate_coord_stub(74.0, comfort_cool=75.0, indoor=78.0 + 1.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True  # 74 < 79: cooler, helps
        coord = _make_gate_coord_stub(74.0, comfort_cool=72.0, indoor=73.0 - 0.5)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is False  # outdoor 74 > comfort_cool 72

    def test_in_band_house_is_not_vetoed_by_cold_outdoor(self):
        coord = _make_gate_coord_stub(45.0, comfort_heat=70.0, comfort_cool=75.0, indoor=72.0)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True

    def test_no_indoor_reading_keeps_the_extreme_cold_cutoff(self):
        coord = _make_gate_coord_stub(52.0, comfort_heat=70.0, indoor=None)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is False


class TestClassifyKwargs:
    """Issue #1211: every classify_day() call passes the configured comfort band and a same-day hysteresis seed."""

    def _coord(self, *, forecast_flag, fetched):
        coord_mod = _importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = object.__new__(coord_mod.ClimateAdvisorCoordinator)
        coord.config = {"comfort_heat": 66, "comfort_cool": 78}
        cls_obj = _mild_day_classification()
        cls_obj.forecast_windows_recommended = forecast_flag
        coord._current_classification = cls_obj
        coord._classification_fetched_at = fetched
        return coord

    def test_passes_the_configured_comfort_band(self):
        coord = self._coord(forecast_flag=False, fetched=None)
        kwargs = coord._classify_kwargs()
        assert kwargs["comfort_heat"] == 66.0
        assert kwargs["comfort_cool"] == 78.0

    def test_seeds_hysteresis_from_todays_previous_classification(self):
        now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
        coord = self._coord(forecast_flag=True, fetched=now)
        with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
            dt.now.return_value = now
            assert coord._classify_kwargs()["previous_windows_recommended"] is True
            assert coord._classify_kwargs(seed_hysteresis=False)["previous_windows_recommended"] is False

    def test_yesterdays_answer_is_never_carried_over(self):
        coord = self._coord(forecast_flag=True, fetched=datetime(2026, 4, 21, 12, 0, tzinfo=UTC))
        with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
            dt.now.return_value = datetime(2026, 4, 22, 6, 0, tzinfo=UTC)
            assert coord._classify_kwargs()["previous_windows_recommended"] is False
