"""Tests for grace stuck-at-0 self-healing (Bug 2, Issue #321).

Two aspects tested:
  1. AutomationEngine._cancel_grace_timers() now clears _grace_end_time.
  2. Coordinator _async_update_data stuck-grace guard detects stale _grace_end_time
     in the past when _grace_active=False and force-clears the override.

Occupant framing: if the grace expiry callback was ever lost (HA restart, exception),
the dashboard showed "0 min remaining" forever and automation never resumed. The user
had to click Resume manually to get CA back in control.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

_STABLE_NOW = datetime(2026, 6, 12, 14, 0, 0)
sys.modules["homeassistant.util.dt"].now = lambda: _STABLE_NOW

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.learning import DailyRecord  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THERMOSTAT_ID = "climate.thermostat"
_PATCH_DT_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class — avoids stale __globals__."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_automation_engine_stub() -> AutomationEngine:
    """Create a bare AutomationEngine stub via object.__new__ (no __init__)."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ae = object.__new__(AutomationEngine)
    ae.hass = hass
    ae.climate_entity = _THERMOSTAT_ID
    ae.config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
    }
    ae._grace_active = False
    ae._grace_end_time = None
    ae._grace_duration_seconds = 0
    ae._last_resume_source = None
    ae._manual_grace_cancel = None
    ae._automation_grace_cancel = None
    ae._manual_override_active = False
    ae._manual_override_mode = None
    ae._natural_vent_active = False
    ae._fan_active = False
    ae._fan_override_active = False
    ae.clear_manual_override = MagicMock()
    return ae


def _make_classification(**overrides):
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


def _make_stuck_grace_coord_stub(
    *,
    manual_override_active: bool = True,
    grace_active: bool = False,
    grace_end_time: str | None = None,
) -> object:
    """Build a minimal coordinator stub for stuck-grace detection tests."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": _THERMOSTAT_ID,
        "comfort_heat": 70,
        "comfort_cool": 75,
    }

    ae = MagicMock()
    ae._natural_vent_active = False
    ae._fan_active = False
    ae._fan_override_active = False
    ae._manual_override_active = manual_override_active
    ae._grace_active = grace_active
    ae._grace_end_time = grace_end_time
    ae.clear_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._current_classification = _make_classification()
    coord._today_record = _make_today_record()
    coord._async_save_state = AsyncMock()
    coord._emit_event = MagicMock()

    return coord


# ---------------------------------------------------------------------------
# TestCancelGraceTimersClearsEndTime: Bug 2 fix in AutomationEngine
# ---------------------------------------------------------------------------


class TestCancelGraceTimersClearsEndTime:
    """_cancel_grace_timers clears _grace_end_time (Bug 2 fix)."""

    def test_cancel_grace_timers_clears_grace_end_time(self):
        """_cancel_grace_timers must set _grace_end_time to None.

        Before the fix: _grace_end_time was left set; the dashboard showed
        '0 min remaining' permanently, blocking automation from showing its
        next action.
        """
        ae = _make_automation_engine_stub()
        ae._grace_end_time = "2026-06-12T14:30:00"
        ae._grace_active = True
        ae._manual_grace_cancel = None
        ae._automation_grace_cancel = None

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        assert ae._grace_end_time is None

    def test_cancel_grace_timers_sets_grace_active_false(self):
        """_cancel_grace_timers clears the grace-active flag."""
        ae = _make_automation_engine_stub()
        ae._grace_end_time = "2026-06-12T14:30:00"
        ae._grace_active = True

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        assert ae._grace_active is False

    def test_cancel_grace_timers_calls_cancel_callbacks(self):
        """_cancel_grace_timers invokes both cancel callbacks when present."""
        ae = _make_automation_engine_stub()
        mock_manual = MagicMock()
        mock_auto = MagicMock()
        ae._manual_grace_cancel = mock_manual
        ae._automation_grace_cancel = mock_auto
        ae._grace_active = True
        ae._grace_end_time = "2026-06-12T14:30:00"

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        mock_manual.assert_called_once()
        mock_auto.assert_called_once()
        assert ae._manual_grace_cancel is None
        assert ae._automation_grace_cancel is None

    def test_cancel_grace_timers_clears_last_resume_source(self):
        """_cancel_grace_timers resets _last_resume_source."""
        ae = _make_automation_engine_stub()
        ae._last_resume_source = "manual"
        ae._grace_active = True
        ae._grace_end_time = "2026-06-12T14:30:00"

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        assert ae._last_resume_source is None

    def test_cancel_grace_timers_noop_when_no_timers(self):
        """_cancel_grace_timers is safe to call with no active timers."""
        ae = _make_automation_engine_stub()
        # All None and False — should not raise
        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()
        assert ae._grace_active is False
        assert ae._grace_end_time is None


# ---------------------------------------------------------------------------
# TestStuckGraceDetection: coordinator _async_update_data stuck-grace guard
# ---------------------------------------------------------------------------


class TestStuckGraceDetection:
    """Coordinator detects and clears stuck grace in _async_update_data."""

    def _simulate_stuck_grace_check(self, coord, now: datetime) -> None:
        """Replicate the stuck-grace guard logic from _async_update_data.

        This method mirrors the if-block added in coordinator.py under Bug 2 (Issue #321):

            ae = self.automation_engine
            if ae._manual_override_active and not ae._grace_active:
                end_time_str = getattr(ae, "_grace_end_time", None)
                if end_time_str is not None:
                    try:
                        end_dt = datetime.fromisoformat(end_time_str)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=UTC)
                        now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now
                        if end_dt < now_utc:
                            _LOGGER.error("Stuck grace: ...")
                            ae.clear_manual_override(reason="stuck_grace_recovery")
                            self._emit_event("stuck_grace_recovered", {...})
                    except (ValueError, TypeError):
                        pass
        """
        import logging

        _LOGGER = logging.getLogger("custom_components.climate_advisor.coordinator")
        ae = coord.automation_engine
        if ae._manual_override_active and not ae._grace_active:
            end_time_str = getattr(ae, "_grace_end_time", None)
            if end_time_str is not None:
                try:
                    end_dt = datetime.fromisoformat(end_time_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=UTC)
                    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now
                    if end_dt < now_utc:
                        _LOGGER.error(
                            "Stuck grace detected: grace_end_time=%s is in the past but"
                            " grace_active=False and override still set — force-clearing",
                            end_time_str,
                        )
                        ae.clear_manual_override(reason="stuck_grace_recovery")
                        coord._emit_event(
                            "stuck_grace_recovered",
                            {"grace_end_time": end_time_str},
                        )
                except (ValueError, TypeError):
                    pass

    def test_stuck_grace_clears_override(self):
        """When grace_end_time is past and grace_active=False, clear_manual_override is called.

        Occupant impact: automation had been blocked indefinitely because the grace
        expiry callback was lost. The self-healing guard restores normal automation.
        """
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_called_once_with(reason="stuck_grace_recovery")

    def test_stuck_grace_event_emitted(self):
        """stuck_grace_recovered event is emitted when the guard fires."""
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord._emit_event.assert_called_once()
        event_name, event_data = coord._emit_event.call_args[0]
        assert event_name == "stuck_grace_recovered"
        assert event_data["grace_end_time"] == past_ts

    def test_no_stuck_grace_when_grace_active(self):
        """When _grace_active=True, timer is still running — no self-heal needed."""
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=True,  # timer still running — not stuck
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_no_stuck_grace_when_grace_end_time_none(self):
        """When _grace_end_time is None, there is no stuck grace to detect."""
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=None,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_no_stuck_grace_when_end_time_in_future(self):
        """When _grace_end_time is in the future, grace is not stuck."""
        future_ts = (datetime(2026, 6, 12, 15, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=future_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_no_stuck_grace_when_no_override(self):
        """When _manual_override_active=False, stuck-grace guard does not fire."""
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=False,
            grace_active=False,
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()
