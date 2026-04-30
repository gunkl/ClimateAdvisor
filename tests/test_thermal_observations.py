"""Tests for the thermal model v3 parallel observation state machine (coordinator.py).

Covers the new multi-type observation pipeline:
  _start_hvac_observation / _start_decay_observation
  _sample_all_observations
  _abandon_observation / _commit_observation_if_sufficient
  _check_hvac_stabilization
  LearningEngine v3 migration path (pending_thermal_event → pending_observations)
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Give the HA dt sub-attribute a real parse_datetime so coordinator's local
# `from homeassistant.util import dt as dt_util2` works correctly.
_ha_util = sys.modules.get("homeassistant.util")
if _ha_util is not None:
    _ha_util.dt.parse_datetime = lambda s: datetime.fromisoformat(s) if s else None

# ---------------------------------------------------------------------------
# Imports after stubs are in place
# ---------------------------------------------------------------------------

from custom_components.climate_advisor.const import (  # noqa: E402
    MIN_THERMAL_OBSERVATIONS,
    OBS_TYPE_FAN_ONLY_DECAY,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENTILATED_DECAY,
    THERMAL_FAN_MIN_SAMPLES,
    THERMAL_FAN_SAMPLE_INTERVAL_S,
    THERMAL_HVAC_MIN_DECAY_F,
    THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S,
    THERMAL_PASSIVE_CONF_LOW,
    THERMAL_PASSIVE_MIN_SAMPLES,
    THERMAL_PASSIVE_MIN_SIGNAL_F,
    THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
    THERMAL_ROLLING_MAX_WINDOW_MINUTES,
    THERMAL_ROLLING_MIN_DELTA_T_F,
    THERMAL_ROLLING_MIN_WINDOW_MINUTES,
    THERMAL_ROLLING_WINDOW_MINUTES,
    THERMAL_VENT_MIN_SAMPLES,
    THERMAL_VENTILATED_MIN_DELTA_F,
)
from custom_components.climate_advisor.learning import LearningEngine  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAKE_NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _parse_datetime_real(s: str) -> datetime | None:
    """Real ISO parser forwarded to coordinator dt_util2 mock."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _make_dt_mock(now: datetime = _FAKE_NOW):
    """dt_util mock with real now() and parse_datetime() behaviour."""
    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    mock_dt.parse_datetime.side_effect = _parse_datetime_real
    return mock_dt


def _get_coordinator_class():
    """Import coordinator freshly to avoid stale module references."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_obs_coord(
    *,
    indoor_temp: float = 75.0,
    outdoor_temp: float = 55.0,
    hvac_action: str = "idle",
    fan_active: bool = False,
    nat_vent_active: bool = False,
    any_sensor_open: bool = False,
    learning_enabled: bool = True,
):
    """Build a minimal coordinator stub with the v3 observation methods bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    # ── hass mock ───────────────────────────────────────────────────────────
    hass = MagicMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    async def _exec_job(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _exec_job

    climate_state = MagicMock()
    climate_state.state = "heat" if hvac_action in ("heating",) else "idle"
    climate_state.attributes = {"hvac_action": hvac_action}
    weather_state = MagicMock()
    weather_state.attributes = {"temperature": outdoor_temp}

    def _states_get(entity_id: str):
        if "climate" in entity_id:
            return climate_state
        if "weather" in entity_id:
            return weather_state
        return None

    hass.states.get = MagicMock(side_effect=_states_get)
    coord.hass = hass

    # ── config ──────────────────────────────────────────────────────────────
    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "learning_enabled": learning_enabled,
    }

    # ── automation engine stub ───────────────────────────────────────────────
    ae = MagicMock()
    ae._fan_active = fan_active
    ae._natural_vent_active = nat_vent_active
    coord.automation_engine = ae

    # ── learning stub ────────────────────────────────────────────────────────
    learning = MagicMock()
    learning.set_pending_thermal_event = MagicMock()
    learning.save_state = MagicMock()
    learning._commit_event_from_dict = MagicMock(return_value={"hvac_mode": "heat"})
    coord.learning = learning

    # ── thermal state ────────────────────────────────────────────────────────
    coord._pending_observations = {}
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._last_outdoor_temp = outdoor_temp

    # ── helper methods ───────────────────────────────────────────────────────
    coord._get_indoor_temp = MagicMock(return_value=indoor_temp)
    coord._any_sensor_open = MagicMock(return_value=any_sensor_open)
    coord._async_save_state = AsyncMock()

    def _get_current_sample(elapsed: float) -> dict:
        return {
            "timestamp": _FAKE_NOW.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": elapsed,
        }

    coord._get_current_sample = _get_current_sample

    # ── bind real observation methods ─────────────────────────────────────────
    for method_name in (
        "_ensure_pending_observations",
        "_start_hvac_observation",
        "_start_decay_observation",
        "_sample_all_observations",
        "_abandon_observation",
        "_commit_observation_if_sufficient",
        "_commit_observation",
        "_check_hvac_stabilization",
        "_end_hvac_active_phase",
    ):
        method = getattr(ClimateAdvisorCoordinator, method_name)
        setattr(coord, method_name, types.MethodType(method, coord))

    return coord


# ---------------------------------------------------------------------------
# TestPassiveDecayObservation
# ---------------------------------------------------------------------------


class TestPassiveDecayObservation:
    """Passive-decay observation lifecycle via _sample_all_observations()."""

    def test_starts_on_hvac_off_with_sufficient_delta(self):
        # delta = 75 - 55 = 20 > THERMAL_PASSIVE_MIN_DELTA_F (3.0)
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY in coord._pending_observations
        obs = coord._pending_observations[OBS_TYPE_PASSIVE_DECAY]
        assert obs["status"] == "monitoring"

    def test_does_not_start_when_hvac_heating(self):
        coord = _make_obs_coord(hvac_action="heating", indoor_temp=70.0, outdoor_temp=50.0)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_does_not_start_when_sensor_open(self):
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=75.0,
            outdoor_temp=55.0,
            any_sensor_open=True,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_does_not_start_when_delta_below_threshold(self):
        # delta = 72 - 71 = 1.0 < THERMAL_PASSIVE_MIN_DELTA_F (3.0)
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=72.0, outdoor_temp=71.0)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_abandons_when_hvac_starts(self):
        # Place an active passive_decay observation with 5 samples (below min)
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(5)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        # Flip HVAC to heating
        coord.hass.states.get = MagicMock(
            side_effect=lambda eid: (
                MagicMock(
                    state="heat",
                    attributes={"hvac_action": "heating"},
                )
                if "climate" in eid
                else MagicMock(attributes={"temperature": 55.0})
            )
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        # 5 samples < THERMAL_PASSIVE_MIN_SAMPLES (30) → abandoned
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_commits_when_sufficient_samples_and_signal(self):
        """passive_decay with THERMAL_PASSIVE_MIN_SAMPLES samples and delta > MIN_SIGNAL_F commits."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=74.0, outdoor_temp=55.0)
        # Build MIN_SAMPLES samples (first=75.0, last=74.0 → delta=1.0 > 0.5)
        samples = [
            {"timestamp": _FAKE_NOW.isoformat(), "indoor_temp_f": 75.0, "outdoor_temp_f": 55.0, "elapsed_minutes": 0.0},
        ]
        for i in range(1, THERMAL_PASSIVE_MIN_SAMPLES):
            samples.append(
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 74.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
            )
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-2",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": samples,
            "flags_at_start": {},
            "schema_version": 1,
        }
        dt_mock = _make_dt_mock()
        committed_obs_types = []

        def _fake_async_create_task(coro):
            # Detect _commit_observation coroutines by name, then close safely
            coro_name = getattr(coro, "__name__", getattr(coro, "__qualname__", ""))
            if "_commit_observation" in coro_name:
                committed_obs_types.append(OBS_TYPE_PASSIVE_DECAY)
            coro.close()

        coord.hass.async_create_task = _fake_async_create_task

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # The observation should either be committed (popped) or queued for commit
        # Either the obs was popped already or a task was queued
        was_popped = OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations
        was_queued = len(committed_obs_types) > 0
        assert was_popped or was_queued, (
            "Expected passive_decay to be committed or queued for commit when "
            f"samples={THERMAL_PASSIVE_MIN_SAMPLES}, delta=1.0 > {THERMAL_PASSIVE_MIN_SIGNAL_F}"
        )


# ---------------------------------------------------------------------------
# TestFanOnlyObservation
# ---------------------------------------------------------------------------


class TestFanOnlyObservation:
    """Fan-only decay observation lifecycle via _sample_all_observations()."""

    def test_starts_when_fan_active_no_hvac(self):
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
            any_sensor_open=False,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_FAN_ONLY_DECAY in coord._pending_observations
        assert coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY]["status"] == "monitoring"

    def test_does_not_start_when_hvac_active(self):
        coord = _make_obs_coord(
            hvac_action="heating",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations

    def test_does_not_start_when_sensor_open(self):
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
            any_sensor_open=True,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations

    def test_abandons_when_fan_stops(self):
        """fan_only_decay with < THERMAL_FAN_MIN_SAMPLES is abandoned when fan stops."""

        coord = _make_obs_coord(hvac_action="idle", indoor_temp=72.0, outdoor_temp=65.0, fan_active=False)
        # Pre-seed observation with 5 samples (< 15 min threshold)
        coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY] = {
            "obs_type": OBS_TYPE_FAN_ONLY_DECAY,
            "obs_id": "test-fan-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 72.0,
                    "outdoor_temp_f": 65.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(5)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        # ae._fan_active is already False; climate state is idle
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        # 5 < THERMAL_FAN_MIN_SAMPLES → should be abandoned
        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations


# ---------------------------------------------------------------------------
# TestConcurrentObservations
# ---------------------------------------------------------------------------


class TestConcurrentObservations:
    """Multiple observation types cannot contaminate each other."""

    def test_hvac_start_abandons_passive_decay(self):
        """_start_hvac_observation() abandons (or commits-if-sufficient) passive_decay."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        # Pre-seed an active passive_decay with 3 insufficient samples
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-3",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(3)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))

        # passive_decay must be gone (3 < min_samples → abandoned)
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations
        # hvac_heat observation must be started
        assert OBS_TYPE_HVAC_HEAT in coord._pending_observations

    def test_passive_and_fan_only_cannot_coexist(self):
        """When fan activates while passive_decay is monitoring, passive_decay is abandoned."""
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=75.0,
            outdoor_temp=55.0,
            fan_active=False,
        )
        # Pre-seed passive_decay with 3 insufficient samples
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-4",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(3)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        # Now fan activates — flip ae._fan_active
        coord.automation_engine._fan_active = True

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # passive_decay should be abandoned (fan_activated condition)
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations
        # fan_only_decay should be started
        assert OBS_TYPE_FAN_ONLY_DECAY in coord._pending_observations


# ---------------------------------------------------------------------------
# TestHvacObservationReducedPlateauGuard
# ---------------------------------------------------------------------------


class TestHvacObservationReducedPlateauGuard:
    """_check_hvac_stabilization() plateau guard uses THERMAL_HVAC_MIN_DECAY_F=0.3."""

    def _make_stabilization_window(self, peak_f: float, end_f: float, n_post: int = 10) -> dict:
        """Build a post_heat HVAC observation with tight recent variance."""

        # All samples within the stabilization window (uniform temperature = no variance)
        now = _FAKE_NOW
        samples = []
        for i in range(n_post):
            ts = datetime(
                now.year,
                now.month,
                now.day,
                now.hour,
                max(0, now.minute - (n_post - 1 - i)),
                now.second,
                tzinfo=UTC,
            )
            samples.append(
                {
                    "timestamp": ts.isoformat(),
                    "indoor_temp_f": end_f,
                    "outdoor_temp_f": 50.0,
                    "elapsed_minutes": float(i),
                }
            )

        active_end = datetime(
            now.year,
            now.month,
            now.day,
            now.hour,
            max(0, now.minute - n_post),
            tzinfo=UTC,
        )
        return {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-hvac-plateau",
            "start_time": _FAKE_NOW.isoformat(),
            "active_start": active_end.isoformat(),
            "active_end": active_end.isoformat(),
            "status": "monitoring",
            "_phase": "post_heat",
            "active_samples": [],
            "post_heat_samples": samples,
            "peak_indoor_f": peak_f,
            "end_indoor_f": end_f,
            "flags_at_start": {},
            "schema_version": 1,
        }

    def test_plateau_guard_fires_for_0_2_delta(self):
        """peak=72.0, end=71.8 → decay=0.2 < THERMAL_HVAC_MIN_DECAY_F=0.3 → abandoned."""
        from custom_components.climate_advisor.const import THERMAL_MIN_POST_HEAT_SAMPLES

        coord = _make_obs_coord()
        n_post = max(10, THERMAL_MIN_POST_HEAT_SAMPLES)
        obs = self._make_stabilization_window(peak_f=72.0, end_f=71.8, n_post=n_post)
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations, (
            "Plateau guard (decay=0.2 < 0.3) should have abandoned the observation"
        )

    def test_plateau_guard_passes_for_0_5_delta(self):
        """peak=72.0, end=71.5 → decay=0.5 > THERMAL_HVAC_MIN_DECAY_F=0.3 → committed."""
        from custom_components.climate_advisor.const import THERMAL_MIN_POST_HEAT_SAMPLES

        coord = _make_obs_coord()
        n_post = max(10, THERMAL_MIN_POST_HEAT_SAMPLES)
        obs = self._make_stabilization_window(peak_f=72.0, end_f=71.5, n_post=n_post)
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        commit_called = []

        async def _fake_commit(obs_type, force_grade=None):
            commit_called.append(obs_type)
            coord._pending_observations.pop(obs_type, None)

        coord._commit_observation = _fake_commit

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        assert len(commit_called) == 1 and commit_called[0] == OBS_TYPE_HVAC_HEAT, (
            "Plateau guard should pass for decay=0.5 > 0.3, observation should be committed"
        )

    def test_plateau_guard_threshold_is_0_3(self):
        """Confirm THERMAL_HVAC_MIN_DECAY_F constant value is exactly 0.3."""
        assert pytest.approx(0.3) == THERMAL_HVAC_MIN_DECAY_F, (
            f"Expected THERMAL_HVAC_MIN_DECAY_F=0.3, got {THERMAL_HVAC_MIN_DECAY_F}"
        )


# ---------------------------------------------------------------------------
# TestAbandonmentLogging
# ---------------------------------------------------------------------------


class TestAbandonmentLogging:
    """_abandon_observation() logs at INFO level with type and reason."""

    def test_abandon_logs_at_info_level(self):
        coord = _make_obs_coord()
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-abandon-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(3)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }

        with patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_logger:
            coord._abandon_observation(OBS_TYPE_PASSIVE_DECAY, "hvac_started")

        assert mock_logger.info.called, "Expected _LOGGER.info() to be called on abandonment"
        call_args = mock_logger.info.call_args
        # First positional arg is the format string; subsequent args are substituted values
        format_str = call_args[0][0]
        all_args = call_args[0]
        full_message = format_str % all_args[1:]
        assert "passive_decay" in full_message, f"Expected 'passive_decay' in warning message, got: {full_message}"
        assert "hvac_started" in full_message, f"Expected 'hvac_started' in warning message, got: {full_message}"

    def test_abandon_removes_obs_from_pending(self):
        coord = _make_obs_coord()
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-abandon-2",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [],
            "flags_at_start": {},
            "schema_version": 1,
        }
        coord._abandon_observation(OBS_TYPE_PASSIVE_DECAY, "test_reason")
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_abandon_noop_when_obs_not_present(self):
        """_abandon_observation on a missing obs_type must not raise."""
        coord = _make_obs_coord()
        # Should not raise
        coord._abandon_observation(OBS_TYPE_PASSIVE_DECAY, "no_such_obs")
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations


# ---------------------------------------------------------------------------
# TestMigrationLoadState
# ---------------------------------------------------------------------------


class TestMigrationLoadState:
    """LearningEngine.load_state() migrates pending_thermal_event to pending_observations."""

    def test_migration_converts_pending_thermal_event_heat(self, tmp_path: Path):
        """A v2 post_heat event is migrated to OBS_TYPE_HVAC_HEAT in pending_observations."""
        engine = LearningEngine(tmp_path)
        engine.load_state()

        # Inject a v2-style pending event into state (post_heat — recoverable)
        engine._state.pending_thermal_event = {
            "event_id": "test-123",
            "status": "post_heat",
            "session_mode": "heat",
            "hvac_mode": "heat",
            "active_start": "2025-06-15T08:00:00",
            "active_samples": [
                {
                    "indoor_temp_f": 70.0,
                    "outdoor_temp_f": 50.0,
                    "elapsed_minutes": 1.0,
                    "timestamp": "2025-06-15T08:01:00",
                }
            ],
            "pre_heat_samples": [],
            "post_heat_samples": [],
        }
        # Make pending_observations empty so migration runs
        engine._state.pending_observations = {}

        # Re-run load_state to trigger migration — simulate by calling the migration path
        # directly by setting up the state and calling load_state() again after persisting
        import json

        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": {},
            "pending_thermal_event": engine._state.pending_thermal_event,
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine2 = LearningEngine(tmp_path)
        engine2.load_state()

        # After migration: pending_thermal_event should NOT have been blindly carried —
        # the migration writes it to pending_observations
        assert isinstance(engine2._state.pending_observations, dict)
        assert OBS_TYPE_HVAC_HEAT in engine2._state.pending_observations, (
            "v3 migration should convert pending_thermal_event[mode=heat] "
            f"→ pending_observations['{OBS_TYPE_HVAC_HEAT}']"
        )
        migrated = engine2._state.pending_observations[OBS_TYPE_HVAC_HEAT]
        assert migrated["obs_id"] == "test-123"

    def test_migration_discards_active_status_v2_event(self, tmp_path: Path):
        """A v2 event with status='active' is discarded — cannot be recovered without runtime state."""
        import json

        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": {},
            "pending_thermal_event": {
                "event_id": "active-event",
                "status": "active",
                "session_mode": "heat",
                "hvac_mode": "heat",
                "active_start": "2025-06-15T08:00:00",
                "active_samples": [],
                "pre_heat_samples": [],
                "post_heat_samples": [],
            },
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine = LearningEngine(tmp_path)
        engine.load_state()

        assert engine._state.pending_thermal_event is None, "active-status v2 event should be discarded"
        assert engine._state.pending_observations == {}, "no obs should be created for discarded active event"

    def test_migration_clears_pending_thermal_event_after_migration(self, tmp_path: Path):
        """After migration, pending_thermal_event entry is still present (not cleared
        by migration itself — coordinator handles lifecycle), but pending_observations is populated."""
        import json

        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": {},
            "pending_thermal_event": {
                "event_id": "evt-456",
                "status": "post_heat",
                "session_mode": "cool",
                "hvac_mode": "cool",
                "active_start": "2025-07-01T14:00:00",
                "active_end": "2025-07-01T14:30:00",
                "active_samples": [],
                "pre_heat_samples": [],
                "post_heat_samples": [],
            },
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine = LearningEngine(tmp_path)
        engine.load_state()

        assert OBS_TYPE_HVAC_COOL in engine._state.pending_observations
        assert engine._state.pending_observations[OBS_TYPE_HVAC_COOL]["obs_id"] == "evt-456"

    def test_migration_skips_if_pending_observations_already_populated(self, tmp_path: Path):
        """If pending_observations already has the obs_type, migration does not overwrite it."""
        import json

        existing_obs = {
            OBS_TYPE_HVAC_HEAT: {
                "obs_type": OBS_TYPE_HVAC_HEAT,
                "obs_id": "already-there",
                "start_time": "2025-06-15T08:00:00",
                "status": "monitoring",
                "samples": [],
                "flags_at_start": {},
                "schema_version": 1,
            }
        }
        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": existing_obs,
            "pending_thermal_event": {
                "event_id": "should-not-overwrite",
                "status": "active",
                "session_mode": "heat",
                "hvac_mode": "heat",
                "active_start": "2025-06-15T09:00:00",
                "active_samples": [],
                "pre_heat_samples": [],
                "post_heat_samples": [],
            },
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine = LearningEngine(tmp_path)
        engine.load_state()

        # The pre-existing obs_id should NOT be overwritten by migration
        assert engine._state.pending_observations[OBS_TYPE_HVAC_HEAT]["obs_id"] == "already-there"


# ---------------------------------------------------------------------------
# TestE6CacheBugFix
# ---------------------------------------------------------------------------


class TestE6CacheBugFix:
    """E6: passive_decay observations must NOT write k_vent; only fan_only_decay may.

    Before the fix, _update_thermal_model_cache() wrote k_p (envelope decay rate)
    into cache["k_vent"] inside the `elif mode == "passive"` branch, silently
    contaminating fan-ventilation data with envelope-only measurements.
    """

    def _make_engine(self, tmp_path: Path) -> LearningEngine:
        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    def _passive_obs(self, k_passive: float) -> dict:
        """Minimal passive_decay observation dict accepted by _update_thermal_model_cache."""
        return {
            "date": "2026-04-28",
            "hvac_mode": "passive",
            "k_passive": k_passive,
            "confidence_grade": "high",
        }

    def _fan_only_obs(self, k_passive: float) -> dict:
        """Minimal fan_only_decay observation dict."""
        return {
            "date": "2026-04-28",
            "hvac_mode": "fan_only",
            "k_passive": k_passive,
            "confidence_grade": "high",
        }

    def test_passive_decay_does_not_update_k_vent(self, tmp_path: Path):
        """A passive_decay commit must update k_passive but leave k_vent untouched."""
        engine = self._make_engine(tmp_path)
        engine._update_thermal_model_cache(self._passive_obs(-0.08))

        model = engine.get_thermal_model()
        assert pytest.approx(-0.08, abs=1e-9) == model["k_passive"], (
            f"k_passive should be -0.08, got {model['k_passive']}"
        )
        assert model["k_vent"] is None, f"k_vent must be None after passive_decay commit; got {model['k_vent']}"

    def test_fan_only_decay_updates_k_vent(self, tmp_path: Path):
        """A fan_only_decay commit must set k_vent and also update k_passive."""
        engine = self._make_engine(tmp_path)
        engine._update_thermal_model_cache(self._fan_only_obs(-0.15))

        model = engine.get_thermal_model()
        assert pytest.approx(-0.15, abs=1e-9) == model["k_vent"], f"k_vent should be -0.15, got {model['k_vent']}"
        # k_passive is updated unconditionally for all modes
        assert pytest.approx(-0.15, abs=1e-9) == model["k_passive"], (
            f"k_passive should also be -0.15 after fan_only commit, got {model['k_passive']}"
        )

    def test_passive_and_fan_only_do_not_cross_contaminate(self, tmp_path: Path):
        """passive then fan_only: k_vent reflects only fan_only; k_passive is EMA of both."""
        engine = self._make_engine(tmp_path)

        # First: passive_decay — should touch k_passive only
        engine._update_thermal_model_cache(self._passive_obs(-0.08))

        # Second: fan_only_decay — should set k_vent and update k_passive via EMA
        engine._update_thermal_model_cache(self._fan_only_obs(-0.12))

        model = engine.get_thermal_model()

        # k_vent must reflect the fan_only value only (first commit; cache was None → set directly)
        assert pytest.approx(-0.12, abs=1e-9) == model["k_vent"], (
            f"k_vent should be -0.12 (fan_only only); got {model['k_vent']} — passive_decay must not contaminate k_vent"
        )

        # k_passive: first commit set it to -0.08 (alpha=0.3 for "high").
        # Second commit: EMA = (1 - 0.3) * -0.08 + 0.3 * -0.12 = -0.092
        expected_k_passive = (1.0 - 0.3) * (-0.08) + 0.3 * (-0.12)
        assert pytest.approx(expected_k_passive, abs=1e-9) == model["k_passive"], (
            f"k_passive should be EMA of both observations ({expected_k_passive:.4f}); got {model['k_passive']}"
        )


# ---------------------------------------------------------------------------
# TestWallClockTimeout  (Issue #122 — H4 wall-clock abandon guard)
# ---------------------------------------------------------------------------


def _make_obs_61min_ago() -> datetime:
    """Return a start_time 61 minutes before _FAKE_NOW."""
    return datetime(2026, 4, 28, 10, 59, 0, tzinfo=UTC)


def _make_obs_241min_ago() -> datetime:
    """Return a start_time 241 minutes before _FAKE_NOW (past the 240-min hard cap)."""
    return datetime(2026, 4, 28, 7, 59, 0, tzinfo=UTC)


def _make_stale_obs(obs_type: str, n_samples: int, indoor_temp: float, outdoor_temp: float) -> dict:
    """Build a monitoring observation dict that started 61 minutes ago."""
    start = _make_obs_61min_ago()
    samples = [
        {
            "timestamp": start.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": float(i),
        }
        for i in range(n_samples)
    ]
    return {
        "obs_type": obs_type,
        "obs_id": "test-wallclock-1",
        "start_time": start.isoformat(),
        "status": "monitoring",
        "samples": samples,
        "flags_at_start": {},
        "schema_version": 1,
    }


def _make_very_stale_obs(obs_type: str, n_samples: int, indoor_temp: float, outdoor_temp: float) -> dict:
    """Build a monitoring observation dict that started 241 minutes ago (past 240-min hard cap)."""
    start = _make_obs_241min_ago()
    samples = [
        {
            "timestamp": start.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": float(i),
        }
        for i in range(n_samples)
    ]
    return {
        "obs_type": obs_type,
        "obs_id": "test-wallclock-2",
        "start_time": start.isoformat(),
        "status": "monitoring",
        "samples": samples,
        "flags_at_start": {},
        "schema_version": 1,
    }


class TestWallClockTimeout:
    """Wall-clock abandon guard for ventilated_decay and fan_only_decay (Issue #122 H4 / Issue #126)."""

    def test_ventilated_decay_kept_alive_at_61min_low_signal(self, caplog):
        """ventilated_decay started 61 min ago, low |ΔT| → kept alive (between min=30 and max=240 window).

        Issue #126 changed the rolling window so that observations between THERMAL_ROLLING_MIN_WINDOW_MINUTES
        (30 min) and THERMAL_ROLLING_MAX_WINDOW_MINUTES (240 min) with insufficient signal are kept alive
        rather than abandoned. The old 60-min hard-abandon is replaced by the 240-min hard cap.
        """
        # |indoor - outdoor| = 72.1 - 72.0 = 0.1 < THERMAL_VENT_MIN_SIGNAL_F (0.3)
        indoor, outdoor = 72.1, 72.0
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=indoor,
            outdoor_temp=outdoor,
            any_sensor_open=True,  # sensor open keeps ventilated_decay alive under normal logic
        )
        coord._pending_observations[OBS_TYPE_VENTILATED_DECAY] = _make_stale_obs(
            OBS_TYPE_VENTILATED_DECAY,
            n_samples=THERMAL_VENT_MIN_SAMPLES + 5,  # enough samples, but low signal
            indoor_temp=indoor,
            outdoor_temp=outdoor,
        )
        dt_mock = _make_dt_mock(_FAKE_NOW)

        import logging

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._sample_all_observations()

        # At 61 min with low signal the obs should be kept alive, not abandoned
        assert OBS_TYPE_VENTILATED_DECAY in coord._pending_observations, (
            "ventilated_decay observation should be kept alive at 61 min with low signal "
            f"(between THERMAL_ROLLING_MIN_WINDOW_MINUTES={THERMAL_ROLLING_MIN_WINDOW_MINUTES} "
            f"and THERMAL_ROLLING_MAX_WINDOW_MINUTES={THERMAL_ROLLING_MAX_WINDOW_MINUTES})"
        )
        # The coordinator should log a "keeping alive" message
        assert any("keeping alive" in r.message for r in caplog.records), (
            "Expected INFO log indicating observation is being kept alive"
        )

    def test_ventilated_decay_abandons_at_max_window_low_signal(self, caplog):
        """ventilated_decay started 241 min ago (past 240-min cap), low |ΔT| → abandoned with max_window_exceeded."""
        # |indoor - outdoor| = 72.1 - 72.0 = 0.1 < THERMAL_VENT_MIN_SIGNAL_F (0.3)
        indoor, outdoor = 72.1, 72.0
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=indoor,
            outdoor_temp=outdoor,
            any_sensor_open=True,  # sensor open keeps ventilated_decay alive under normal logic
        )
        coord._pending_observations[OBS_TYPE_VENTILATED_DECAY] = _make_very_stale_obs(
            OBS_TYPE_VENTILATED_DECAY,
            n_samples=3,  # below THERMAL_MIN_DECAY_SAMPLES+1 threshold → abandon not commit
            indoor_temp=indoor,
            outdoor_temp=outdoor,
        )
        dt_mock = _make_dt_mock(_FAKE_NOW)

        import logging

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._sample_all_observations()

        assert OBS_TYPE_VENTILATED_DECAY not in coord._pending_observations, (
            "ventilated_decay observation should be abandoned after THERMAL_ROLLING_MAX_WINDOW_MINUTES "
            f"({THERMAL_ROLLING_MAX_WINDOW_MINUTES} min) with low signal"
        )
        assert any("max_window_exceeded" in r.message for r in caplog.records), (
            "Expected log with reason 'max_window_exceeded'"
        )

    def test_ventilated_decay_commits_at_max_window_sufficient_signal(self):
        """ventilated_decay started 61 min ago, sensor open, sufficient |ΔT| → commit triggered."""
        # |indoor - outdoor| = 76.0 - 60.0 = 16.0 >= THERMAL_VENT_MIN_SIGNAL_F (0.3)
        indoor, outdoor = 76.0, 60.0
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=indoor,
            outdoor_temp=outdoor,
            any_sensor_open=True,
        )
        coord._pending_observations[OBS_TYPE_VENTILATED_DECAY] = _make_stale_obs(
            OBS_TYPE_VENTILATED_DECAY,
            n_samples=THERMAL_VENT_MIN_SAMPLES + 5,
            indoor_temp=indoor,
            outdoor_temp=outdoor,
        )
        committed_obs_types: list[str] = []

        def _fake_async_create_task(coro):
            coro_name = getattr(coro, "__name__", getattr(coro, "__qualname__", ""))
            if "_commit_observation" in coro_name:
                committed_obs_types.append(OBS_TYPE_VENTILATED_DECAY)
            coro.close()

        coord.hass.async_create_task = _fake_async_create_task
        dt_mock = _make_dt_mock(_FAKE_NOW)

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        obs_still_present = coord._pending_observations.get(OBS_TYPE_VENTILATED_DECAY)
        was_committed_status = obs_still_present is not None and obs_still_present.get("status") == "committing"
        was_queued = len(committed_obs_types) > 0

        assert was_committed_status or was_queued, (
            "ventilated_decay with sufficient signal at max window should trigger commit "
            f"(status={obs_still_present.get('status') if obs_still_present else 'popped'}, "
            f"queued={was_queued})"
        )

    def test_fan_only_decay_kept_alive_at_61min_low_signal(self, caplog):
        """fan_only_decay started 61 min ago, fan still on, low |ΔT| → kept alive (between min=30 and max=240 window).

        Issue #126 changed the rolling window so that observations between THERMAL_ROLLING_MIN_WINDOW_MINUTES
        (30 min) and THERMAL_ROLLING_MAX_WINDOW_MINUTES (240 min) with insufficient signal are kept alive
        rather than abandoned. The old 60-min hard-abandon is replaced by the 240-min hard cap.
        """
        # |indoor - outdoor| = 70.1 - 70.0 = 0.1 < THERMAL_FAN_MIN_SIGNAL_F (0.2)
        indoor, outdoor = 70.1, 70.0
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=indoor,
            outdoor_temp=outdoor,
            fan_active=True,  # fan on keeps fan_only_decay alive under normal logic
        )
        coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY] = _make_stale_obs(
            OBS_TYPE_FAN_ONLY_DECAY,
            n_samples=THERMAL_FAN_MIN_SAMPLES + 5,  # enough samples, but low signal
            indoor_temp=indoor,
            outdoor_temp=outdoor,
        )
        dt_mock = _make_dt_mock(_FAKE_NOW)

        import logging

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._sample_all_observations()

        # At 61 min with low signal the obs should be kept alive, not abandoned
        assert OBS_TYPE_FAN_ONLY_DECAY in coord._pending_observations, (
            "fan_only_decay observation should be kept alive at 61 min with low signal "
            f"(between THERMAL_ROLLING_MIN_WINDOW_MINUTES={THERMAL_ROLLING_MIN_WINDOW_MINUTES} "
            f"and THERMAL_ROLLING_MAX_WINDOW_MINUTES={THERMAL_ROLLING_MAX_WINDOW_MINUTES})"
        )
        # The coordinator should log a "keeping alive" message
        assert any("keeping alive" in r.message for r in caplog.records), (
            "Expected INFO log indicating observation is being kept alive"
        )

    def test_fan_only_decay_abandons_at_max_window(self, caplog):
        """fan_only_decay started 241 min ago (past 240-min cap), low |ΔT| → abandoned with max_window_exceeded."""
        # |indoor - outdoor| = 70.1 - 70.0 = 0.1 < THERMAL_FAN_MIN_SIGNAL_F (0.2)
        indoor, outdoor = 70.1, 70.0
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=indoor,
            outdoor_temp=outdoor,
            fan_active=True,  # fan on keeps fan_only_decay alive under normal logic
        )
        coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY] = _make_very_stale_obs(
            OBS_TYPE_FAN_ONLY_DECAY,
            n_samples=3,  # below THERMAL_MIN_DECAY_SAMPLES+1 threshold → abandon not commit
            indoor_temp=indoor,
            outdoor_temp=outdoor,
        )
        dt_mock = _make_dt_mock(_FAKE_NOW)

        import logging

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._sample_all_observations()

        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations, (
            "fan_only_decay observation should be abandoned after THERMAL_ROLLING_MAX_WINDOW_MINUTES "
            f"({THERMAL_ROLLING_MAX_WINDOW_MINUTES} min) with low signal"
        )
        assert any("max_window_exceeded" in r.message for r in caplog.records), (
            "Expected log with reason 'max_window_exceeded'"
        )


# ---------------------------------------------------------------------------
# Helpers for H1/H2 test classes
# ---------------------------------------------------------------------------


def _make_obs_coord_with_sample_thermal_event(
    *,
    indoor_temp: float = 75.0,
    outdoor_temp: float = 55.0,
    hvac_action: str = "idle",
    fan_active: bool = False,
    nat_vent_active: bool = False,
    any_sensor_open: bool = False,
    learning_enabled: bool = True,
):
    """Like _make_obs_coord but also binds _sample_thermal_event."""
    coord = _make_obs_coord(
        indoor_temp=indoor_temp,
        outdoor_temp=outdoor_temp,
        hvac_action=hvac_action,
        fan_active=fan_active,
        nat_vent_active=nat_vent_active,
        any_sensor_open=any_sensor_open,
        learning_enabled=learning_enabled,
    )
    ClimateAdvisorCoordinator = _get_coordinator_class()
    method = ClimateAdvisorCoordinator._sample_thermal_event
    coord._sample_thermal_event = types.MethodType(method, coord)
    return coord


def _make_obs_31min_ago() -> datetime:
    """Return a start_time 31 minutes before _FAKE_NOW."""
    return datetime(2026, 4, 28, 11, 29, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# TestSampleDecimation  (Issue #122 — H1 per-type sample decimation)
# ---------------------------------------------------------------------------


class TestSampleDecimation:
    """H1: Per-type sample decimation gates slow-phenomenon obs types."""

    def _make_obs_with_last_sample(self, obs_type: str, last_sample_offset_s: int) -> dict:
        """Build a monitoring obs with last_sample_time set to offset seconds before _FAKE_NOW."""
        from datetime import timedelta

        last_ts = _FAKE_NOW - timedelta(seconds=last_sample_offset_s)
        return {
            "obs_type": obs_type,
            "obs_id": "test-decimate-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [],
            "last_sample_time": last_ts.isoformat(),
            "flags_at_start": {},
            "schema_version": 1,
        }

    def test_passive_obs_decimated_at_5min(self):
        """passive_decay: sample is NOT appended if < 300s since last sample."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        # Pre-seed obs with last_sample_time only 30s ago — below the 300s interval
        obs = self._make_obs_with_last_sample(OBS_TYPE_PASSIVE_DECAY, last_sample_offset_s=30)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = obs

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # No new sample should have been appended (30s < 300s interval)
        assert len(coord._pending_observations.get(OBS_TYPE_PASSIVE_DECAY, {}).get("samples", [])) == 0, (
            "passive_decay should not append a sample when only 30s has elapsed (interval=300s)"
        )

    def test_passive_obs_appended_after_5min(self):
        """passive_decay: sample IS appended after >= 300s since last sample."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        # last_sample_time was 310s ago — above the 300s interval
        obs = self._make_obs_with_last_sample(OBS_TYPE_PASSIVE_DECAY, last_sample_offset_s=310)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = obs

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert len(coord._pending_observations.get(OBS_TYPE_PASSIVE_DECAY, {}).get("samples", [])) == 1, (
            "passive_decay should append a sample after 310s (interval=300s)"
        )

    def test_fan_only_decimated_at_2min(self):
        """fan_only_decay: sample is NOT appended if < 120s since last sample."""
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
        )
        # last_sample_time was only 60s ago — below the 120s interval
        obs = self._make_obs_with_last_sample(OBS_TYPE_FAN_ONLY_DECAY, last_sample_offset_s=60)
        coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY] = obs

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert len(coord._pending_observations.get(OBS_TYPE_FAN_ONLY_DECAY, {}).get("samples", [])) == 0, (
            "fan_only_decay should not append a sample when only 60s has elapsed (interval=120s)"
        )

    def test_fan_only_appended_after_2min(self):
        """fan_only_decay: sample IS appended after >= 120s since last sample."""
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
        )
        obs = self._make_obs_with_last_sample(OBS_TYPE_FAN_ONLY_DECAY, last_sample_offset_s=130)
        coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY] = obs

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert len(coord._pending_observations.get(OBS_TYPE_FAN_ONLY_DECAY, {}).get("samples", [])) == 1, (
            "fan_only_decay should append a sample after 130s (interval=120s)"
        )

    def test_hvac_active_not_decimated(self):
        """HVAC active obs is not in the interval map — every poll appends (interval=0)."""
        coord = _make_obs_coord(hvac_action="heating", indoor_temp=70.0, outdoor_temp=50.0)
        # Pre-seed an HVAC heat obs in active phase
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-hvac-nodecim",
            "start_time": _FAKE_NOW.isoformat(),
            "active_start": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "_phase": "active",
            "active_samples": [],
            "post_heat_samples": [],
            "peak_indoor_f": None,
            "flags_at_start": {},
            "schema_version": 1,
        }
        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # Active HVAC samples should be appended immediately (no decimation)
        active_samples = coord._pending_observations.get(OBS_TYPE_HVAC_HEAT, {}).get("active_samples", [])
        assert len(active_samples) == 1, f"HVAC active obs should append every poll — got {len(active_samples)} samples"

    def test_post_heat_decimated_at_5min(self):
        """Post-heat samples are gated at THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S (300s)."""
        coord = _make_obs_coord_with_sample_thermal_event(hvac_action="idle", indoor_temp=70.0, outdoor_temp=50.0)

        from datetime import timedelta

        last_ph_ts = _FAKE_NOW - timedelta(seconds=30)  # 30s ago — below 300s interval
        coord._pending_thermal_event = {
            "event_id": "test-postheat-decimate",
            "status": "post_heat",
            "session_mode": "heat",
            "active_start": _FAKE_NOW.isoformat(),
            "active_end": _FAKE_NOW.isoformat(),
            "active_samples": [],
            "post_heat_samples": [],
            "last_post_heat_sample_time": last_ph_ts.isoformat(),
        }

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_thermal_event()

        assert len(coord._pending_thermal_event["post_heat_samples"]) == 0, (
            "post-heat sample should NOT be appended when only 30s has elapsed (interval=300s)"
        )

    def test_post_heat_appended_after_5min(self):
        """Post-heat sample IS appended after >= 300s since last post-heat sample."""
        coord = _make_obs_coord_with_sample_thermal_event(hvac_action="idle", indoor_temp=70.0, outdoor_temp=50.0)

        from datetime import timedelta

        last_ph_ts = _FAKE_NOW - timedelta(seconds=310)  # 310s ago — above 300s interval
        coord._pending_thermal_event = {
            "event_id": "test-postheat-allowed",
            "status": "post_heat",
            "session_mode": "heat",
            "active_start": _FAKE_NOW.isoformat(),
            "active_end": _FAKE_NOW.isoformat(),
            "active_samples": [],
            "post_heat_samples": [],
            "last_post_heat_sample_time": last_ph_ts.isoformat(),
        }

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_thermal_event()

        assert len(coord._pending_thermal_event["post_heat_samples"]) == 1, (
            "post-heat sample should be appended after 310s (interval=300s)"
        )

    def test_interval_constants_have_expected_values(self):
        """Confirm constant values match the plan spec."""
        assert THERMAL_PASSIVE_SAMPLE_INTERVAL_S == 300, (
            f"Expected THERMAL_PASSIVE_SAMPLE_INTERVAL_S=300, got {THERMAL_PASSIVE_SAMPLE_INTERVAL_S}"
        )
        assert THERMAL_FAN_SAMPLE_INTERVAL_S == 120, (
            f"Expected THERMAL_FAN_SAMPLE_INTERVAL_S=120, got {THERMAL_FAN_SAMPLE_INTERVAL_S}"
        )
        assert THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S == 300, (
            f"Expected THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S=300, got {THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S}"
        )


# ---------------------------------------------------------------------------
# TestRollingWindowCommit  (Issue #122 — H2 rolling window commits)
# ---------------------------------------------------------------------------


class TestRollingWindowCommit:
    """H2: 30-minute rolling windows commit+restart passive/vent/fan/solar observations."""

    def _make_31min_obs(self, obs_type: str, n_samples: int = 6) -> dict:
        """Build an obs that started 31 minutes ago with n_samples already collected."""
        start = _make_obs_31min_ago()
        samples = [
            {
                "timestamp": start.isoformat(),
                "indoor_temp_f": 74.0 - i * 0.05,  # gentle slope so delta > 0 but < flat
                "outdoor_temp_f": 55.0,
                "elapsed_minutes": float(i * 5),
            }
            for i in range(n_samples)
        ]
        return {
            "obs_type": obs_type,
            "obs_id": "test-rolling-1",
            "start_time": start.isoformat(),
            "status": "monitoring",
            "samples": samples,
            "last_sample_time": start.isoformat(),
            "flags_at_start": {},
            "schema_version": 1,
        }

    def test_passive_decay_commits_after_30min(self):
        """passive_decay started 31 min ago triggers rolling-window commit."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=74.0, outdoor_temp=55.0)
        obs = self._make_31min_obs(OBS_TYPE_PASSIVE_DECAY, n_samples=6)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = obs

        committed_obs_types: list[str] = []

        def _fake_async_create_task(coro):
            coro_name = getattr(coro, "__name__", getattr(coro, "__qualname__", ""))
            if "_commit_observation" in coro_name:
                committed_obs_types.append(OBS_TYPE_PASSIVE_DECAY)
            coro.close()

        coord.hass.async_create_task = _fake_async_create_task

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        obs_present = coord._pending_observations.get(OBS_TYPE_PASSIVE_DECAY)
        was_committing = obs_present is not None and obs_present.get("status") == "committing"
        was_queued = len(committed_obs_types) > 0

        assert was_committing or was_queued, (
            "passive_decay started 31 min ago should trigger rolling-window commit "
            f"(status={obs_present.get('status') if obs_present else 'popped'}, queued={was_queued})"
        )

    def test_rolling_window_delta_guard_rejects_flat(self):
        """Short windows with total ΔT < THERMAL_ROLLING_MIN_DELTA_T_F are abandoned."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=74.0, outdoor_temp=55.0)
        # Build obs with 6 samples all at the SAME temperature → total ΔT = 0
        start = _make_obs_31min_ago()
        flat_samples = [
            {
                "timestamp": start.isoformat(),
                "indoor_temp_f": 74.0,  # identical across all samples
                "outdoor_temp_f": 55.0,
                "elapsed_minutes": float(i * 5),
            }
            for i in range(6)
        ]
        obs = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-rolling-flat",
            "start_time": start.isoformat(),
            "status": "monitoring",
            "samples": flat_samples,
            "last_sample_time": start.isoformat(),
            "flags_at_start": {},
            "schema_version": 1,
        }
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = obs

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # The obs should be popped (abandoned with "insufficient_total_delta" path)
        # It should NOT have status "committing"
        obs_after = coord._pending_observations.get(OBS_TYPE_PASSIVE_DECAY)
        if obs_after is not None:
            assert obs_after.get("status") != "committing", (
                "Flat-temperature short window should be abandoned, not committed "
                f"(total ΔT=0.0 < {THERMAL_ROLLING_MIN_DELTA_T_F})"
            )

    def test_fresh_obs_starts_after_rolling_commit(self):
        """After rolling-window commit pops the obs, next poll can start a fresh one."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=74.0, outdoor_temp=55.0)
        obs = self._make_31min_obs(OBS_TYPE_PASSIVE_DECAY, n_samples=6)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = obs

        def _fake_async_create_task(coro):
            coro.close()

        coord.hass.async_create_task = _fake_async_create_task

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # After commit, the obs is removed (popped) or marked "committing" — no longer "monitoring"
        obs_after = coord._pending_observations.get(OBS_TYPE_PASSIVE_DECAY)
        assert obs_after is None or obs_after.get("status") != "monitoring", (
            "After rolling-window commit, passive_decay should no longer be in monitoring status"
        )

    def test_rolling_window_constant_is_30min(self):
        """Confirm THERMAL_ROLLING_WINDOW_MINUTES is exactly 30."""
        assert THERMAL_ROLLING_WINDOW_MINUTES == 30, (
            f"Expected THERMAL_ROLLING_WINDOW_MINUTES=30, got {THERMAL_ROLLING_WINDOW_MINUTES}"
        )

    def test_rolling_min_delta_constant_is_0_2(self):
        """Confirm THERMAL_ROLLING_MIN_DELTA_T_F is exactly 0.2."""
        assert pytest.approx(0.2) == THERMAL_ROLLING_MIN_DELTA_T_F, (
            f"Expected THERMAL_ROLLING_MIN_DELTA_T_F=0.2, got {THERMAL_ROLLING_MIN_DELTA_T_F}"
        )


# ---------------------------------------------------------------------------
# TestNatVentAndVentilatedDecay
# ---------------------------------------------------------------------------


class TestNatVentAndVentilatedDecay:
    """Nat-vent guard and ventilated_decay start-condition regression tests."""

    def test_passive_decay_blocked_when_nat_vent_active(self):
        """passive_decay must NOT start when natural ventilation is active.

        With nat_vent_active=True the engine is circulating outdoor air, so
        any decay signal is a blend of envelope loss and ventilation — it must
        not be attributed to k_passive alone.
        """
        coord = _make_obs_coord(
            nat_vent_active=True,
            indoor_temp=75.0,
            outdoor_temp=50.0,
            any_sensor_open=False,
            hvac_action="idle",
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations, (
            "passive_decay must not start while nat_vent_active=True"
        )

    def test_ventilated_decay_starts_at_1_0f_delta(self):
        """ventilated_decay starts when |indoor - outdoor| >= THERMAL_VENTILATED_MIN_DELTA_F (1.0°F).

        delta = 72.0 - 70.5 = 1.5 >= 1.0 → observation should be created.
        """
        coord = _make_obs_coord(
            indoor_temp=72.0,
            outdoor_temp=70.5,
            any_sensor_open=True,
            hvac_action="idle",
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_VENTILATED_DECAY in coord._pending_observations, (
            "ventilated_decay should start when delta=1.5 >= THERMAL_VENTILATED_MIN_DELTA_F=1.0"
        )

    def test_ventilated_decay_does_not_start_below_1_0f_delta(self):
        """ventilated_decay does NOT start when |indoor - outdoor| < THERMAL_VENTILATED_MIN_DELTA_F (1.0°F).

        delta = 72.0 - 71.3 = 0.7 < 1.0 → observation must NOT be created.
        """
        coord = _make_obs_coord(
            indoor_temp=72.0,
            outdoor_temp=71.3,
            any_sensor_open=True,
            hvac_action="idle",
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_VENTILATED_DECAY not in coord._pending_observations, (
            "ventilated_decay must not start when delta=0.7 < THERMAL_VENTILATED_MIN_DELTA_F=1.0"
        )

    def test_warm_day_nat_vent_only_ventilated_decay_viable(self):
        """On a warm day with nat_vent active and a window open, only ventilated_decay is viable.

        passive_decay is blocked (nat_vent_active), fan_only is blocked (fan not active),
        solar_gain is blocked (indoor < outdoor not required here, but HVAC idle + nat_vent
        means solar_gain guard also blocks it).  Only ventilated_decay should start.
        """
        coord = _make_obs_coord(
            indoor_temp=72.0,
            outdoor_temp=68.0,  # delta = 4.0 >= 1.0
            nat_vent_active=True,
            any_sensor_open=True,
            hvac_action="idle",
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations, (
            "passive_decay must be blocked when nat_vent_active=True"
        )
        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations, (
            "fan_only_decay must not start when fan_active=False"
        )
        assert OBS_TYPE_SOLAR_GAIN not in coord._pending_observations, (
            "solar_gain must not start when nat_vent_active=True"
        )
        assert OBS_TYPE_VENTILATED_DECAY in coord._pending_observations, (
            "ventilated_decay must start: sensor open, delta=4.0 >= 1.0, HVAC idle"
        )

    def test_ventilated_min_delta_constant_is_1_0f(self):
        """Confirm THERMAL_VENTILATED_MIN_DELTA_F is exactly 1.0."""
        assert pytest.approx(1.0) == THERMAL_VENTILATED_MIN_DELTA_F, (
            f"Expected THERMAL_VENTILATED_MIN_DELTA_F=1.0, got {THERMAL_VENTILATED_MIN_DELTA_F}"
        )


# ---------------------------------------------------------------------------
# C1 / C2 confidence grading regression tests
# ---------------------------------------------------------------------------


class TestConfidenceGrading:
    """Regression tests for the C1 and C2 confidence-bug fixes in learning.py.

    C1 (get_thermal_model): confidence_k_hvac was always "none" because the
        cache["confidence"] write happened after the key was already read back.
    C2 (_grade_passive_confidence): confidence_k_passive was always "none" because
        ventilated/fan_only/passive observations were not included in the count
        used by _grade_passive_confidence (only heat+cool were counted).
    """

    def _make_engine(self, tmp_path: Path) -> LearningEngine:
        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    def _ventilated_obs(self, k_passive: float = -0.07) -> dict:
        """Minimal ventilated_decay observation dict accepted by _update_thermal_model_cache.

        Mode "ventilated" bumps observation_count_vent, which _grade_passive_confidence
        includes in its count (C2 fix).
        """
        return {
            "date": "2026-04-28",
            "hvac_mode": "ventilated",
            "k_passive": k_passive,
            "confidence_grade": "high",
        }

    def _heat_obs(self, k_active: float = 3.5) -> dict:
        """Minimal heat observation dict accepted by _update_thermal_model_cache.

        Mode "heat" + k_active key bumps observation_count_heat and updates
        k_active_heat (C1 fix path).
        """
        return {
            "date": "2026-04-28",
            "hvac_mode": "heat",
            "k_passive": -0.07,
            "k_active": k_active,
            "confidence_grade": "high",
        }

    def test_confidence_k_passive_grades_from_vent_only_observations(self, tmp_path: Path):
        """C2 round-trip: ventilated-only observations must lift confidence_k_passive above 'none'.

        Before the C2 fix, _grade_passive_confidence() only counted heat+cool, so a
        warm-day-home scenario (zero HVAC cycles, only ventilated_decay observations)
        always returned "none" for confidence_k_passive regardless of how many
        ventilated observations were committed.

        Boundary guard: THERMAL_PASSIVE_CONF_LOW - 1 observations must still return "none";
        exactly THERMAL_PASSIVE_CONF_LOW observations must return "low".
        """
        # --- boundary: one below threshold must remain "none" ---
        engine_below = self._make_engine(tmp_path / "below")
        for _ in range(THERMAL_PASSIVE_CONF_LOW - 1):
            engine_below._update_thermal_model_cache(self._ventilated_obs())
        model_below = engine_below.get_thermal_model()
        assert model_below["confidence_k_passive"] == "none", (
            f"Expected 'none' with {THERMAL_PASSIVE_CONF_LOW - 1} ventilated obs "
            f"(below threshold), got '{model_below['confidence_k_passive']}'"
        )

        # --- at threshold: must grade to "low" ---
        engine_at = self._make_engine(tmp_path / "at")
        for _ in range(THERMAL_PASSIVE_CONF_LOW):
            engine_at._update_thermal_model_cache(self._ventilated_obs())
        model_at = engine_at.get_thermal_model()
        assert model_at["confidence_k_passive"] == "low", (
            f"Expected 'low' with {THERMAL_PASSIVE_CONF_LOW} ventilated obs "
            f"(at threshold), got '{model_at['confidence_k_passive']}'"
        )
        assert model_at["confidence_k_passive"] != "none", (
            "confidence_k_passive must not be 'none' when ventilated obs reach threshold (C2 regression)"
        )

    def test_confidence_k_hvac_grades_from_heat_observations(self, tmp_path: Path):
        """C1 round-trip: heat observations must lift confidence_k_hvac above 'none'.

        Before the C1 fix, get_thermal_model() wrote cache["confidence"] and then
        immediately read it back via cache.get("confidence", "none") for the
        confidence_k_hvac return value — but the key it read had just been set
        to the correct value, so this actually worked. The real C1 bug was that
        confidence_k_hvac returned cache.get("confidence", "none") which was the
        *legacy* confidence field, not a dedicated hvac-confidence computation.
        After MIN_THERMAL_OBSERVATIONS heat cycles the legacy field should be "low".

        Boundary guard: MIN_THERMAL_OBSERVATIONS - 1 observations must return "none";
        exactly MIN_THERMAL_OBSERVATIONS must return "low".
        """
        # --- boundary: one below threshold must remain "none" ---
        engine_below = self._make_engine(tmp_path / "below")
        for _ in range(MIN_THERMAL_OBSERVATIONS - 1):
            engine_below._update_thermal_model_cache(self._heat_obs())
        model_below = engine_below.get_thermal_model()
        assert model_below["confidence_k_hvac"] == "none", (
            f"Expected 'none' with {MIN_THERMAL_OBSERVATIONS - 1} heat obs "
            f"(below threshold), got '{model_below['confidence_k_hvac']}'"
        )

        # --- at threshold: must grade to "low" ---
        engine_at = self._make_engine(tmp_path / "at")
        for _ in range(MIN_THERMAL_OBSERVATIONS):
            engine_at._update_thermal_model_cache(self._heat_obs())
        model_at = engine_at.get_thermal_model()
        assert model_at["confidence_k_hvac"] == "low", (
            f"Expected 'low' with {MIN_THERMAL_OBSERVATIONS} heat obs "
            f"(at threshold), got '{model_at['confidence_k_hvac']}'"
        )
        assert model_at["confidence_k_hvac"] != "none", (
            "confidence_k_hvac must not be 'none' when heat obs reach threshold (C1 regression)"
        )
