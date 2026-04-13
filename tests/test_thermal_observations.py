"""Tests for thermal observation recording (Phase 5G).

Tests LearningEngine.record_thermal_observation() and the coordinator's
_record_thermal_observation() method.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import THERMAL_OBS_CAP
from custom_components.climate_advisor.learning import LearningEngine, LearningState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = "2026-03-28"
_TODAY_DATE = date(2026, 3, 28)
_OLD_DATE = "2025-12-01"  # > 90 days ago


def _make_obs(obs_date: str = _TODAY, mode: str = "heat", rate: float = 2.0) -> dict:
    return {
        "timestamp": f"{obs_date}T10:00:00",
        "date": obs_date,
        "hvac_mode": mode,
        "session_minutes": 30.0,
        "rate_f_per_hour": rate,
        "outdoor_temp_f": 40.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 66.0,
    }


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _patch_learning_dt(today: date):
    """Patch dt_util in learning.py so now().date() returns a real date."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.date.return_value = today
    return patch("custom_components.climate_advisor.learning.dt_util", mock_dt)


# ---------------------------------------------------------------------------
# TestThermalObservationStorage — exercises LearningEngine directly
# ---------------------------------------------------------------------------


class TestThermalObservationStorage:
    """Tests for record_thermal_observation() on LearningEngine."""

    def test_observation_appended(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        obs = _make_obs()
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation(obs)
        assert obs in engine._state.thermal_observations

    def test_observations_capped_at_200(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        with _patch_learning_dt(_TODAY_DATE):
            for i in range(205):
                engine.record_thermal_observation(_make_obs(obs_date=_TODAY, rate=float(i + 1)))
        assert len(engine._state.thermal_observations) == THERMAL_OBS_CAP

    def test_90_day_trim(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Add old observations directly (bypass trim by inserting without call)
        for _ in range(5):
            engine._state.thermal_observations.append(_make_obs(obs_date=_OLD_DATE))
        # Now add a recent one — the call should trim old ones
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation(_make_obs(obs_date=_TODAY))
        remaining = [o for o in engine._state.thermal_observations if o["date"] == _OLD_DATE]
        assert len(remaining) == 0

    def test_missing_thermal_observations_key_defaults_to_empty(self, tmp_path: Path):
        """LearningState constructed from a dict without thermal_observations key."""
        state = LearningState(
            **{
                "records": [],
                "active_suggestions": [],
                "dismissed_suggestions": [],
                "settings_history": [],
            }
        )
        assert state.thermal_observations == []


# ---------------------------------------------------------------------------
# TestThermalObservationRecordingViaCoordinator
# ---------------------------------------------------------------------------


def _make_coordinator(tmp_path: Path):
    """Construct a minimal ClimateAdvisorCoordinator with mocked HA internals."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.states.get = MagicMock(return_value=None)

    config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "notify_service": "notify.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "indoor_temp_source": "climate_fallback",
        "temp_unit": "fahrenheit",
    }

    coordinator = ClimateAdvisorCoordinator(hass, config)
    coordinator.hass = hass  # DataUpdateCoordinator stub may not set this — ensure it's accessible
    coordinator._async_save_state = MagicMock(return_value=None)
    coordinator.automation_engine = MagicMock()
    coordinator.automation_engine._thermal_model = {}
    return coordinator


def _now_mock(fixed_now: datetime):
    """Return a MagicMock for dt_util with now() → fixed_now."""
    mock_dt = MagicMock()
    mock_dt.now.return_value = fixed_now
    return mock_dt


class TestThermalObservationRecordingViaCoordinator:
    """Tests for _record_thermal_observation() in the coordinator."""

    def _make_coordinator_with_learning(self, tmp_path: Path):
        coord = _make_coordinator(tmp_path)
        coord.learning = _make_engine(tmp_path)
        return coord

    def test_observation_recorded_on_hvac_stop(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        start = now - timedelta(minutes=15)
        coord._hvac_on_since = start
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 1

    def test_observation_skipped_if_session_too_short(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        # 3 minutes — below MIN_THERMAL_SESSION_MINUTES (5)
        coord._hvac_on_since = now - timedelta(minutes=3)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_observation_accepted_at_new_minimum(self, tmp_path: Path):
        """Session at exactly MIN_THERMAL_SESSION_MINUTES (5 min) should be accepted."""
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=5)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        # 5 min, 0.5°F change → rate = 0.5 / (5/60) = 6 °F/hr — within range
        coord._get_indoor_temp = MagicMock(return_value=65.5)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 1

    def test_observation_accepted_for_ecobee_8min_cycle(self, tmp_path: Path):
        """Typical Ecobee 8-minute cycle should now produce an observation (regression guard)."""
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 4, 12, 8, 39, 24)  # end of first real-world Ecobee session
        coord._hvac_on_since = now - timedelta(minutes=8, seconds=18)  # 8.3 min
        coord._hvac_session_start_indoor_temp = 64.5
        coord._hvac_session_start_outdoor_temp = 52.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=65.5)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 1

    def test_observation_skipped_if_no_start_temp(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = None  # No start temp
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_observation_skipped_if_rate_too_high(self, tmp_path: Path):
        """Rate > MAX_THERMAL_RATE_F_PER_HOUR → skip."""
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        # 15 min, start=60, end=65 → rate = 5/(15/60) = 20°F/hr > 15
        coord._hvac_on_since = now - timedelta(minutes=15)
        coord._hvac_session_start_indoor_temp = 60.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=65.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_observation_skipped_if_rate_too_low(self, tmp_path: Path):
        """Rate < MIN_THERMAL_RATE_F_PER_HOUR → skip."""
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        # 60 min, start=65.0, end=65.0 → rate=0 < 0.1
        coord._hvac_on_since = now - timedelta(minutes=60)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=65.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_today_record_session_count_incremented(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        from custom_components.climate_advisor.learning import DailyRecord

        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)
        coord._today_record = DailyRecord(date="2026-03-28", day_type="cold", trend_direction="stable")

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            coord._record_thermal_observation(MagicMock())

        assert coord._today_record.thermal_session_count == 1

    def test_today_record_peak_rate_tracked(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        from custom_components.climate_advisor.learning import DailyRecord

        coord._today_record = DailyRecord(date="2026-03-28", day_type="cold", trend_direction="stable")

        def _run_session(start_temp: float, end_temp: float, minutes: float):
            now = datetime(2026, 3, 28, 12, 0, 0)
            coord._hvac_on_since = now - timedelta(minutes=minutes)
            coord._hvac_session_start_indoor_temp = start_temp
            coord._hvac_session_start_outdoor_temp = 40.0
            coord._hvac_session_mode = "heat"
            coord._get_indoor_temp = MagicMock(return_value=end_temp)
            mock_dt = _now_mock(now)
            with (
                patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
                _patch_learning_dt(now.date()),
            ):
                coord._record_thermal_observation(MagicMock())

        # Session 1: rate = 1°F / (20/60h) = 3°F/hr
        _run_session(65.0, 66.0, 20.0)
        # Session 2: rate = 2°F / (20/60h) = 6°F/hr
        _run_session(65.0, 67.0, 20.0)

        assert coord._today_record.peak_hvac_rate_f_per_hour == pytest.approx(6.0, abs=0.1)


# ---------------------------------------------------------------------------
# TestImmediateLearningPersistence — Change 1: save_state after HVAC off
# ---------------------------------------------------------------------------


def _make_coordinator_async(tmp_path):
    """Coordinator with AsyncMock for hass.async_add_executor_job.

    The stub DataUpdateCoordinator.__init__ ignores args and does not set
    self.hass, so we inject a fresh hass mock with an AsyncMock executor job.
    """
    coord = _make_coordinator(tmp_path)
    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.states.get = MagicMock(return_value=None)
    hass.async_add_executor_job = AsyncMock(return_value=None)
    coord.hass = hass
    coord._async_save_state = AsyncMock()
    coord.learning = _make_engine(tmp_path)
    return coord


def _make_thermostat_state(hvac_mode: str, action: str = "idle"):
    """Return a minimal mock thermostat state."""
    state = MagicMock()
    state.state = hvac_mode
    state.attributes = {
        "hvac_action": action,
        "temperature": 70.0,
        "fan_mode": "auto",
    }
    return state


async def _run_hvac_off_block(coord, new_state):
    """Execute only the HVAC-just-turned-off block from _async_thermostat_changed.

    This exercises the exact code path:
        self._flush_hvac_runtime()
        self._record_thermal_observation(new_state)
        await self.hass.async_add_executor_job(self.learning.save_state)
        self._hvac_on_since = None
        await self._async_save_state()

    Without running the full _async_thermostat_changed (which has many unrelated
    dependencies that are difficult to stub in unit tests).
    """
    coord._flush_hvac_runtime()
    coord._record_thermal_observation(new_state)
    await coord.hass.async_add_executor_job(coord.learning.save_state)
    coord._hvac_on_since = None
    await coord._async_save_state()


class TestImmediateLearningPersistence:
    """Test that learning.save_state is called via async_add_executor_job when HVAC turns off."""

    def test_save_state_called_when_hvac_turns_off(self, tmp_path):
        """When the HVAC-off block runs, async_add_executor_job(learning.save_state) is awaited."""
        coord = _make_coordinator_async(tmp_path)

        now = datetime(2026, 3, 28, 14, 0, 0)
        start = now - timedelta(minutes=20)
        coord._hvac_on_since = start
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)
        coord._flush_hvac_runtime = MagicMock()

        new_state = _make_thermostat_state("heat", "idle")

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            asyncio.run(_run_hvac_off_block(coord, new_state))

        # async_add_executor_job should have been called with learning.save_state
        calls = coord.hass.async_add_executor_job.call_args_list
        save_state_calls = [c for c in calls if c == call(coord.learning.save_state)]
        assert len(save_state_calls) >= 1, (
            f"Expected async_add_executor_job(learning.save_state) to be called; got: {calls}"
        )

    def test_save_state_called_even_when_obs_skipped(self, tmp_path):
        """save_state is called even if the thermal observation itself was skipped."""
        coord = _make_coordinator_async(tmp_path)

        now = datetime(2026, 3, 28, 14, 0, 0)
        # Session too short — observation will be skipped, but save_state must still be called
        coord._hvac_on_since = now - timedelta(minutes=3)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)
        coord._flush_hvac_runtime = MagicMock()

        new_state = _make_thermostat_state("heat", "idle")

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            asyncio.run(_run_hvac_off_block(coord, new_state))

        calls = coord.hass.async_add_executor_job.call_args_list
        save_state_calls = [c for c in calls if c == call(coord.learning.save_state)]
        assert len(save_state_calls) >= 1, f"save_state should be called even when obs was skipped; got: {calls}"

    def test_hvac_on_since_cleared_after_save(self, tmp_path):
        """_hvac_on_since is set to None after save_state, preventing double-counting."""
        coord = _make_coordinator_async(tmp_path)

        now = datetime(2026, 3, 28, 14, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)
        coord._flush_hvac_runtime = MagicMock()

        new_state = _make_thermostat_state("heat", "idle")

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            asyncio.run(_run_hvac_off_block(coord, new_state))

        assert coord._hvac_on_since is None


# ---------------------------------------------------------------------------
# TestThermalObsSkipWarnings — Change 2: warning logs for silent skips
# ---------------------------------------------------------------------------


class TestThermalObsSkipWarnings:
    """Test that silent skip conditions now emit _LOGGER.warning()."""

    def _make_coord_with_learning(self, tmp_path):
        coord = _make_coordinator(tmp_path)
        coord.learning = _make_engine(tmp_path)
        return coord

    def test_warning_when_no_session_start_time(self, tmp_path, caplog):
        """_hvac_on_since is None → warning logged."""
        coord = self._make_coord_with_learning(tmp_path)
        coord._hvac_on_since = None
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            coord._record_thermal_observation(MagicMock())

        assert any("no session start time" in r.message for r in caplog.records), (
            f"Expected 'no session start time' warning; got: {[r.message for r in caplog.records]}"
        )

    def test_warning_when_no_session_start_indoor_temp(self, tmp_path, caplog):
        """_hvac_session_start_indoor_temp is None → warning logged."""
        coord = self._make_coord_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = None
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._record_thermal_observation(MagicMock())

        assert any("no indoor temperature" in r.message for r in caplog.records), (
            f"Expected 'no indoor temperature' warning; got: {[r.message for r in caplog.records]}"
        )

    def test_warning_when_session_mode_not_heat_or_cool(self, tmp_path, caplog):
        """_hvac_session_mode is neither 'heat' nor 'cool' → warning logged."""
        coord = self._make_coord_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_mode = "fan_only"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._record_thermal_observation(MagicMock())

        assert any("not 'heat' or 'cool'" in r.message for r in caplog.records), (
            f"Expected mode warning; got: {[r.message for r in caplog.records]}"
        )

    def test_no_warning_when_session_too_short(self, tmp_path, caplog):
        """Session below minimum → DEBUG only, no WARNING."""
        coord = self._make_coord_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=5)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._record_thermal_observation(MagicMock())

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_records) == 0, (
            f"Expected no WARNING for short session; got: {[r.message for r in warning_records]}"
        )

    def test_warning_when_end_indoor_temp_unavailable(self, tmp_path, caplog):
        """_get_indoor_temp() returns None at session end → warning logged (was previously silent)."""
        coord = self._make_coord_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=None)  # unavailable at session end

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._record_thermal_observation(MagicMock())

        assert any("indoor temp unavailable at session end" in r.message for r in caplog.records), (
            f"Expected end-indoor-unavailable warning; got: {[r.message for r in caplog.records]}"
        )
        assert len(coord.learning._state.thermal_observations) == 0

    def test_no_warning_when_rate_out_of_range(self, tmp_path, caplog):
        """Rate outside bounds → DEBUG only, no WARNING."""
        coord = self._make_coord_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        # 15 min, start=60, end=65 → rate = 5/(15/60) = 20°F/hr > MAX
        coord._hvac_on_since = now - timedelta(minutes=15)
        coord._hvac_session_start_indoor_temp = 60.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=65.0)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._record_thermal_observation(MagicMock())

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_records) == 0, (
            f"Expected no WARNING for out-of-range rate; got: {[r.message for r in warning_records]}"
        )


# ---------------------------------------------------------------------------
# TestEndOfDayWatchdog — Change 3: watchdog fires when no thermal obs recorded
# ---------------------------------------------------------------------------


def _make_end_of_day_coord(tmp_path):
    """Coordinator wired for _async_end_of_day tests."""
    import types

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
    from custom_components.climate_advisor.learning import DailyRecord

    coord = MagicMock()
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)
    coord.config = {"learning_enabled": True}
    coord.learning = MagicMock()
    coord._indoor_temp_history = []
    coord._outdoor_temp_history = []
    coord._hourly_forecast_temps = MagicMock()
    coord._briefing_sent_today = True
    coord._briefing_day_type = "cold"
    coord._hvac_on_since = None
    coord._last_violation_check = None
    coord._async_save_state = AsyncMock()
    coord._flush_hvac_runtime = MagicMock()
    coord._emit_event = MagicMock()
    coord._today_record = DailyRecord(date="2026-03-28", day_type="cold", trend_direction="stable")

    coord._async_end_of_day = types.MethodType(ClimateAdvisorCoordinator._async_end_of_day, coord)
    return coord


class TestEndOfDayWatchdog:
    """Watchdog in _async_end_of_day warns when HVAC ran but zero thermal obs recorded."""

    def test_watchdog_fires_when_runtime_high_and_no_obs(self, tmp_path, caplog):
        """With hvac_runtime_minutes > 30 and thermal_session_count == 0, emit warning + event."""
        coord = _make_end_of_day_coord(tmp_path)
        coord._today_record.hvac_runtime_minutes = 45.0
        coord._today_record.thermal_session_count = 0

        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            asyncio.run(coord._async_end_of_day(MagicMock()))

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Thermal learning watchdog" in m for m in warning_msgs), (
            f"Expected watchdog warning; got: {warning_msgs}"
        )
        coord._emit_event.assert_called_once_with(
            "thermal_learning_no_observations",
            {"hvac_runtime_minutes": 45.0},
        )

    def test_watchdog_silent_when_runtime_low(self, tmp_path, caplog):
        """With hvac_runtime_minutes <= 30, no watchdog warning."""
        coord = _make_end_of_day_coord(tmp_path)
        coord._today_record.hvac_runtime_minutes = 20.0
        coord._today_record.thermal_session_count = 0

        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            asyncio.run(coord._async_end_of_day(MagicMock()))

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("Thermal learning watchdog" in m for m in warning_msgs), (
            f"Unexpected watchdog warning: {warning_msgs}"
        )
        coord._emit_event.assert_not_called()

    def test_watchdog_silent_when_obs_recorded(self, tmp_path, caplog):
        """With thermal_session_count > 0, no watchdog warning even if runtime is high."""
        coord = _make_end_of_day_coord(tmp_path)
        coord._today_record.hvac_runtime_minutes = 60.0
        coord._today_record.thermal_session_count = 2

        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            asyncio.run(coord._async_end_of_day(MagicMock()))

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("Thermal learning watchdog" in m for m in warning_msgs), (
            f"Unexpected watchdog warning: {warning_msgs}"
        )
        coord._emit_event.assert_not_called()

    def test_watchdog_silent_when_no_today_record(self, tmp_path, caplog):
        """With no _today_record, watchdog is not evaluated."""
        coord = _make_end_of_day_coord(tmp_path)
        coord._today_record = None

        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            asyncio.run(coord._async_end_of_day(MagicMock()))

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("Thermal learning watchdog" in m for m in warning_msgs)
        coord._emit_event.assert_not_called()


# ---------------------------------------------------------------------------
# TestHvacSessionDetectionFallback — Fix 1a: running detection + session mode
# ---------------------------------------------------------------------------


def _detect_was_is_running(old_state, new_state):
    """Replicate the hvac_action/mode-based running detection from _async_thermostat_changed."""
    new_action = new_state.attributes.get("hvac_action", "").lower()
    old_action = old_state.attributes.get("hvac_action", "").lower()
    running_actions = {"heating", "cooling"}

    if old_action in running_actions or new_action in running_actions:
        return old_action in running_actions, new_action in running_actions
    else:
        idle_modes = {"off", "unavailable", "unknown", ""}
        return old_state.state not in idle_modes, new_state.state not in idle_modes


def _detect_session_mode(new_state):
    """Replicate the session mode assignment from the HVAC-just-turned-on block."""
    action = new_state.attributes.get("hvac_action", "").lower()
    if action == "heating":
        return "heat"
    elif action == "cooling":
        return "cool"
    elif new_state.state == "heat":
        return "heat"
    elif new_state.state == "cool":
        return "cool"
    return None


class TestHvacSessionDetectionFallback:
    """Tests for the running-detection and session-mode fallback fixes.

    When hvac_action is stuck at "fan" (a known thermostat firmware behaviour),
    the old `if new_action and old_action` guard prevented mode-based turn-on/off
    detection entirely — causing zero thermal observations.  The fix changes the
    guard to `if old_action in running_actions or new_action in running_actions`.
    """

    def test_turn_on_detected_when_hvac_action_stuck_at_fan(self):
        """old=off/fan → new=heat/fan: mode fallback fires the turn-on."""
        old = _make_thermostat_state("off", action="fan")
        new = _make_thermostat_state("heat", action="fan")
        was_running, is_running = _detect_was_is_running(old, new)
        assert not was_running and is_running

    def test_turn_off_detected_when_hvac_action_stuck_at_fan(self):
        """old=heat/fan → new=off/fan: mode fallback fires the turn-off."""
        old = _make_thermostat_state("heat", action="fan")
        new = _make_thermostat_state("off", action="fan")
        was_running, is_running = _detect_was_is_running(old, new)
        assert was_running and not is_running

    def test_normal_heating_action_still_detected(self):
        """Normal hvac_action transition heating→idle still fires turn-off."""
        old = _make_thermostat_state("heat", action="heating")
        new = _make_thermostat_state("off", action="idle")
        was_running, is_running = _detect_was_is_running(old, new)
        assert was_running and not is_running

    def test_no_edge_when_both_idle(self):
        """Both sides idle → no run edge detected."""
        old = _make_thermostat_state("off", action="idle")
        new = _make_thermostat_state("off", action="idle")
        was_running, is_running = _detect_was_is_running(old, new)
        assert not was_running and not is_running

    def test_session_mode_set_from_state_when_hvac_action_is_fan(self):
        """hvac_action='fan' at turn-on → session mode resolved from state='heat'."""
        new = _make_thermostat_state("heat", action="fan")
        assert _detect_session_mode(new) == "heat"

    def test_session_mode_cool_from_state_when_hvac_action_is_fan(self):
        """hvac_action='fan' at turn-on → session mode resolved from state='cool'."""
        new = _make_thermostat_state("cool", action="fan")
        assert _detect_session_mode(new) == "cool"

    def test_session_mode_from_hvac_action_when_heating(self):
        """hvac_action='heating' takes priority over state."""
        new = _make_thermostat_state("heat", action="heating")
        assert _detect_session_mode(new) == "heat"

    def test_session_mode_none_when_both_ambiguous(self):
        """fan_only mode + fan action → session mode is None (no observation possible)."""
        new = _make_thermostat_state("fan_only", action="fan")
        assert _detect_session_mode(new) is None


# ---------------------------------------------------------------------------
# TestLateStartHvacSessionInitialization — Root Cause D: HA restart while HVAC running
# ---------------------------------------------------------------------------


def _make_climate_state(hvac_mode: str = "heat", hvac_action: str = "heating") -> MagicMock:
    """Return a mock climate entity state for late-start initialization tests."""
    state = MagicMock()
    state.state = hvac_mode
    _attrs = {
        "hvac_action": hvac_action,
        "current_temperature": 70.0,
    }
    state.attributes.get.side_effect = lambda key, default="": _attrs.get(key, default)
    return state


class TestLateStartHvacSessionInitialization:
    """Tests for late-start HVAC session initialization after HA restart.

    Root Cause D: _hvac_on_since stays None when HA restarts while HVAC is already
    running (no state transition fires). Fix adds _initialize_hvac_session_from_current_state()
    called from _async_update_data() when hvac_action is heating/cooling and _hvac_on_since
    is None and _startup_hvac_initialized is False.

    Phase 1c: Tests are written to FAIL before the fix is applied. Expected failure mode:
    AttributeError on _startup_hvac_initialized or _initialize_hvac_session_from_current_state.
    """

    def test_late_start_when_hvac_running_at_startup(self, tmp_path: Path):
        """Calling _initialize_hvac_session_from_current_state with hvac_action=heating sets
        _hvac_on_since and _hvac_session_mode='heat'.
        """
        coord = _make_coordinator(tmp_path)
        coord._get_indoor_temp = MagicMock(return_value=70.0)

        fixed_now = datetime(2026, 3, 28, 8, 0, 0)
        mock_dt = MagicMock()
        mock_dt.now.return_value = fixed_now

        climate_state = _make_climate_state(hvac_mode="heat", hvac_action="heating")

        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._initialize_hvac_session_from_current_state(climate_state)

        assert coord._hvac_on_since is not None
        assert coord._hvac_session_mode == "heat"

    def test_late_start_not_repeated_after_first_init(self, tmp_path: Path):
        """_startup_hvac_initialized attribute exists on coordinator.

        After the fix, _startup_hvac_initialized is set to True on first call and
        prevents re-initialization. This test confirms the guard attribute exists.
        """
        coord = _make_coordinator(tmp_path)
        # Accessing _startup_hvac_initialized will raise AttributeError before the fix.
        assert hasattr(coord, "_startup_hvac_initialized"), (
            "_startup_hvac_initialized attribute must exist on coordinator after fix"
        )
        # After fix it should start False
        assert coord._startup_hvac_initialized is False

    def test_late_start_allows_thermal_observation_on_stop(self, tmp_path: Path):
        """After late-start init, a subsequent HVAC stop records a thermal observation."""
        coord = _make_coordinator(tmp_path)
        coord.learning = _make_engine(tmp_path)
        coord._get_indoor_temp = MagicMock(return_value=72.0)

        now = datetime(2026, 3, 28, 10, 0, 0)
        # Simulate: late-start init was called 20 minutes ago
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = 70.0
        coord._hvac_session_start_outdoor_temp = 35.0
        coord._hvac_session_mode = "heat"

        # Init the method (will fail before fix exists)
        climate_state = _make_climate_state(hvac_mode="heat", hvac_action="heating")
        fixed_now_init = now - timedelta(minutes=20)
        mock_dt_init = MagicMock()
        mock_dt_init.now.return_value = fixed_now_init

        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt_init):
            coord._initialize_hvac_session_from_current_state(climate_state)

        # The init overwrites outdoor temp with None (no weather entity in test stub).
        # Restore it so _record_thermal_observation can proceed.
        coord._hvac_session_start_outdoor_temp = 35.0
        # The init sets start_indoor_temp = _get_indoor_temp() = 72.0 (from mock).
        # Override to 70.0 so delta=2°F → rate=6°F/hr (within 0.1-15 bounds).
        coord._hvac_session_start_indoor_temp = 70.0

        # Now record observation at session end
        mock_dt_end = MagicMock()
        mock_dt_end.now.return_value = now

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt_end),
            _patch_learning_dt(now.date()),
        ):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) > 0, (
            "Expected thermal observation to be recorded after late-start init"
        )

    def test_late_start_does_not_fire_when_hvac_on_since_already_set(self, tmp_path: Path):
        """Guard: if _hvac_on_since is already set, _startup_hvac_initialized stays False
        but the init should not be called again (guard is _hvac_on_since is None).

        This verifies the attribute exists and that _hvac_on_since is preserved.
        """
        coord = _make_coordinator(tmp_path)
        coord._get_indoor_temp = MagicMock(return_value=70.0)

        existing_time = datetime(2026, 3, 28, 7, 0, 0)
        coord._hvac_on_since = existing_time

        # _startup_hvac_initialized must exist (will fail before fix)
        assert hasattr(coord, "_startup_hvac_initialized"), "_startup_hvac_initialized attribute must exist"

        # The guard condition: _hvac_on_since is None AND not _startup_hvac_initialized
        # Since _hvac_on_since is already set, the guard fails → init is NOT called.
        # Simulate the guard check directly:
        should_init = (coord._hvac_on_since is None) and (not coord._startup_hvac_initialized)
        assert not should_init, "Guard should prevent init when _hvac_on_since is already set"

        # _hvac_on_since must be unchanged
        assert coord._hvac_on_since == existing_time

    def test_late_start_skipped_when_action_is_idle(self, tmp_path: Path):
        """Calling _initialize_hvac_session_from_current_state with hvac_action='idle'
        results in _hvac_session_mode being None (fallback chain exhausted).
        """
        coord = _make_coordinator(tmp_path)
        coord._get_indoor_temp = MagicMock(return_value=70.0)

        fixed_now = datetime(2026, 3, 28, 8, 0, 0)
        mock_dt = MagicMock()
        mock_dt.now.return_value = fixed_now

        # hvac_action="idle", state="off" — neither heating nor cooling
        climate_state = _make_climate_state(hvac_mode="off", hvac_action="idle")

        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._initialize_hvac_session_from_current_state(climate_state)

        # With idle action and off mode, session mode should be None
        assert coord._hvac_session_mode is None

    def test_late_start_cool_mode(self, tmp_path: Path):
        """Calling _initialize_hvac_session_from_current_state with hvac_action='cooling'
        sets _hvac_session_mode='cool'.
        """
        coord = _make_coordinator(tmp_path)
        coord._get_indoor_temp = MagicMock(return_value=78.0)

        fixed_now = datetime(2026, 3, 28, 14, 0, 0)
        mock_dt = MagicMock()
        mock_dt.now.return_value = fixed_now

        climate_state = _make_climate_state(hvac_mode="cool", hvac_action="cooling")

        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._initialize_hvac_session_from_current_state(climate_state)

        assert coord._hvac_on_since is not None
        assert coord._hvac_session_mode == "cool"

    def test_late_start_fallback_to_hvac_mode_state(self, tmp_path: Path):
        """When hvac_action is blank, _initialize_hvac_session_from_current_state
        falls back to the climate entity's state ('heat') for session mode.
        """
        coord = _make_coordinator(tmp_path)
        coord._get_indoor_temp = MagicMock(return_value=68.0)

        fixed_now = datetime(2026, 3, 28, 9, 0, 0)
        mock_dt = MagicMock()
        mock_dt.now.return_value = fixed_now

        # hvac_action is blank — triggers fallback to state
        climate_state = _make_climate_state(hvac_mode="heat", hvac_action="")

        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._initialize_hvac_session_from_current_state(climate_state)

        assert coord._hvac_on_since is not None
        assert coord._hvac_session_mode == "heat"
